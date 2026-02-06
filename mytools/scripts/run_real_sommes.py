from __future__ import annotations

import os
import sys
from pathlib import Path

# Make repo root importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frequency_db_utils import word_adder  # noqa: E402


def main() -> None:
	word = "sommes"
	if len(sys.argv) > 1 and sys.argv[1].strip():
		word = sys.argv[1].strip()

	user_id = os.getenv("MYTOOLS_USER_ID", "local").strip() or "local"

	result = word_adder.prepare_word_actions(word, 22, "fr", user_id=user_id)


	inserts = [params for (sql, params) in result["queries"] if "INSERT INTO words" in sql]
	print(f"Generated INSERT rows: {len(inserts)}")
	for params in inserts:
		# Print all values in the tuple for debugging
		print("- " + " | ".join(str(x) for x in params))

	# Try to extract word and ipa for by_word dict, fallback to safe indexing
	by_word = {}
	for params in inserts:
		# Try to find word and ipa by position (word, association, list_id, ipa, ...)
		word = params[0] if len(params) > 0 else None
		ipa = params[3] if len(params) > 3 else None
		by_word[word] = ipa or ""

	problems: list[str] = []

	# Targeted liaison checks when those phrases exist.
	checks = {
		# expect /z/ liaison
		"vous êtes": True,
		"vous avez": True,
		"ils ont": True,
		"elles ont": True,
		# do NOT invent /z/
		"ils sont": False,
		"elles sont": False,
	}

	for phrase, must_have_z in checks.items():
		ipa = by_word.get(phrase)
		if ipa is None:
			continue
		has_z = "z" in ipa
		if must_have_z and not has_z:
			problems.append(f"Expected liaison /z/ in IPA for '{phrase}', got: {ipa}")
		elif (not must_have_z) and has_z:
			problems.append(f"Unexpected /z/ in IPA for '{phrase}', got: {ipa}")

	print("\nLiaison validation:")
	if problems:
		print("FAIL")
		for p in problems:
			print(f"- {p}")
	else:
		print("PASS")


if __name__ == "__main__":
	main()
