# DB Migrations

This repository includes a small migration runner `db_migrate.py` that applies SQL files placed in the `migrations/` folder.

Quick usage:

- Show status:
  ```
  python db_migrate.py status
  ```

- Apply pending migrations (reads `DATABASE_URL` from env or use `--db`):
  ```
  export DATABASE_URL="postgresql://..."
  python db_migrate.py apply
  # or on Windows PowerShell
  $env:DATABASE_URL = 'postgresql://...'
  python db_migrate.py apply
  ```

- Create a new migration template (will be created in `migrations/`):
  ```
  python db_migrate.py create add_new_column
  ```

Notes:
- Migrations are applied in filename sort order. Filenames should start with a sortable prefix (timestamp or sequential number).
- The script records applied migrations in table `applied_migrations` in the target DB; it will create that table if missing.
- Migration SQL should be idempotent or check existence before altering schema (examples in `migrations/001_fix_schema.sql`).
