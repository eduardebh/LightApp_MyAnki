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
			f"Missing {name}. Set it in env or .env at repo root."
		)
	return value


def main() -> int:
	# Load .env if present
	word_adder._load_dotenv_if_present()
	api_key = _require_env("OPENAI_API_KEY")

	language = os.getenv("MYTOOLS_LANG", "fr").strip() or "fr"
	model = os.getenv("OPENAI_MODEL", "gpt-4o")

	# Table-driven expectations for the FULL PHRASE.
	# NOTE: Strict string compare against the phrase-level IPA.
	expected: dict[str, str] = {
		# ÊTRE (présent)
		"je suis": "/ʒə sɥi/",
		"tu es": "/ty ɛ/",
		"il est": "/il ɛ/",
		"elle est": "/ɛl ɛ/",
		"on est": "/ɔ̃ ɛ/",
		"nous sommes": "/nu sɔm/",
		"vous êtes": "/vu z‿ɛt/",
		"ils sont": "/il sɔ̃/",
		"elles sont": "/ɛl sɔ̃/",
		# AVOIR (présent)
		"j'ai": "/ʒe/",
		"tu as": "/ty a/",
		"il a": "/il a/",
		"elle a": "/ɛl a/",
		"on a": "/ɔ̃ a/",
		"nous avons": "/nu z‿avɔ̃/",
		"vous avez": "/vu z‿ave/",
		"ils ont": "/il z‿ɔ̃/",
		"elles ont": "/ɛl z‿ɔ̃/",
		# ALLER (présent)
		"je vais": "/ʒə vɛ/",
		"tu vas": "/ty va/",
		"il va": "/il va/",
		"elle va": "/ɛl va/",
		"on va": "/ɔ̃ va/",
		"nous allons": "/nu z‿alɔ̃/",
		"vous allez": "/vu z‿ale/",
		"ils vont": "/il vɔ̃/",
		"elles vont": "/ɛl vɔ̃/",
		# AVOIR (parfait)
		"j'ai eu": "/ʒe‿y/",
		"tu as eu": "/ty a‿y/",
		"il a eu": "/il a‿y/",
		"elle a eu": "/ɛl a‿y/",
		"on a eu": "/ɔ̃ a‿y/",
		"nous avons eu": "/nu z‿avɔ̃‿y/",
		"vous avez eu": "/vu z‿ave‿y/",
		"ils ont eu": "/il z‿ɔ̃‿t‿y/",
		"elles ont eu": "/ɛl z‿ɔ̃‿t‿y/",
		# ALLER (parfait)
		"je suis allé": "/ʒə sɥi‿a.le/",
		"tu es allé": "/ty ɛ‿a.le/",
		"il est allé": "/il ɛ‿a.le/",
		"elle est allé": "/ɛl ɛ‿a.le/",
		"on est allé": "/ɔ̃ ɛ‿a.le/",
		"nous sommes allé": "/nu sɔm‿a.le/",
		"vous êtes allé": "/vu z‿ɛt‿a.le/",
		"ils sont allé": "/il sɔ̃‿t‿a.le/",
		"elles sont allé": "/ɛl sɔ̃‿t‿a.le/",
	}

	print(f"Model: {model}")
	print(f"Language: {language}")
	print("-")

	fails: list[str] = []
	for phrase, exp in expected.items():
		ipa = word_adder._get_phrase_ipa(api_key, language, phrase, timeout=60)
		got = (ipa or "").strip()
		ok = got == exp
		status = "OK" if ok else "MISMATCH"
		print(f"{phrase} -> {got} [{status}] expected={exp}")
		if not ok:
			fails.append(phrase)

	print("-")
	if fails:
		print("Mismatches:")
		for p in fails:
			print(f"- {p}")
		return 1

	print("All table rows matched.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
