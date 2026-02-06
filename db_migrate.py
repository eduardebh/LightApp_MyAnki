#!/usr/bin/env python3
"""
Simple DB migration runner for the project.

Features:
- Reads `DATABASE_URL` from env or `--db` argument
- Keeps applied migrations in table `applied_migrations`
- Commands: status, apply, create <name>

Usage examples:
  python db_migrate.py status
  python db_migrate.py apply
  python db_migrate.py create add_new_column

This is intentionally small and dependency-free (uses only `psycopg2`).
"""
import os
import sys
import argparse
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
from glob import glob
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).parent / 'migrations'


def get_conn(db_url=None):
    if db_url is None:
        db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise RuntimeError('DATABASE_URL not set (or --db not provided)')
    return psycopg2.connect(db_url)


def ensure_migrations_table(conn):
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS applied_migrations (
        id SERIAL PRIMARY KEY,
        filename TEXT NOT NULL UNIQUE,
        applied_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
    );
    ''')
    conn.commit()
    cur.close()


def list_migration_files():
    MIGRATIONS_DIR.mkdir(exist_ok=True)
    files = sorted([Path(p) for p in glob(str(MIGRATIONS_DIR / '*.sql'))])
    return files


def get_applied(conn):
    cur = conn.cursor()
    cur.execute('SELECT filename FROM applied_migrations ORDER BY filename')
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    return set(rows)


def apply_migration(conn, path: Path):
    sql = path.read_text(encoding='utf-8')
    cur = conn.cursor()
    try:
        cur.execute('BEGIN');
        cur.execute(sql)
        cur.execute('INSERT INTO applied_migrations (filename, applied_at) VALUES (%s, %s)', (path.name, datetime.utcnow()))
        conn.commit()
        print(f'APPLIED: {path.name}')
    except Exception:
        conn.rollback()
        print(f'FAILED: {path.name}')
        raise
    finally:
        cur.close()


def cmd_status(conn):
    files = list_migration_files()
    applied = get_applied(conn)
    if not files:
        print('No migration files found in migrations/')
        return
    print('Migrations status:')
    for f in files:
        mark = 'APPLIED' if f.name in applied else 'PENDING'
        print(f'  {f.name:40} {mark}')


def cmd_apply(conn):
    ensure_migrations_table(conn)
    files = list_migration_files()
    if not files:
        print('No migration files to apply.')
        return
    applied = get_applied(conn)
    pending = [f for f in files if f.name not in applied]
    if not pending:
        print('No pending migrations.')
        return
    print(f'Applying {len(pending)} migration(s)...')
    for p in pending:
        apply_migration(conn, p)


def cmd_create(name: str):
    MIGRATIONS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename = f"{timestamp}_{name}.sql"
    path = MIGRATIONS_DIR / filename
    template = """-- Migration: %s
-- Write your SQL statements below. For Postgres, you can use DO $$ BEGIN ... END $$;

-- Example safe operation (add column if not exists):
-- DO $$ BEGIN
--     IF NOT EXISTS (
--         SELECT 1 FROM information_schema.columns
--         WHERE table_name='words' AND column_name='example_col'
--     ) THEN
--         ALTER TABLE words ADD COLUMN example_col TEXT;
--     END IF;
-- END $$;

""" % name
    path.write_text(template, encoding='utf-8')
    print(f'Created migration: {path}')


def main(argv):
    parser = argparse.ArgumentParser(description='DB migration helper')
    parser.add_argument('--db', help='Database URL (overrides DATABASE_URL env)')
    parser.add_argument('command', choices=['status', 'apply', 'create'], help='Command')
    parser.add_argument('name', nargs='?', help='Name for create command')
    args = parser.parse_args(argv)

    if args.command == 'create':
        if not args.name:
            print('Please provide a name for the migration: python db_migrate.py create add_column')
            sys.exit(2)
        cmd_create(args.name)
        return

    conn = get_conn(args.db)
    try:
        if args.command == 'status':
            ensure_migrations_table(conn)
            cmd_status(conn)
        elif args.command == 'apply':
            cmd_apply(conn)
    finally:
        conn.close()


if __name__ == '__main__':
    main(sys.argv[1:])
