# frequency-db-utils

Small helper package that provides `frequency_db_utils.add_word` — a helper
to insert a word into a Postgres `words` table, fetch translation, IPA and
verb forms (if applicable), and insert related forms as separate rows.

Usage

1. Install dependencies (recommended inside a venv):

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

2. Call from your code passing a psycopg2 `conn` and `cur`:

```py
from frequency_db_utils.word_adder import add_word

result = add_word(conn, cur, 'haus', list_id=1, language='de')
print(result)
```

Notes
- The function does NOT commit; the caller should call `conn.commit()`.
- IPA lookup: this project no longer queries the external `dictionaryapi.dev` service.
	Instead, IPA/phonetic transcriptions are generated using the OpenAI Chat API (when
	an `OPENAI_API_KEY` is available). Set `OPENAI_API_KEY` in your environment or pass
	an `openai_api_key` to `add_word`.

Real OpenAI tests (no mocks)

- Phrase suite (prints IPA + flags formatting issues):
	- `python scripts/run_real_phrase_suite.py`
- Table suite (strict compare against the reference table in the script):
	- `python scripts/run_real_openai_table_suite.py`

Backup database (Postgres)

- Requires `pg_dump` (comes with PostgreSQL).
- Uses `DATABASE_URL` from your environment or repo `.env`.

```powershell
python scripts/backup_database.py
```

Optional:

```powershell
# Explicit output folder
python scripts/backup_database.py --out-dir backups

# Pick format (custom|plain|tar|directory)
python scripts/backup_database.py --format plain
```

Security & behaviour:
- The OpenAI fallback is used only when an API key is present; network calls may
	incur costs depending on your OpenAI plan. The package will not attempt to write
	or commit to the database unless the caller explicitly calls `conn.commit()`.

version:1
