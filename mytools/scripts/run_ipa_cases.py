from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Ensure the repository root is on sys.path so we can import local packages
# when executing this file as a script (python scripts/run_ipa_cases.py).
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
	sys.path.insert(0, str(ROOT_DIR))

from frequency_db_utils import word_adder


@dataclass(frozen=True)
class Case:
	text: str
	language: str
	note: str = ""


def _iter_cases(path: Path, default_language: str) -> Iterable[Case]:
	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue
		parts = [p.strip() for p in line.split("|")]
		text = parts[0] if parts else ""
		language = (parts[1] if len(parts) > 1 and parts[1] else default_language).strip()
		note = (parts[2] if len(parts) > 2 else "").strip()
		if text:
			yield Case(text=text, language=language, note=note)


def _print_case_result(case: Case, ipa: Optional[str]) -> None:
	note = f"  # {case.note}" if case.note else ""
	print(f"{case.language}\t{case.text}{note}")
	print(f"  -> {ipa or ''}")


def main() -> int:
	parser = argparse.ArgumentParser(description="Run IPA cases via frequency_db_utils.word_adder prompts")
	parser.add_argument(
		"--text",
		default="",
		help="Run a single case (overrides --file).",
	)
	parser.add_argument(
		"--file",
		default=str(Path(__file__).with_name("IPA_cases.txt")),
		help="Path to cases file (default: scripts/IPA_cases.txt)",
	)
	parser.add_argument("--language", default="fr", help="Default language if omitted per line")
	parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds per request (default: 60)")
	parser.add_argument("--retries", type=int, default=2, help="Retries on timeout/temporary network errors (default: 2)")
	parser.add_argument(
		"--api-key",
		default=os.getenv("OPENAI_API_KEY", ""),
		help="OpenAI API key (default: env OPENAI_API_KEY)",
	)
	args = parser.parse_args()

	# Load .env (repo root) so OPENAI_API_KEY works out-of-the-box.
	# Reuse the same tiny parser used by the main module.
	word_adder._load_dotenv_if_present()

	api_key = (args.api_key or os.getenv("OPENAI_API_KEY", "")).strip()
	if not api_key:
		raise SystemExit("Missing OpenAI API key. Provide --api-key or set OPENAI_API_KEY.")

	text = (args.text or "").strip()
	if text:
		cases = [Case(text=text, language=str(args.language), note="")]
	else:
		path = Path(args.file)
		if not path.exists():
			raise SystemExit(f"Cases file not found: {path}")
		cases = list(_iter_cases(path, default_language=str(args.language)))
	if not cases:
		print("No cases found.")
		return 0

	for case in cases:
		last_error: Optional[BaseException] = None
		ipa: Optional[str] = None
		for attempt in range(int(args.retries) + 1):
			try:
				ipa = word_adder._get_phrase_ipa(api_key, case.language, case.text, timeout=int(args.timeout))
				last_error = None
				break
			except Exception as exc:  # best-effort runner
				last_error = exc
				# tiny backoff for transient network errors
				if attempt < int(args.retries):
					time.sleep(0.5 * (attempt + 1))
		if last_error is not None:
			_print_case_result(case, None)
			print(f"  !! ERROR: {type(last_error).__name__}: {last_error}")
			continue
		_print_case_result(case, ipa)

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
