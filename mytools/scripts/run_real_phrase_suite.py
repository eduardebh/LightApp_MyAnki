from __future__ import annotations


import os
import sys
from pathlib import Path

# Make repo root importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frequency_db_utils import word_adder  # noqa: E402


def _format_issues(ipa: str | None) -> list[str]:
	issues: list[str] = []
	if not ipa or not str(ipa).strip():
		return ["missing"]
	s = str(ipa)
	inside = s.strip()
	if inside.startswith("/") and inside.endswith("/"):
		inside = inside[1:-1]
	if " \u203f" in inside or "\u203f " in inside:
		issues.append("space_around_link")
	return issues


def _extract_insert_rows(result: dict) -> list[tuple]:
	queries = result.get("queries") or []
	inserts: list[tuple] = []
	for sql, params in queries:
		if "INSERT INTO words" in (sql or ""):
			inserts.append(params)
	return inserts


def main() -> None:
	list_id = int(os.getenv("MYTOOLS_LIST_ID", "22"))
	user_id = os.getenv("MYTOOLS_USER_ID", "local").strip() or "local"
	language = os.getenv("MYTOOLS_LANG", "fr").strip() or "fr"

	phrases = [
		"vous êtes",
		"nous avons",
		"ils ont",
		"elles ont",
		"ils sont",
		"elles ont été",
		"ils ont été",
	]

	print(f"Model default: {os.getenv('OPENAI_MODEL', 'gpt-4o')} (env override if set)")
	print(f"Language={language} list_id={list_id} user_id={user_id}")
	print("-")

	total_inserts = 0
	total_null_ipa = 0
	missing_target_ipa: list[str] = []

	for text in phrases:
		result = word_adder.prepare_word_actions(text, list_id, language, user_id=user_id)
		inserts = _extract_insert_rows(result)
		total_inserts += len(inserts)

		# Build lookup for IPA by word/phrase
		by_word: dict[str, str | None] = {}
		for params in inserts:
			w = params[0] if len(params) > 0 else None
			ipa = params[3] if len(params) > 3 else None
			if isinstance(w, str):
				by_word[w] = ipa
			if ipa is None:
				total_null_ipa += 1

		target_ipa = by_word.get(text)
		issues = _format_issues(target_ipa)
		status = "OK" if not issues else "BAD_FORMAT" if issues != ["missing"] else "NO_IPA"
		if status != "OK":
			missing_target_ipa.append(text)
		issues_txt = ("" if not issues else " issues=" + ",".join(issues))
		print(f"{text} -> {target_ipa} [{status}]{issues_txt}")

		print("-")

	print(f"TOTAL inserts: {total_inserts}")
	print(f"TOTAL rows with NULL IPA: {total_null_ipa}")
	if missing_target_ipa:
		print("Phrases with missing IPA (NULL/empty):")
		for t in missing_target_ipa:
			print(f"- {t}")
	else:
		print("All target phrases returned IPA.")


if __name__ == "__main__":
	main()
