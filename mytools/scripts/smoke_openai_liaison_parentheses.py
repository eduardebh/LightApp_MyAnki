from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

from frequency_db_utils import word_adder  # noqa: E402


def _require_env(name: str) -> str:
	value = os.getenv(name, "").strip()
	if not value:
		raise SystemExit(
			f"{name} no está configurada.\n"
			"Configúrala y vuelve a ejecutar:\n"
			f"  $env:{name}=\"...\"\n"
			"  python scripts\\smoke_openai_liaison_parentheses.py\n\n"
			"O agrega la variable en el archivo .env en la raíz del repo."
		)
	return value


def main() -> None:
	word_adder._load_dotenv_if_present()
	api_key = _require_env("OPENAI_API_KEY")

	list_id = 22
	language = "fr"
	words = ["amis", "ont", "avez"]
	if len(sys.argv) > 1:
		words = [w.strip() for w in sys.argv[1:] if w.strip()]

	for w in words:
		result = word_adder.prepare_word_actions(
			palabra=w,
			list_id=list_id,
			language=language,
			openai_api_key=api_key,
			user_id=os.getenv("MYTOOLS_USER_ID", "local").strip() or "local",
		)

		print("\n===", w, "===")
		print("pos:", result.get("pos"), "is_verb:", result.get("is_verb"), "is_noun:", result.get("is_noun"))
		print("association:", result.get("association"))
		print("ipa_word:    ", result.get("ipa_word"))

		entries = result.get("entries") or []
		if entries:
			print("verb_entries sample:")
			for entry in entries[:6]:
				print(" -", entry.get("phrase"), "=>", entry.get("ipa"))


if __name__ == "__main__":
	main()
