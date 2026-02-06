from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

# Ensure repo root importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frequency_db_utils import word_adder  # noqa: E402


def _collect_inserts(actions: dict) -> list[tuple[str, str | None, int, str | None]]:
	rows: list[tuple[str, str | None, int, str | None]] = []
	for sql, params in actions.get("queries", []):
		if "INSERT INTO words" in sql:
			word, association, list_id, ipa = params
			rows.append((word, association, list_id, ipa))
	return rows


def main() -> None:
	word_adder._load_dotenv_if_present()
	db_url = os.getenv("DATABASE_URL")
	if not db_url:
		raise SystemExit("DATABASE_URL not set (check .env)")

	user_id = os.getenv("MYTOOLS_USER_ID", "local").strip() or "local"

	inputs = ["sommes", "avez", "été"]
	list_id = 22
	language = "fr"

	print("Calling OpenAI + preparing SQL…")
	all_rows: list[tuple[str, str | None, int, str | None]] = []
	per_input: dict[str, list[tuple[str, str | None, int, str | None]]] = {}

	for w in inputs:
		actions = word_adder.prepare_word_actions(w, list_id, language, user_id=user_id)
		rows = _collect_inserts(actions)
		per_input[w] = rows
		all_rows.extend(rows)
		print(f"- {w}: {len(rows)} INSERT rows")

	print("\nInserting into DB…")
	conn = psycopg2.connect(db_url)
	try:
		conn.autocommit = False
		with conn.cursor() as cur:
			for w, association, lid, ipa in all_rows:
				cur.execute(
					"""
					INSERT INTO words (word, used, association, state, list_id, successes, "IPA_word")
					VALUES (%s, FALSE, %s, 'New', %s, 0, %s)
					ON CONFLICT (word, list_id) DO NOTHING;
					""".strip(),
					(w, association, lid, ipa),
				)
			conn.commit()

		print("\nSelecting from DB (words just inserted or already present)…")
		with conn.cursor() as cur:
			# Fetch exactly the words we attempted to insert
			words = [r[0] for r in all_rows]
			cur.execute(
				"""
				SELECT word, list_id, association, "IPA_word"
				FROM words
				WHERE list_id = %s AND word = ANY(%s)
				ORDER BY word;
				""".strip(),
				(list_id, words),
			)
			rows = cur.fetchall()
			print(f"DB returned {len(rows)} rows")
			for word, lid, assoc, ipa in rows:
				print(f"- {word} | {ipa} | {assoc}")
	finally:
		conn.close()


if __name__ == "__main__":
	main()
