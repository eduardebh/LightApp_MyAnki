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
			"  python scripts\\check_ipa_dots_db.py\n\n"
			"O agrega la variable en el archivo .env en la raíz del repo."
		)
	return value


def main() -> None:
	# Load repo .env if present
	word_adder._load_dotenv_if_present()

	db_url = _require_env("DATABASE_URL")

	list_id = 22
	limit = 50

	if len(sys.argv) >= 2 and sys.argv[1].strip():
		list_id = int(sys.argv[1].strip())
	if len(sys.argv) >= 3 and sys.argv[2].strip():
		limit = int(sys.argv[2].strip())

	try:
		import psycopg2  # type: ignore
	except Exception as e:
		raise SystemExit(
			"Falta psycopg2. Instálalo (idealmente en tu venv):\n"
			"  pip install psycopg2-binary\n\n"
			f"Detalle: {e}"
		)

	print(f"Connecting DB (list_id={list_id}) …")
	conn = psycopg2.connect(db_url)
	try:
		with conn.cursor() as cur:
			# We avoid LIKE with % on purpose (psycopg2 uses % for interpolation).
			cur.execute(
				"""
				SELECT
					COUNT(*) FILTER (WHERE "IPA_word" IS NULL OR btrim("IPA_word") = '') AS empty_ipa,
					COUNT(*) FILTER (WHERE "IPA_word" IS NOT NULL AND "IPA_word" !~ '^/.*?/$') AS not_slash_wrapped,
					COUNT(*) FILTER (WHERE "IPA_word" IS NOT NULL AND strpos("IPA_word", '..') > 0) AS double_dot,
					COUNT(*) FILTER (WHERE "IPA_word" IS NOT NULL AND (strpos("IPA_word", '.‿') > 0 OR strpos("IPA_word", '‿.') > 0)) AS dot_next_to_liaison,
					COUNT(*) FILTER (WHERE "IPA_word" IS NOT NULL AND "IPA_word" ~ '^/[^/]*[[:space:]]+[^/]*$') AS has_spaces
				FROM words
				WHERE list_id = %s;
				""".strip(),
				(list_id,),
			)
			(
				empty_ipa,
				not_slash_wrapped,
				double_dot,
				dot_next_to_liaison,
				has_spaces,
			) = cur.fetchone()

			print("\nSQL Counts (basic integrity):")
			print(f"- empty_ipa: {empty_ipa}")
			print(f"- not_slash_wrapped: {not_slash_wrapped}")
			print(f"- double_dot ('..'): {double_dot}")
			print(f"- dot_next_to_liaison ('.‿' or '‿.'): {dot_next_to_liaison}")
			print(f"- has_spaces (inside IPA): {has_spaces}")

			cur.execute(
				"""
				SELECT word, association, "IPA_word"
				FROM words
				WHERE list_id = %s
				ORDER BY word;
				""".strip(),
				(list_id,),
			)
			all_rows = cur.fetchall()

			vowel_bases = set(
				"aeiouyAEIOUY"
				"ɑɛɔøœə"
				"ãẽĩõũ"
				"ɑɛɔœ"
			)

			def ipa_body(ipa: str) -> str:
				ipa = (ipa or "").strip()
				if ipa.startswith("/") and ipa.endswith("/") and len(ipa) >= 2:
					return ipa[1:-1]
				return ipa

			def vowel_nuclei_count(body: str) -> int:
				# Very rough heuristic: count vowel base characters; ignore combining diacritics.
				count = 0
				for ch in body:
					if ch in vowel_bases:
						count += 1
				return count

			categories: dict[str, list[tuple[str, str, str]]] = {
				"missing_dot_for_multi_vowel": [],
				"dot_but_single_vowel": [],
				"dot_count_ge_vowel_count": [],
				"dot_at_edges": [],
				"double_dot": [],
				"dot_next_to_liaison": [],
				"has_spaces": [],
				"not_slash_wrapped": [],
			}

			for w, assoc, ipa in all_rows:
				ipa_s = (ipa or "").strip() if ipa is not None else ""
				assoc_s = (assoc or "").strip() if assoc is not None else ""
				body = ipa_body(ipa_s)
				vowels = vowel_nuclei_count(body)
				dots = body.count(".")
				if not ipa_s or not body:
					continue
				if ipa_s.startswith("/") != ipa_s.endswith("/") or (ipa_s and not (ipa_s.startswith("/") and ipa_s.endswith("/"))):
					# Keep this aligned with the SQL check for visibility.
					if not (ipa_s.startswith("/") and ipa_s.endswith("/")):
						categories["not_slash_wrapped"].append((w, ipa_s, assoc_s))
				if " " in body or "\t" in body or "\n" in body:
					categories["has_spaces"].append((w, ipa_s, assoc_s))
				if ".." in body:
					categories["double_dot"].append((w, ipa_s, assoc_s))
				if ".‿" in body or "‿." in body:
					categories["dot_next_to_liaison"].append((w, ipa_s, assoc_s))
				if body.startswith(".") or body.endswith("."):
					categories["dot_at_edges"].append((w, ipa_s, assoc_s))
				if vowels >= 2 and dots == 0:
					categories["missing_dot_for_multi_vowel"].append((w, ipa_s, assoc_s))
				if dots > 0 and vowels <= 1:
					categories["dot_but_single_vowel"].append((w, ipa_s, assoc_s))
				if vowels > 0 and dots >= vowels:
					categories["dot_count_ge_vowel_count"].append((w, ipa_s, assoc_s))

			print("\nHeuristic Dot Checks (syllable-ish):")
			for key in [
				"missing_dot_for_multi_vowel",
				"dot_but_single_vowel",
				"dot_count_ge_vowel_count",
				"dot_at_edges",
				"double_dot",
				"dot_next_to_liaison",
				"has_spaces",
				"not_slash_wrapped",
			]:
				print(f"- {key}: {len(categories[key])}")

			def show_examples(key: str) -> None:
				examples = categories[key][:limit]
				print(f"\nExamples for {key} (up to {limit}):")
				if not examples:
					print("- (none)")
					return
				for w, ipa_s, assoc_s in examples:
					print(f"- {w} | {ipa_s} | {assoc_s}")

			# Show only the categories that have hits, to keep output readable.
			for key in [
				"missing_dot_for_multi_vowel",
				"dot_but_single_vowel",
				"dot_count_ge_vowel_count",
				"dot_at_edges",
				"double_dot",
				"dot_next_to_liaison",
				"has_spaces",
				"not_slash_wrapped",
			]:
				if categories[key]:
					show_examples(key)

		print("\nDone.")
	finally:
		conn.close()


if __name__ == "__main__":
	main()
