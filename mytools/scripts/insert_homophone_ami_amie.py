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
			"  python scripts\\insert_homophone_ami_amie.py\n\n"
			"O agrega la variable en el archivo .env en la raíz del repo."
		)
	return value


def main() -> None:
	# Load repo .env if present
	word_adder._load_dotenv_if_present()

	db_url = _require_env("DATABASE_URL")
	api_key = _require_env("OPENAI_API_KEY")
	user_id = os.getenv("MYTOOLS_USER_ID", "local").strip() or "local"

	list_id = 22
	language = "fr"
	default_words = ["ami", "amie"]

	word_1 = default_words[0]
	word_2: str | None = default_words[1]

	if len(sys.argv) >= 2 and sys.argv[1].strip():
		word_1 = sys.argv[1].strip()
	if len(sys.argv) >= 3:
		arg2 = sys.argv[2].strip()
		if not arg2 or arg2.lower() in {"-", "none", "null"}:
			word_2 = None
		else:
			word_2 = arg2
	if len(sys.argv) >= 4 and sys.argv[3].strip():
		list_id = int(sys.argv[3].strip())

	words = [word_1] + ([word_2] if word_2 else [])

	try:
		import psycopg2  # type: ignore
	except Exception as e:
		raise SystemExit(
			"Falta psycopg2. Instálalo (idealmente en tu venv):\n"
			"  pip install psycopg2-binary\n\n"
			f"Detalle: {e}"
		)

	print(f"Connecting DB (list_id={list_id}, lang={language}) …")
	conn = psycopg2.connect(db_url)
	try:
		conn.autocommit = False
		with conn.cursor() as cur:
			print("\nBefore:")
			cur.execute(
				"""
				SELECT word, association, "IPA_word"
				FROM words
				WHERE list_id = %s AND word = ANY(%s)
				ORDER BY word;
				""".strip(),
				(list_id, words),
			)
			for w, assoc, ipa in cur.fetchall():
				print(f"- {w} | {ipa} | {assoc}")

			for w in words:
				print(f"\nPreparing + inserting: {w}")
				actions = word_adder.prepare_word_actions(
					palabra=w,
					list_id=list_id,
					language=language,
					openai_api_key=api_key,
					user_id=user_id,
				)
				for sql, params in actions.get("queries", []):
					cur.execute(sql, params)
				conn.commit()

				cur.execute(
					"""
					SELECT word, association, "IPA_word"
					FROM words
					WHERE list_id = %s AND word = ANY(%s)
					ORDER BY word;
					""".strip(),
					(list_id, words),
				)
				print("After commit:")
				for w2, assoc2, ipa2 in cur.fetchall():
					print(f"- {w2} | {ipa2} | {assoc2}")

		print("\nDone.")
	finally:
		conn.close()


if __name__ == "__main__":
	main()
