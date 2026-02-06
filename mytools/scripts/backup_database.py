from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse


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
			"  python scripts\\backup_database.py\n\n"
			"O agrega la variable en el archivo .env en la raíz del repo."
		)
	return value


def _sanitize_db_url(db_url: str) -> str:
	"""Return a safe-to-print version of a Postgres URL (mask password)."""
	try:
		p = urlparse(db_url)
		if p.username and p.password:
			netloc = f"{p.username}:***@{p.hostname or ''}"
			if p.port:
				netloc += f":{p.port}"
			return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
		return db_url
	except Exception:
		return "[unparseable DATABASE_URL]"


def _infer_db_name(db_url: str) -> str:
	try:
		p = urlparse(db_url)
		db = (p.path or "").lstrip("/").strip()
		return db or "postgres"
	except Exception:
		return "postgres"


def _find_pg_dump() -> str | None:
	# 1) explicit override
	override = os.getenv("PG_DUMP", "").strip()
	if override:
		p = Path(override)
		if p.exists():
			return str(p)

	# 2) PATH
	for name in ("pg_dump", "pg_dump.exe"):
		found = shutil.which(name)
		if found:
			return found

	# 3) common Windows install locations
	candidates: list[Path] = []
	for base in (
		Path(r"C:\\Program Files\\PostgreSQL"),
		Path(r"C:\\Program Files (x86)\\PostgreSQL"),
	):
		if not base.exists():
			continue
		for exe in base.glob("*\\bin\\pg_dump.exe"):
			candidates.append(exe)
	if not candidates:
		return None
	# Prefer the highest version folder name if possible
	candidates.sort(key=lambda p: p.as_posix())
	return str(candidates[-1])


def _default_output_path(out_dir: Path, db_name: str, fmt: str) -> Path:
	ts = datetime.now().strftime("%Y%m%d_%H%M%S")
	stem = f"{db_name}_{ts}"
	if fmt == "plain":
		return out_dir / f"{stem}.sql"
	if fmt == "tar":
		return out_dir / f"{stem}.tar"
	if fmt == "directory":
		return out_dir / stem
	# custom
	return out_dir / f"{stem}.dump"


def main() -> None:
	parser = argparse.ArgumentParser(
		description="Backup de la base de datos Postgres vía pg_dump (usa DATABASE_URL)."
	)
	parser.add_argument(
		"--database-url",
		dest="database_url",
		default=None,
		help="Override de DATABASE_URL (si no, usa la variable de entorno).",
	)
	parser.add_argument(
		"--out-dir",
		dest="out_dir",
		default=str(REPO_ROOT / "backups"),
		help="Directorio destino (default: ./backups)",
	)
	parser.add_argument(
		"--out",
		dest="out_path",
		default=None,
		help="Ruta de salida exacta (archivo o carpeta si format=directory).",
	)
	parser.add_argument(
		"--format",
		dest="fmt",
		choices=["custom", "plain", "tar", "directory"],
		default="custom",
		help="Formato de pg_dump (default: custom).",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="No ejecuta pg_dump; solo muestra lo que haría.",
	)
	args = parser.parse_args()

	# Load repo .env if present (for DATABASE_URL)
	word_adder._load_dotenv_if_present()

	db_url = (args.database_url or os.getenv("DATABASE_URL", "")).strip()
	if not db_url:
		db_url = _require_env("DATABASE_URL")

	pg_dump = _find_pg_dump()
	if not pg_dump:
		raise SystemExit(
			"No encuentro pg_dump.\n\n"
			"Opciones:\n"
			"- Instala PostgreSQL (incluye pg_dump) y asegúrate de que esté en PATH\n"
			"- O configura la variable de entorno PG_DUMP con la ruta a pg_dump.exe\n"
			"\nEjemplo:\n  $env:PG_DUMP=\"C:\\\\Program Files\\\\PostgreSQL\\\\16\\\\bin\\\\pg_dump.exe\""
		)

	out_dir = Path(args.out_dir).expanduser().resolve()
	out_dir.mkdir(parents=True, exist_ok=True)

	db_name = _infer_db_name(db_url)
	out_path = Path(args.out_path).expanduser().resolve() if args.out_path else _default_output_path(out_dir, db_name, args.fmt)

	# Prepare pg_dump command
	cmd: list[str] = [
		pg_dump,
		"--no-owner",
		"--no-privileges",
		"--format",
		args.fmt,
		"--file",
		str(out_path),
		"--dbname",
		db_url,
	]

	print("Backup Postgres (pg_dump)")
	print(f"- DB: {_sanitize_db_url(db_url)}")
	print(f"- Format: {args.fmt}")
	print(f"- Output: {out_path}")

	if args.dry_run:
		print("\nDRY RUN: pg_dump se ejecutaría (comando oculto por seguridad).")
		return

	try:
		# Avoid echoing the full command (it may contain credentials)
		completed = subprocess.run(cmd, check=False)
	except FileNotFoundError:
		raise SystemExit(f"pg_dump no existe en: {pg_dump}")

	if completed.returncode != 0:
		raise SystemExit(f"pg_dump falló (exit={completed.returncode}).")

	if args.fmt == "directory":
		print("\nOK. Backup creado (formato directory).")
		return

	try:
		size = out_path.stat().st_size
		size_mb = size / (1024 * 1024)
		print(f"\nOK. Backup creado ({size_mb:.1f} MB).")
	except Exception:
		print("\nOK. Backup creado.")


if __name__ == "__main__":
	main()
