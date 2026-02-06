#!/usr/bin/env python3
"""
Database backup helper.

Behavior:
- Prefer to run `pg_dump` (if available in PATH). Produces a single dump file.
- If `pg_dump` is not available, falls back to exporting each user table as CSV (compressed)
  and writes a simple manifest SQL that recreates basic table columns.

Usage examples:
  python db_backup.py --out backups/mydump.dump
  python db_backup.py --db "postgresql://..." apply

Warning: do not run against production without backups and review.
"""
import os
import sys
import argparse
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
import gzip


def which_pg_dump():
    return shutil.which('pg_dump')


def default_out_path(format_ext='sql'):
    now = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    p = Path('backups')
    p.mkdir(exist_ok=True)
    return p / f'backup_{now}.{format_ext}'


def run_pg_dump(db_url, out: Path, fmt='custom'):
    pg_dump = which_pg_dump()
    if not pg_dump:
        raise FileNotFoundError('pg_dump not found in PATH')
    # Use --dbname to pass connection string safely
    args = [pg_dump, f'--dbname={db_url}', '--no-owner', '--no-privileges', '-F', fmt[0], '-f', str(out)]
    print('Running:', ' '.join(args))
    subprocess.run(args, check=True)


def fallback_csv_dump(db_url, out_dir: Path):
    # import psycopg2 lazily so the script can run `pg_dump` without needing psycopg2 installed
    try:
        import psycopg2
    except Exception as e:
        raise
    print('pg_dump not available — falling back to CSV per-table export')
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    # list user tables (exclude pg_* and information_schema)
    cur.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type='BASE TABLE'
          AND table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name
    """)
    tables = cur.fetchall()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for schema, table in tables:
        full = f'{schema}.{table}' if schema != 'public' else table
        csv_path = out_dir / f"{schema}__{table}.csv.gz"
        print('Exporting', full, '->', csv_path)
        with gzip.open(csv_path, 'wt', encoding='utf-8') as f:
            # Use COPY from a SELECT to support partitioned tables
            sql = f"COPY (SELECT * FROM {schema}.{table}) TO STDOUT WITH CSV HEADER"
            cur.copy_expert(sql, f)
        # basic columns meta
        cur2 = conn.cursor()
        cur2.execute("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
        """, (schema, table))
        cols = cur2.fetchall()
        manifest.append({'table': full, 'columns': cols, 'csv': str(csv_path)})
        cur2.close()
    conn.close()
    # write manifest SQL
    manifest_sql = out_dir / 'manifest_basic.sql'
    with open(manifest_sql, 'w', encoding='utf-8') as mf:
        mf.write('-- Basic manifest: recreates tables without constraints/indices\n')
        for m in manifest:
            mf.write(f"-- Table: {m['table']}\n")
            cols_sql = []
            for c in m['columns']:
                colname, dtype, nullable, default = c
                # basic mapping: use dtype as-is
                null_sql = ' NOT NULL' if nullable == 'NO' else ''
                default_sql = f' DEFAULT {default}' if default else ''
                cols_sql.append(f'    "{colname}" {dtype}{default_sql}{null_sql}')
            mf.write(f"CREATE TABLE {m['table']} (\n")
            mf.write(',\n'.join(cols_sql))
            mf.write('\n);\n\n')
        mf.write('-- To restore data, use COPY FROM for each CSV file.\n')
    print('Fallback CSV export complete. Files in', out_dir)


def main(argv):
    parser = argparse.ArgumentParser(description='DB backup helper')
    parser.add_argument('--db', help='Database URL (overrides DATABASE_URL env)')
    parser.add_argument('--out', help='Output file or directory')
    parser.add_argument('--format', choices=['plain', 'custom'], default='custom', help='pg_dump format')
    args = parser.parse_args(argv)

    db_url = args.db or os.environ.get('DATABASE_URL')
    if not db_url:
        print('ERROR: DATABASE_URL not provided via --db or env')
        sys.exit(2)

    pg_dump = which_pg_dump()
    if args.out:
        out = Path(args.out)
    else:
        ext = 'dump' if args.format == 'custom' else 'sql'
        out = default_out_path(ext)

    try:
        if pg_dump:
            print('Found pg_dump at', pg_dump)
            out.parent.mkdir(parents=True, exist_ok=True)
            run_pg_dump(db_url, out, fmt=args.format)
            print('Backup written to', out)
        else:
            # fallback to directory of CSVs
            out_dir = Path(args.out) if args.out else Path('backups') / datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            fallback_csv_dump(db_url, out_dir)
    except subprocess.CalledProcessError as e:
        print('pg_dump failed:', e)
        sys.exit(1)
    except Exception as e:
        print('ERROR during backup:', e)
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv[1:])
