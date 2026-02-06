from __future__ import annotations

import os
import sys
from pathlib import Path


# Allow running this script directly from `scripts/` without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from frequency_db_utils import word_adder  # noqa: E402


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (avoids external deps).

    Supports simple KEY=VALUE lines, with optional single/double quotes.
    Ignores blank lines and comments starting with '#'.
    """
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            key, value = s.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except Exception:
        return


def read_list_test(path: Path):
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.split(",")]
            word = parts[0]
            lang = parts[1] if len(parts) > 1 and parts[1] else "fr"
            list_id = int(parts[2]) if len(parts) > 2 and parts[2] else 1
            entries.append((word, lang, list_id))
    return entries


def main():
    list_path = REPO_ROOT / "scripts" / "List_test.txt"

    # Load repo .env if present.
    _load_env_file(REPO_ROOT / ".env")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY no está configurada.\n"
            "Configúrala y vuelve a ejecutar:\n"
            "  $env:OPENAI_API_KEY=\"...\"\n"
            "  python scripts\\run_list_test_dry.py\n\n"
            "O agrega OPENAI_API_KEY en el archivo .env en la raíz del repo."
        )

    entries = read_list_test(list_path)
    if not entries:
        print(f"No entries found in {list_path}")
        return

    print(f"Running List_test with real API calls for {len(entries)} entries from {list_path}...\n")

    for palabra, language, list_id in entries:
        result = word_adder.prepare_word_actions(
            palabra=palabra,
            list_id=list_id,
            language=language,
            openai_api_key=api_key,
            user_id=os.getenv("MYTOOLS_USER_ID", "local").strip() or "local",
        )
        print(f"- {palabra} ({language}, list_id={list_id})")
        print(f"  association: {result.get('association')}")
        print(f"  ipa_word:     {result.get('ipa_word')}")
        print(f"  queries:      {len(result.get('queries', []))}")
        print()


if __name__ == "__main__":
    main()
