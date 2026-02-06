"""Add/prepare words for the frequency DB.

This module implements the simplified two-call flow:
1) Ask the API for translation (max 3 words) and the article (if noun).
2) Ask the API for the IPA of "article + word" (including liaison/contracciones).

`prepare_word_actions` performs the network calls but returns SQL statements
without executing them.

`add_word` executes the prepared SQL statements using the provided DB cursor.
"""

from __future__ import annotations

import json
import os
import re
import sys
import string
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


_DOTENV_LOADED = False


# Prompt-only reference examples (user-provided).
# This is NOT local IPA post-processing; it's only injected in the prompt.
_FR_IPA_REFERENCE_EXAMPLES = """Ejemplos de referencia (si coincide EXACTAMENTE, usa el mismo IPA):
| Pronombre | ÊTRE (présent) | AVOIR (présent) | ALLER (présent) | AVOIR (passé composé) | ALLER (passé composé) |
| --- | --- | --- | --- | --- | --- |
| je | /ʒə sɥi/ | /ʒe/ | /ʒə vɛ/ | /ʒe‿y/ | /ʒə sɥi‿a.le/ |
| tu | /ty ɛ/ | /ty a/ | /ty va/ | /ty a‿y/ | /ty ɛ‿a.le/ |
| il | /il ɛ/ | /il a/ | /il va/ | /il a‿y/ | /il ɛ‿a.le/ |
| elle | /ɛl ɛ/ | /ɛl a/ | /ɛl va/ | /ɛl a‿y/ | /ɛl ɛ‿a.le/ |
| on | /ɔ̃ ɛ/ | /ɔ̃ a/ | /ɔ̃ va/ | /ɔ̃ a‿y/ | /ɔ̃ ɛ‿a.le/ |
| nous | /nu sɔm/ | /nu z‿avɔ̃/ | /nu z‿alɔ̃/ | /nu z‿avɔ̃‿y/ | /nu sɔm‿a.le/ |
| vous | /vu z‿ɛt/ | /vu z‿ave/ | /vu z‿ale/ | /vu z‿ave‿y/ | /vu z‿ɛt‿a.le/ |
| ils | /il sɔ̃/ | /il z‿ɔ̃/ | /il vɔ̃/ | /il z‿ɔ̃‿t‿y/ | /il sɔ̃‿t‿a.le/ |
| elles | /ɛl sɔ̃/ | /ɛl z‿ɔ̃/ | /ɛl vɔ̃/ | /ɛl z‿ɔ̃‿t‿y/ | /ɛl sɔ̃‿t‿a.le/ |
"""


_UNICODE_APOSTROPHES = {
	"\u2019",  # ’
	"\u02bc",  # ʼ
	"\u2032",  # ′
	"\u2018",  # ‘
}


def _normalize_apostrophes(text: str) -> str:
	"""Normalize apostrophes to ASCII `'` to reduce duplicates."""
	s = text or ""
	for ch in _UNICODE_APOSTROPHES:
		s = s.replace(ch, "'")
	return s


def _strip_edge_punctuation(text: str) -> str:
	"""Strip punctuation at the edges but keep internal apostrophes/hyphens."""
	if not isinstance(text, str):
		return ""
	s = text.strip()
	if not s:
		return ""
	# Note: do NOT strip ASCII apostrophe here; it's meaningful in FR.
	strip_chars = (
		string.punctuation.replace("'", "")
		+ "“”‘’«»¿¡…–—·"
		+ "\t\n\r"
	)
	return s.strip(strip_chars)


def _normalize_user_text(language: str, text: str) -> str:
	"""Normalize user-provided text to avoid duplicate DB entries.

	Goals:
	- treat trailing/leading punctuation differences as the same entry
	- normalize curly apostrophes
	"""
	s = _normalize_apostrophes(text or "")
	s = _strip_edge_punctuation(s)
	# Collapse whitespace.
	s = re.sub(r"\s+", " ", s).strip()
	if (language or "").lower() in {"fr", "de"}:
		s = s.lower()
	return s


def _fr_drop_contraction_prefix(token: str) -> str:
	"""Drop FR contractions that should not count as different 'words'.

	We treat tokens like d'erreurs / l'erreur as the underlying word (erreurs/erreur)
	for token-level insertion. This avoids creating separate entries for contracted
	forms.

	We intentionally keep this conservative (only d' and l').
	"""
	w = (token or "").strip()
	if not w:
		return ""
	m = re.match(r"^(d|l)'(.+)$", w, flags=re.IGNORECASE)
	if not m:
		return w
	rhs = (m.group(2) or "").strip()
	return rhs or w


def _normalize_phrase_for_tts_storage(language: str, phrase: str) -> str:
	"""Normalize a phrase for TTS-friendly storage.

	Goal: avoid screen readers / TTS reading punctuation names like "punto" or
	"apostrofe".

	Important:
	- This affects ONLY the stored full-phrase entry (the `word`/association phrase).
	- IPA requests still use the normal phrase with apostrophes preserved.

	Rules:
	- Normalize apostrophes and strip edge punctuation first.
	- Remove apostrophes entirely (d'erreurs -> derreurs).
	- Replace other punctuation with spaces, then collapse whitespace.
	- Lowercase for FR/DE.
	"""
	lang = (language or "").strip().lower()
	s = _normalize_user_text(lang, phrase or "")
	if not s:
		return ""
	# Remove apostrophes (TTS often reads them).
	s = s.replace("'", "")
	# Replace remaining punctuation with spaces.
	# Keep hyphen as space too (some TTS reads it as 'guion').
	punct = string.punctuation.replace("'", "") + "“”‘’«»¿¡…–—·"
	trans = str.maketrans({ch: " " for ch in punct})
	s = s.translate(trans)
	# Collapse whitespace.
	s = re.sub(r"\s+", " ", s).strip()
	if lang in {"fr", "de"}:
		s = s.lower()
	return s


def _load_dotenv_if_present() -> None:
	"""Load environment variables from a repo-local .env file.

	This avoids requiring users to manually export OPENAI_API_KEY in each shell.
	We intentionally implement a tiny parser instead of adding a dependency.
	"""

	global _DOTENV_LOADED
	if _DOTENV_LOADED:
		return

	repo_root = Path(__file__).resolve().parents[1]
	dotenv_path = repo_root / ".env"
	if not dotenv_path.exists():
		_DOTENV_LOADED = True
		return

	try:
		for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#"):
				continue
			if "=" not in line:
				continue
			key, value = line.split("=", 1)
			key = key.strip()
			value = value.strip()
			if not key:
				continue
			# Strip surrounding quotes if present.
			if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
				value = value[1:-1]
			os.environ.setdefault(key, value)
	except Exception:
		# Best-effort; don't fail module import if .env is malformed.
		pass
	finally:
		_DOTENV_LOADED = True


def _resolve_rejected_words_config(
	*,
	user_id: Optional[Any],
	rejected_words_table: Optional[str],
	language: str,
) -> tuple[Optional[Any], Optional[str], str]:
	"""Resolve rejected-words guard configuration.

	This guard is intended to be enforced at DB-write time, where the caller
	already has access to `user_id` and `language`.

	- `user_id` is required; if missing we raise to avoid bypassing.
	- If `rejected_words_table` is not provided, we default to
	  the production table name `palabras_rechazadas`.
	- We normalize language to lowercase for the DB lookup.
	"""
	lang_norm = (language or "").strip().lower()
	if user_id is None:
		raise ValueError("user_id is required to enforce rejected-words validation")

	table = (rejected_words_table or "").strip() if rejected_words_table is not None else ""
	if not table:
		table = "palabras_rechazadas"

	return user_id, table, lang_norm


def _call_openai_chat(
	api_key: str,
	messages: List[Dict[str, str]],
	max_tokens: int = 120,
	timeout: int = 30,
	response_format: Optional[Dict[str, Any]] = None,
):
	url = "https://api.openai.com/v1/chat/completions"
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	primary_model = os.getenv("OPENAI_MODEL", "gpt-4o")
	fallback_model = "gpt-3.5-turbo"
	models_to_try = [primary_model]
	if fallback_model not in models_to_try:
		models_to_try.append(fallback_model)

	last_error: Optional[Exception] = None
	for model in models_to_try:
		payload: Dict[str, Any] = {
			"model": model,
			"messages": messages,
			"max_tokens": max_tokens,
			"temperature": 0,
		}
		if response_format:
			payload["response_format"] = response_format

		resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
		if resp.status_code >= 400:
			# If the chosen model is unavailable for this key, retry with fallback.
			try:
				err = resp.json().get("error", {})
				msg = str(err.get("message", ""))
			except Exception:
				msg = resp.text

			# Some models/keys don't support response_format on chat completions.
			# Retry once without it.
			if response_format and any(s in msg.lower() for s in ["response_format", "unsupported", "unrecognized"]):
				payload.pop("response_format", None)
				resp2 = requests.post(url, headers=headers, json=payload, timeout=timeout)
				if resp2.status_code < 400:
					return resp2.json()
				resp = resp2
				try:
					err2 = resp.json().get("error", {})
					msg = str(err2.get("message", ""))
				except Exception:
					msg = resp.text

			if model != fallback_model and any(s in msg.lower() for s in ["model", "not found", "does not exist", "unknown"]):
				last_error = RuntimeError(f"OpenAI model '{model}' unavailable; retrying with fallback")
				continue
			resp.raise_for_status()
		return resp.json()

	if last_error:
		raise last_error
	raise RuntimeError("OpenAI request failed")


def _extract_json_from_text(raw: str) -> Optional[Dict[str, Any]]:
	try:
		start = raw.index("{")
		end = raw.rindex("}")
		return json.loads(raw[start : end + 1])
	except Exception:
		return None


def _normalize_ipa_wrapping(ipa: str) -> Optional[str]:
	"""Return IPA only if it is already exactly wrapped as /.../.

	User requirement: no local post-processing or heuristics.
	So we accept only the model's exact /.../ output and otherwise return None.
	"""
	raw = (ipa or "").strip()
	if not raw:
		return None
	# Accept only a single IPA segment wrapped in slashes with no extra text.
	if re.fullmatch(r"/[^/]+/", raw):
		inside = raw[1:-1]
		# Reject bracketed IPA like /[l‿a.mi]/ (no normalization allowed).
		if "[" in inside or "]" in inside:
			return None
		return raw
	return None


def _ipa_is_reasonably_well_formed(ipa: Optional[str]) -> bool:
	"""Basic sanity check for IPA format.

	We cannot guarantee phonetic correctness, but we can enforce:
	- wrapped as /.../
	- non-empty content
	- no raw '//' sequences
	- no obvious malformed liaison marker patterns (e.g. spaces around '‿', '‿‿', or '‿z‿')
	"""
	if not ipa:
		return False
	s = ipa.strip()
	if "//" in s:
		return False
	if not (s.startswith("/") and s.endswith("/")):
		return False
	inside = s[1:-1]
	inside = inside.strip()
	if not inside:
		return False
	# Avoid newlines/tabs in stored IPA.
	if any(ch in inside for ch in ("\n", "\r", "\t")):
		return False
	# If the liaison marker appears, it must not be surrounded by spaces.
	if " \u203f" in inside or "\u203f " in inside:
		return False
	# Avoid obviously broken marker repetition.
	if "\u203f\u203f" in inside:
		return False
	return True


def _ipa_is_reasonably_well_formed_for_phrase(language: str, phrase: str, ipa: Optional[str]) -> bool:
	"""Phrase-aware IPA sanity check.

	Goal: catch structurally broken outputs (especially for FR liaison segmentation)
	without trying to "fix" IPA locally.

	For French multi-word phrases, enforce that word-boundary segmentation in IPA
	(space or '‿') produces exactly as many segments as there are tokens in the
	original phrase. This rejects patterns like:
	- /nu‿z‿avɔ̃/ (extra segment 'z')
	- /vu‿z‿ɛt/ (extra segment 'z')
	- /ɛl z‿ɔ̃ te/ (extra segment 'z')
	"""
	if not _ipa_is_reasonably_well_formed(ipa):
		return False
	lang = (language or "").lower().strip()
	if lang != "fr":
		return True

	tokens = _tokenize_phrase(lang, phrase or "")
	if not tokens:
		return True

	# Only enforce segmentation rules for multi-word phrases.
	if len(tokens) <= 1:
		return True

	inside = (ipa or "").strip()[1:-1].strip()
	# Split on either spaces or liaison markers.
	segments = [seg for seg in re.split(r"[ \u203f]+", inside) if seg]
	return len(segments) == len(tokens)


def _simple_ipa_from_text(content: str) -> Optional[str]:
	"""Accept only the model's exact IPA format: /.../.

User requirement: no local post-processing/rewrites. We only strip outer
whitespace and accept the IPA if it is already exactly wrapped as a single
/.../ segment.
	"""
	raw = (content or "").strip()
	return _normalize_ipa_wrapping(raw)


def _build_phrase(article: str, palabra: str) -> str:
	article = (article or "").strip("\n")
	if not article:
		return palabra
	if article.endswith("'") or article.endswith("’"):
		return f"{article}{palabra}"
	if article.endswith(" ") or article.endswith("\t"):
		return f"{article}{palabra}"
	return f"{article} {palabra}"


def _starts_with_french_elision_sound(word: str) -> bool:
	# Best-effort: vowels + common accented vowels + 'h'.
	first = (word or "").strip().lower()[:1]
	return first in "aeiouyàâäéèêëîïôöùûüœæh"


def _accept_phrase_correction(language: str, original_phrase: str, original_article: str, palabra: str, candidate: str) -> bool:
	"""Allow only safe phrase corrections.

	We only allow:
	- No change, OR
	- For FR: changing "le <word>" / "la <word>" to "l'<word>" when elision applies.
	"""
	if not isinstance(candidate, str):
		return False
	cand = candidate.strip()
	if not cand:
		return False
	if cand == original_phrase:
		return True
	if (language or "").lower() != "fr":
		return False

	orig = (original_article or "").strip().lower()
	# Only allow elision correction (le/la -> l')
	if orig not in {"le", "la", "le ", "la "}:
		return False
	if not _starts_with_french_elision_sound(palabra):
		return False
	if cand in {f"l'{palabra}", f"l’{palabra}"}:
		return True
	return False


def _normalize_article(article: str, language: str) -> str:
	"""Validate/normalize the article for the source language.

	We only accept articles that make sense for the *original* language (FR/DE).
	If the model returns a Spanish article (e.g. "el"), we drop it to avoid
	polluting the stored phrase.
	"""

	if not article:
		return ""
	raw = article.strip()
	if not raw:
		return ""

	lang = (language or "").lower()
	# Keep original spacing preference: callers may provide trailing space.
	has_trailing_space = isinstance(article, str) and article.endswith(" ")

	if lang == "fr":
		# We store a noun phrase with the definite article (or elision l').
		# Do NOT accept indefinite articles here.
		allowed = {"le", "la", "les", "l'", "l’"}
	elif lang == "de":
		allowed = {"der", "die", "das", "ein", "eine"}
	else:
		# Unknown language: best-effort, accept as-is.
		return article

	lowered = raw.lower()
	if lowered not in allowed:
		return ""

	if lowered in {"l'", "l’"}:
		# French elision: no trailing space.
		return lowered
	return f"{lowered} " if has_trailing_space or len(raw) == len(lowered) else f"{raw} "


def _normalize_fr_gender(value: Any) -> str:
	"""Normalize French grammatical gender markers.

	We only accept 'm' or 'f'. Anything else becomes ''.
	"""
	if value is None:
		return ""
	raw = str(value).strip().lower()
	if not raw:
		return ""
	if raw in {"m", "masc", "masculin", "masculine"}:
		return "m"
	if raw in {"f", "fem", "féminin", "feminine", "femenin", "femenino"}:
		return "f"
	return ""


def _maybe_append_fr_gender_marker(language: str, phrase: str, gender: str) -> str:
	"""Append gender marker to stored French elided noun phrases.

	Requirement: when the phrase uses the contracted definite article l', store it as:
	- l'<noun> (m) or l'<noun> (f)

	We do NOT modify the phrase used for IPA requests.
	"""
	if (language or "").strip().lower() != "fr":
		return (phrase or "").strip()
	p = (phrase or "").strip()
	if not p:
		return p
	low = p.lower()
	if not (low.startswith("l'") or low.startswith("l’")):
		return p
	marker = _normalize_fr_gender(gender)
	if marker not in {"m", "f"}:
		return p
	if re.search(r"\s+\([mf]\)\s*$", p, flags=re.IGNORECASE):
		return p
	return f"{p} ({marker})"


def _normalize_fr_base_article(value: Any) -> str:
	"""Normalize the underlying definite article for French nouns.

	Accepted values: le, la, les (optionally with trailing space).
	Used to infer gender when the displayed article is elided (l').
	"""
	if value is None:
		return ""
	raw = str(value).strip().lower()
	if raw in {"le", "la", "les"}:
		return raw
	return ""


def _normalize_spanish_noun_translation(translation: str) -> str:
	"""Normalize Spanish noun translations.

	Rule: we store translations without determiners/articles (e.g. 'un amigo' -> 'amigo').
	This keeps associations consistent with the existing convention: "l'ami, amigo".
	"""
	text = (translation or "").strip()
	if not text:
		return ""
	# Associations use comma as a separator, so translations must never contain
	# comma-separated extra notes like: "error, d'erreurs".
	text = re.split(r"\s*[,;|]\s*", text, maxsplit=1)[0].strip()
	# Drop trailing parentheticals like "(infinitivo)" or "(algo)" when returned
	# unexpectedly by the model.
	text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
	# Strip wrapping quotes.
	text = text.strip("\"'“”‘’")
	# Remove common leading determiners.
	# Keep it conservative; only strip at the start.
	text = re.sub(r"^(un|una|unos|unas|el|la|los|las)\s+", "", text, flags=re.IGNORECASE).strip()
	# Final edge cleanup.
	text = _strip_edge_punctuation(text)
	text = re.sub(r"\s+", " ", text).strip()
	return text


def _normalize_fr_pronoun(pronoun: str) -> str:
	"""Normalize French subject pronouns.

	We keep it intentionally conservative; this is only used for building phrases
	for verb entries.
	"""
	raw = (pronoun or "").strip().lower()
	if not raw:
		return ""
	aliases = {
		"j": "je",
		"j'": "je",
		"j’": "je",
		"je": "je",
		"tu": "tu",
		"il": "il",
		"elle": "elle",
		"on": "on",
		"nous": "nous",
		"vous": "vous",
		"ils": "ils",
		"elles": "elles",
	}
	return aliases.get(raw, raw)


def _build_verb_phrase(language: str, pronoun: str, verb_form: str) -> str:
	pronoun_norm = (pronoun or "").strip()
	verb_form = (verb_form or "").strip()
	if not pronoun_norm:
		return verb_form
	if not verb_form:
		return pronoun_norm

	lang = (language or "").lower()
	if lang == "fr":
		p = _normalize_fr_pronoun(pronoun_norm)
		# Elision for je before vowel/h.
		if p == "je" and _starts_with_french_elision_sound(verb_form):
			return f"j'{verb_form}"
		return f"{p} {verb_form}"

	# Default: just join with a space.
	return f"{pronoun_norm} {verb_form}"


def _accept_verb_phrase_correction(language: str, original_phrase: str, candidate: str) -> bool:
	"""Allow only safe phrase corrections for verb phrases.

	For now we accept:
	- No change, OR
	- FR: je <verb> -> j'<verb> (or with typographic apostrophe) when elision applies.
	"""
	if not isinstance(candidate, str):
		return False
	cand = candidate.strip()
	if not cand:
		return False
	if cand == (original_phrase or "").strip():
		return True
	if (language or "").lower() != "fr":
		return False
	orig = (original_phrase or "").strip()
	# Only allow je -> j' elision
	if orig.lower().startswith("je "):
		rest = orig[3:].strip()
		if _starts_with_french_elision_sound(rest) and cand in {f"j'{rest}", f"j’{rest}"}:
			return True
	return False


def _group_ambiguous_pronouns(language: str, verb_entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
	"""Group ambiguous subject pronoun variants into a single entry.

	Requested groupings:
	- FR: il/elle/on, ils/elles
	- DE: er/sie/man

	We only group when the remaining verb form text is identical.
	"""
	lang = (language or "").lower()
	if lang not in {"fr", "de"} or not verb_entries:
		return verb_entries

	if lang == "fr":
		group_map = {
			"il": "il/elle/on",
			"elle": "il/elle/on",
			"on": "il/elle/on",
			"ils": "ils/elles",
			"elles": "ils/elles",
		}
	elif lang == "de":
		group_map = {
			"er": "er/sie/man",
			"sie": "er/sie/man",
			"man": "er/sie/man",
		}
	else:
		group_map = {}

	# Preserve order; merge into first-seen entry.
	merged: Dict[tuple[str, str], Dict[str, str]] = {}
	order: List[tuple[str, str]] = []

	for entry in verb_entries:
		phrase = (entry.get("phrase") or "").strip()
		if not phrase:
			continue
		parts = phrase.split(" ", 1)
		if len(parts) != 2:
			# Can't split pronoun + verbform reliably.
			key = ("", phrase)
			if key not in merged:
				merged[key] = dict(entry)
				order.append(key)
			continue

		pron, rest = parts[0].strip().lower(), parts[1].strip()
		group = group_map.get(pron)
		if not group:
			key = (pron, rest)
			if key not in merged:
				merged[key] = dict(entry)
				order.append(key)
			continue

		key = (group, rest)
		if key not in merged:
			new_entry = dict(entry)
			new_entry["pronoun"] = group
			new_entry["phrase"] = f"{group} {rest}"
			# Clear IPA; we'll regenerate after grouping.
			new_entry.pop("ipa", None)
			merged[key] = new_entry
			order.append(key)
		else:
			# Prefer keeping an existing translation if present.
			if not (merged[key].get("translation") or "").strip():
				merged[key]["translation"] = (entry.get("translation") or "").strip()

	return [merged[k] for k in order]


def _parse_pos_field(pos_value: Any) -> tuple[bool, bool, bool, str]:
	"""Parse POS field from model output.

	Returns: (is_noun, is_verb, is_adjective, normalized_pos)
	normalized_pos is one of: noun, verb, adjective, noun_verb, other, "".
	"""
	tokens: List[str] = []
	if isinstance(pos_value, list):
		for item in pos_value:
			if isinstance(item, str) and item.strip():
				tokens.append(item.strip().lower())
	elif isinstance(pos_value, str):
		raw = pos_value.strip().lower()
		# Normalize common combined formats.
		raw = raw.replace("+", " ").replace("/", " ").replace("_", " ")
		raw = raw.replace("and", " ")
		for part in raw.split():
			if part:
				tokens.append(part)

	is_adjective = any(t in {"adjective", "adj", "adjetivo", "adjetive"} for t in tokens)
	is_noun = any(t in {"noun", "n", "sustantivo", "sustantive"} for t in tokens)
	is_verb = any(t in {"verb", "v", "verbo"} for t in tokens)

	# Explicit combined markers.
	if isinstance(pos_value, str):
		low = pos_value.strip().lower()
		if any(s in low for s in ["noun_verb", "nounverb", "noun verb", "verb noun", "both"]):
			is_noun = True
			is_verb = True

	# If the model indicates adjective, we treat it as adjective even if it can
	# be a verb form in isolation. (User requirement: verb-as-noun/adjective
	# should not be counted as a verb.)
	if is_adjective:
		return False, False, True, "adjective"

	if is_noun and is_verb:
		return True, True, False, "noun_verb"
	if is_noun:
		return True, False, False, "noun"
	if is_verb:
		return False, True, False, "verb"
	if tokens:
		return False, False, False, "other"
	return False, False, False, ""


def _looks_like_fr_conjugated_verb_form(word: str) -> bool:
	"""Heuristic: should we double-check verb-ness?

	Used only as a trigger to run a *second* OpenAI check when the model said
	"noun" but the token looks like a common French verb form (e.g. mange, parle).
	"""
	w = (word or "").strip().lower()
	if not w or " " in w:
		return False
	# Some short but highly ambiguous / frequent verb forms should still be checked.
	if w in {"été"}:
		return True
	# Avoid extremely short tokens.
	if len(w) < 4:
		return False
	# Words ending in -ment are typically adverbs (rapidement) or nouns (gouvernement),
	# not conjugated verb forms. Avoid triggering extra verb-check calls.
	if w.endswith("ment"):
		return False
	# Common present endings for -er verbs and 3p plural.
	common_suffixes = ("e", "es", "ent", "ons", "ez")
	if w.endswith(common_suffixes):
		return True
	# A few very common irregular present forms that are frequently typed.
	if w in {"suis", "es", "est", "sommes", "êtes", "sont", "ai", "as", "a", "avons", "avez", "ont"}:
		return True
	return False


def _is_fr_ambiguous_noun_verb_token(word: str) -> bool:
	"""Return True for tokens that commonly exist as both noun and verb forms."""
	w = (word or "").strip().lower()
	return w in {"été"}


def _is_fr_common_adverb(word: str) -> bool:
	"""Return True for common French adverbs that should not get articles.

	This is a safety override against occasional model misclassification
	(noun/noun_verb) for high-frequency adverbs.
	"""
	w = (word or "").strip().lower()
	# Keep small + high-confidence.
	return w in {
		"tard",
		"tôt",
		"ici",
		"là",
		"hier",
		"demain",
		"aujourd'hui",
		"toujours",
		"jamais",
		"souvent",
		"parfois",
		"déjà",
		"encore",
		"très",
		"trop",
		"assez",
		"bien",
		"mal",
		"vite",
	}


def _fr_default_lemma_for_ambiguous_token(word: str) -> str:
	w = (word or "").strip().lower()
	defaults = {
		"été": "être",
	}
	return defaults.get(w, "")


def _fr_singular_candidate_for_plural_noun(word: str) -> Optional[str]:
	"""Best-effort singular candidate for a French plural noun.

	Rule requirement: if the user tries to add a noun but its singular already
	exists in the list, the noun should not be added.

	We implement a conservative, FR-only heuristic and apply it only to single
	tokens. This is used to generate a DB-time guard (NOT EXISTS).
	"""
	w = (word or "").strip()
	if not w:
		return None
	if re.search(r"\s", w):
		return None
	low = w.lower()
	# bateaux -> bateau
	# We only handle very regular, high-confidence plural patterns.
	# NOTE: We intentionally do NOT try to singularize "-aux" (travaux->travail,
	# chevaux->cheval, etc.) because it's irregular and can be ambiguous.
	if low.endswith("eaux") and len(w) > 4:
		return w[:-1]
	# livres -> livre (most regular plurals)
	if low.endswith("s") and len(w) > 3:
		return w[:-1]
	return None


def _default_feminine_by_suffix(language: str, adjective: str) -> str:
	"""Return the default feminine form by the simple suffix rule.

	- FR: add 'e' (if not already ending in e)
	- ES: add 'a' (if not already ending in a)
	Other languages: return '' (no rule).

	This is used only to decide whether a feminine form is "irregular".
	"""
	lang = (language or "").strip().lower()
	w = (adjective or "").strip()
	if not w:
		return ""
	if lang == "fr":
		return w if w.lower().endswith("e") else f"{w}e"
	if lang == "es":
		return w if w.lower().endswith("a") else f"{w}a"
	return ""


def _get_combined_infinitive_noun_ipa(
	api_key: str,
	language: str,
	infinitive: str,
	noun_phrase: str,
	original_article: str,
	palabra: str,
) -> Optional[str]:
	"""Get IPA for both infinitive and noun phrase in a single call.

	Returns a combined string like: "/.../; /.../" (best-effort).
	"""
	infinitive = (infinitive or "").strip()
	noun_phrase = (noun_phrase or "").strip()
	if not infinitive and not noun_phrase:
		return None

	ipa_system = "Responde SOLO con JSON válido."
	examples = _FR_IPA_REFERENCE_EXAMPLES if (language or "").lower() == "fr" else ""
	ipa_prompt = (
		"Devuélveme SOLO JSON válido con dos objetos: \"infinitive\" y \"noun\".\n"
		"- Cada objeto debe tener: \"phrase\" y \"ipa\" (IPA envuelto en barras /.../).\n"
		"- infinitive.phrase debe ser EXACTAMENTE el infinitivo propuesto (sin cambios).\n"
		"- noun.phrase debe ser la misma frase propuesta, o (solo en FR) puede corregir le/la -> l' cuando aplique.\n\n"
		"Formato IPA (importante):\n"
		"- Por defecto separa palabras con un espacio (p. ej. /ʒə sɥi/).\n"
		"- Usa '‿' SOLO cuando haya enlace real en esa frontera (encadenamiento o liaison), nunca como separador fijo.\n"
		"- NO pongas espacios alrededor de '‿'.\n"
		"- Si aparece consonante de enlace, escríbela justo antes del '‿' que introduce la palabra derecha (p. ej. /vu z‿ɛt/, /il z‿ɔ̃/).\n"
		"- Reglas (FR): la IPA se basa solo en sonidos reales (inventario activo), nunca en ortografía.\n"
		"  - Cada frontera se evalúa de forma independiente.\n"
		"  - Consonante inicial (fonética) en la palabra derecha bloquea cualquier enlace.\n"
		"  - Vocal + vocal -> encadenamiento (‿).\n"
		"    * Una consonante SOLO puede aparecer si es una consonante latente del morfema izquierdo (liaison productiva).\n"
		"    * PROHIBIDO insertar consonantes epentéticas (p. ej. /t/ por defecto) cuando el morfema izquierdo no tiene /t/ latente.\n"
		"  - PROHIBIDO inventar consonantes de enlace: solo si la consonante existe en el inventario activo del morfema en ese contexto.\n"
		"    * En particular: 'on' se realiza como /ɔ̃/ (sin /n/ final). No escribas /ɔ̃ n‿…/.\n"
		"  - Liaison requiere consonante latente fonológicamente activa; letras mudas no cuentan si no tienen realización sistemática.\n"
		"  - La gramática no puede forzar un enlace imposible; ningún sonido aparece si no existe en la realización aislada del morfema.\n\n"
		"Contraejemplos (NO hacer):\n"
		"- il est allé: NO /il ɛ‿ta.le/ -> SÍ /il ɛ‿a.le/ (" 
		"est" 
		"aislado: /ɛ/, sin /t/ latente aquí)\n"
		"- on est allé: NO /ɔ̃ n‿ɛ…/ ni /…‿t‿a.le/ -> SÍ /ɔ̃ ɛ‿a.le/\n"
		"- nous avons eu: NO /nu z‿avɔ̃‿z‿y/ (o /…‿zy/) -> SÍ /nu z‿avɔ̃‿y/\n"
		"  (" 
		"avons" 
		"aislado es /avɔ̃/ sin /z/; no inventes /z/ antes de 'eu')\n\n"
		"Ejemplos donde SÍ puede aparecer /t/ (liaison real, consonante latente):\n"
		"- ils ont eu: /il z‿ɔ̃‿t‿y/\n"
		"- ils sont allé: /il sɔ̃‿t‿a.le/\n\n"
		+ (examples + "\n" if examples else "")
		+ f"cual es el ipa de cada una:\n- {infinitive}\n- {noun_phrase}\n\n"
		f"Idioma: {language.upper()}\n"
		f"Infinitivo: {infinitive}\n"
		f"Frase sustantivo: {noun_phrase}\n"
	)

	resp = _call_openai_chat(
		api_key,
		[
			{"role": "system", "content": ipa_system},
			{"role": "user", "content": ipa_prompt},
		],
		max_tokens=220,
		response_format={"type": "json_object"},
	)
	raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
	parsed = _extract_json_from_text(str(raw)) or {}

	inf = parsed.get("infinitive") if isinstance(parsed, dict) else None
	noun = parsed.get("noun") if isinstance(parsed, dict) else None

	ipa_inf: Optional[str] = None
	ipa_noun: Optional[str] = None
	final_noun_phrase = noun_phrase

	if isinstance(inf, dict):
		maybe_ipa = inf.get("ipa")
		if isinstance(maybe_ipa, str) and maybe_ipa.strip():
			ipa_inf = _simple_ipa_from_text(maybe_ipa)

	if isinstance(noun, dict):
		maybe_phrase = noun.get("phrase")
		maybe_ipa = noun.get("ipa")
		candidate_phrase = maybe_phrase.strip() if isinstance(maybe_phrase, str) else ""
		if candidate_phrase and _accept_phrase_correction(language, noun_phrase, original_article, palabra, candidate_phrase):
			final_noun_phrase = candidate_phrase
		if isinstance(maybe_ipa, str) and maybe_ipa.strip():
			ipa_noun = _simple_ipa_from_text(maybe_ipa)


	parts: List[str] = []
	if ipa_inf:
		parts.append(ipa_inf)
	if ipa_noun:
		parts.append(ipa_noun)
	if not parts:
		return None
	return "; ".join(parts)


def _tokenize_phrase(language: str, phrase: str) -> List[str]:
	"""Best-effort tokenization for multi-word inputs.

	We keep this intentionally simple:
	- split on whitespace
	- strip surrounding punctuation
	- keep internal apostrophes/hyphens (e.g. j'aime)

	Return tokens in original order.
	"""
	text = _normalize_user_text(language, phrase or "")
	if not text:
		return []
	parts = re.split(r"\s+", text)
	tokens: List[str] = []
	for part in parts:
		p = _normalize_apostrophes((part or "").strip())
		if not p:
			continue
		# Strip common punctuation around tokens.
		p = p.strip('"“”‘’()[]{}.,;:!?«»')
		if not p:
			continue
		# Normalize casing for FR/DE to reduce duplicates.
		if (language or "").lower() in {"fr", "de"}:
			p = p.lower()
		# FR: contractions like d'erreurs / l'erreur should not count as different tokens.
		if (language or "").lower() == "fr":
			p = _fr_drop_contraction_prefix(p)
		tokens.append(p)
	return tokens


def _get_phrase_translation(api_key: str, language: str, phrase: str) -> str:
	lang_name = "francés" if language == "fr" else ("alemán" if language == "de" else language)
	prompt = (
		"Devuélveme únicamente JSON válido con un campo: \"translation\".\n"
		"- translation: traducción al español natural de la frase completa.\n"
		"  - Si la frase tiene pronombre sujeto explícito (p. ej. je/tu/il/elle/on/nous/vous/ils/elles), incluye el pronombre equivalente en español (yo/tú/él/ella/nosotros/vosotros/ellos/ellas).\n"
		"- No hay límite estricto de palabras, pero sé conciso (ideal: <= 20 palabras).\n\n"
		f"Idioma: {language.upper()} ({lang_name})\n"
		f"Frase completa: {phrase}\n"
		"Devuelve SOLO el JSON."
	)
	resp = _call_openai_chat(
		api_key,
		[{"role": "user", "content": prompt}],
		max_tokens=180,
		response_format={"type": "json_object"},
	)
	raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
	parsed = _extract_json_from_text(str(raw)) or {}
	tr = (parsed.get("translation") or "").strip()
	return tr


def _get_phrase_ipa(api_key: str, language: str, phrase: str, timeout: int = 30) -> Optional[str]:
	ipa_system = "Responde SOLO con JSON válido."
	examples = _FR_IPA_REFERENCE_EXAMPLES if (language or "").lower() == "fr" else ""
	ipa_prompt = (
		"Devuélveme SOLO JSON válido con EXACTAMENTE estos campos: \"phrase\" y \"ipa\".\n"
		"NO añadas texto extra.\n\n"
		"Formato IPA (importante):\n"
		"- Por defecto separa palabras con un espacio (p. ej. /ʒə sɥi/).\n"
		"- Usa '‿' SOLO cuando haya enlace real en esa frontera (encadenamiento o liaison), nunca como separador fijo.\n"
		"- NO pongas espacios alrededor de '‿'.\n"
		"- Si aparece consonante de enlace, escríbela justo antes del '‿' que introduce la palabra derecha (p. ej. /vu z‿ɛt/, /il z‿ɔ̃/).\n"
		"- Reglas (FR): la IPA se basa solo en sonidos reales (inventario activo), nunca en ortografía.\n"
		"  - Cada frontera se evalúa de forma independiente.\n"
		"  - Consonante inicial (fonética) en la palabra derecha bloquea cualquier enlace.\n"
		"  - Vocal + vocal -> encadenamiento (‿).\n"
		"    * Una consonante SOLO puede aparecer si es una consonante latente del morfema izquierdo (liaison productiva).\n"
		"    * PROHIBIDO insertar consonantes epentéticas (p. ej. /t/ por defecto) cuando el morfema izquierdo no tiene /t/ latente.\n"
		"  - PROHIBIDO inventar consonantes de enlace: solo si la consonante existe en el inventario activo del morfema en ese contexto.\n"
		"    * En particular: 'on' se realiza como /ɔ̃/ (sin /n/ final). No escribas /ɔ̃ n‿…/.\n"
		"  - Liaison requiere consonante latente fonológicamente activa; letras mudas no cuentan si no tienen realización sistemática.\n"
		"  - La gramática no puede forzar un enlace imposible; ningún sonido aparece si no existe en la realización aislada del morfema.\n\n"
		"Contraejemplos (NO hacer):\n"
		"- il est allé: NO /il ɛ‿ta.le/ -> SÍ /il ɛ‿a.le/ (" 
		"est" 
		"aislado: /ɛ/, sin /t/ latente aquí)\n"
		"- on est allé: NO /ɔ̃ n‿ɛ…/ ni /…‿t‿a.le/ -> SÍ /ɔ̃ ɛ‿a.le/\n"
		"- nous avons eu: NO /nu z‿avɔ̃‿z‿y/ (o /…‿zy/) -> SÍ /nu z‿avɔ̃‿y/\n"
		"  (" 
		"avons" 
		"aislado es /avɔ̃/ sin /z/; no inventes /z/ antes de 'eu')\n\n"
		"Ejemplos donde SÍ puede aparecer /t/ (liaison real, consonante latente):\n"
		"- ils ont eu: /il z‿ɔ̃‿t‿y/\n"
		"- ils sont allé: /il sɔ̃‿t‿a.le/\n\n"
		+ (examples + "\n" if examples else "")
		+ f"cual es el ipa de: {phrase}\n"
		"Devuelve la transcripción IPA envuelta en barras (/.../).\n\n"
		f"Idioma: {language.upper()}\n"
		f"Frase: {phrase}\n"
	)
	resp = _call_openai_chat(
		api_key,
		[
			{"role": "system", "content": ipa_system},
			{"role": "user", "content": ipa_prompt},
		],
		max_tokens=220,
		timeout=timeout,
		response_format={"type": "json_object"},
	)
	raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
	parsed = _extract_json_from_text(str(raw))
	if parsed and isinstance(parsed, dict):
		maybe_ipa = parsed.get("ipa")
		# We do not allow phrase corrections here; we want to store exactly what the user entered.
		if isinstance(maybe_ipa, str) and maybe_ipa.strip():
			return _simple_ipa_from_text(maybe_ipa)

	return _simple_ipa_from_text(str(raw))


def _prepare_phrase_actions(
	phrase: str,
	list_id: int,
	language: str,
	openai_api_key: Optional[str] = None,
	user_id: Optional[Any] = None,
	rejected_words_table: Optional[str] = None,
) -> Dict[str, Any]:
	"""Prepare SQL actions for a multi-word phrase.

	Requirements:
	- Insert/update each token as an individual entry using existing logic.
	- Also insert/update the full phrase with phrase-level IPA that includes liaison.
	"""
	_load_dotenv_if_present()
	api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
	clean_phrase = _normalize_user_text(language, phrase or "")
	phrase_for_storage = _normalize_phrase_for_tts_storage(language, clean_phrase)
	tokens = _tokenize_phrase(language, clean_phrase)
	# Deduplicate tokens while preserving order.
	seen: set[str] = set()
	unique_tokens: List[str] = []
	for t in tokens:
		key = t.lower()
		if key in seen:
			continue
		seen.add(key)
		unique_tokens.append(t)

	queries: List[Tuple[str, Tuple[Any, ...]]] = []
	subactions: List[Dict[str, Any]] = []
	for token in unique_tokens:
		# Token must be treated as an individual entry.
		if not token or " " in token:
			continue
		action = prepare_word_actions(
			palabra=token,
			list_id=list_id,
			language=language,
			openai_api_key=openai_api_key,
			user_id=user_id,
			rejected_words_table=rejected_words_table,
		)
		for q in action.get("queries", []) or []:
			queries.append(q)
		subactions.append(
			{
				"word": token,
				"pos": action.get("pos"),
				"is_verb": bool(action.get("is_verb")),
				"is_noun": bool(action.get("is_noun")),
				"is_noun_and_verb": bool(action.get("is_noun_and_verb")),
			}
		)

	phrase_translation = ""
	phrase_ipa: Optional[str] = None
	phrase_association: Optional[str] = None
	if api_key:
		phrase_translation = _get_phrase_translation(api_key, language, clean_phrase)
		phrase_ipa = _get_phrase_ipa(api_key, language, clean_phrase)

	if phrase_translation:
		phrase_association = f"{phrase_for_storage}, {phrase_translation}"

	def _already_inserts_word(qs: List[Tuple[str, Tuple[Any, ...]]], w: str, lid: int) -> bool:
		for sql, params in qs:
			if "INSERT INTO words" not in (sql or ""):
				continue
			if not params or len(params) < 3:
				continue
			try:
				pw = params[0]
				plid = params[2]
			except Exception:
				continue
			if pw == w and plid == lid:
				return True
		return False

	user_id, rejected_words_table, language_norm = _resolve_rejected_words_config(
		user_id=user_id,
		rejected_words_table=rejected_words_table,
		language=language,
	)
	reject_enabled = bool((rejected_words_table or "").strip()) and (user_id is not None)
	phrase_insert_already_present = _already_inserts_word(queries, phrase_for_storage, list_id)

	if reject_enabled:
		if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", str(rejected_words_table).strip()):
			raise ValueError("Invalid rejected_words_table identifier")
		table = str(rejected_words_table).strip()
		insert_sql = (
			"INSERT INTO words (word, used, association, state, list_id, successes, \"IPA_word\", added)\n"
			"SELECT %s, FALSE, %s, 'New', %s, 0, %s, TRUE\n"
			f"WHERE NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s)\n"
			"ON CONFLICT (word, list_id) DO NOTHING;"
		)
		if not phrase_insert_already_present:
			queries.append((insert_sql, (phrase_for_storage, phrase_association, list_id, phrase_ipa, user_id, language_norm, phrase_for_storage)))
	else:
		insert_sql = (
			"INSERT INTO words (word, used, association, state, list_id, successes, \"IPA_word\", added)\n"
			"        VALUES (%s, FALSE, %s, 'New', %s, 0, %s, TRUE)\n"
			"        ON CONFLICT (word, list_id) DO NOTHING;"
		)
		if not phrase_insert_already_present:
			queries.append((insert_sql, (phrase_for_storage, phrase_association, list_id, phrase_ipa)))
	if phrase_ipa:
		upd_ipa = (
			'UPDATE words SET "IPA_word" = %s\n'
			'            WHERE word = %s AND list_id = %s AND "IPA_word" IS NULL'
		)
		if reject_enabled:
			upd_ipa = upd_ipa + f"\n              AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s);"
			queries.append((upd_ipa, (phrase_ipa, phrase_for_storage, list_id, user_id, language_norm, phrase_for_storage)))
		else:
			upd_ipa = upd_ipa + ";"
			queries.append((upd_ipa, (phrase_ipa, phrase_for_storage, list_id)))
	if phrase_association:
		upd_assoc = (
			"UPDATE words SET association = %s\n"
			"            WHERE word = %s AND list_id = %s AND (association IS NULL OR association = '')"
		)
		if reject_enabled:
			upd_assoc = upd_assoc + f"\n              AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s);"
			queries.append((upd_assoc, (phrase_association, phrase_for_storage, list_id, user_id, language_norm, phrase_for_storage)))
		else:
			upd_assoc = upd_assoc + ";"
			queries.append((upd_assoc, (phrase_association, phrase_for_storage, list_id)))

	return {
		"word": phrase_for_storage,
		"list_id": list_id,
		"pos": "phrase",
		"is_phrase": True,
		"tokens": unique_tokens,
		"association": phrase_association,
		"ipa_word": phrase_ipa,
		"entries": [],
		"queries": queries,
		"debug": {"subactions": subactions},
	}


def prepare_word_actions(
	palabra: str,
	list_id: int,
	language: str,
	openai_api_key: Optional[str] = None,
	user_id: Optional[Any] = None,
	rejected_words_table: Optional[str] = None,
	expand_verbs: bool = True,
) -> Dict[str, Any]:
	user_id, rejected_words_table, language_norm = _resolve_rejected_words_config(
		user_id=user_id,
		rejected_words_table=rejected_words_table,
		language=language,
	)
	# Phrase mode: if the user inputs a multi-word phrase, we create entries for
	# each token AND an entry for the full phrase with phrase-level IPA.
	if isinstance(palabra, str):
		clean = _normalize_user_text(language, palabra)
		if clean and re.search(r"\s", clean) and len(clean.split()) >= 2:
			return _prepare_phrase_actions(
				clean,
				list_id=list_id,
				language=language,
				openai_api_key=openai_api_key,
				user_id=user_id,
				rejected_words_table=rejected_words_table,
			)
		# Single token: normalize punctuation/apostrophes and drop conservative FR contractions.
		if clean:
			if (language or "").lower() == "fr":
				clean = _fr_drop_contraction_prefix(clean)
			palabra = clean

	_load_dotenv_if_present()
	api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
	translation = ""  # noun translation
	article = ""
	gender_fr = ""
	pos: str = ""
	is_verb = False
	is_noun = False
	is_adjective = False
	is_noun_and_verb = False
	tense: str = ""
	verb_entries: List[Dict[str, str]] = []
	verb_infinitive: str = ""
	verb_inf_translation: str = ""
	ipa_word: Optional[str] = None
	association: Optional[str] = None
	debug: Dict[str, Any] = {}
	debug_enabled = os.getenv("WORD_ADDER_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
	extra_entries: List[Dict[str, Any]] = []

	# 1) POS detection + details (noun: translation+article, verb: full paradigm)
	if api_key:
		lang_name = "francés" if language == "fr" else ("alemán" if language == "de" else language)
		articles_hint = (
			"IMPORTANTE: el artículo debe ser del idioma original (FR/DE), no en español. "
			"Devuelve el ARTÍCULO DEFINIDO correcto que se usaría con esa palabra en el idioma original, "
			"respetando género/número y elisión/contracción ortográfica cuando aplique. "
			"\n"
			"- FR: el campo article debe ser EXACTAMENTE uno de: le, la, l', les (o cadena vacía si no aplica). "
			"  Si es singular y empieza por vocal (a/e/i/o/u/y) o vocal acentuada (é/è/ê/à/ï/î/ô/ü/œ/æ), el artículo debe ser l'. "
			"  Ejemplos: homme -> l' ; idée -> l' ; livre -> le ; place -> la. "
			"- DE: article debe ser EXACTAMENTE uno de: der, die, das (o cadena vacía si no aplica)."
		)
		# 1a) POS classification (robust; handles conjugated verb forms like 'sommes')
		pos_prompt = (
			"Devuélveme SOLO JSON válido con campos: \"pos\" y opcionalmente \"lemma\".\n"
			"- pos: noun/verb/adjective/noun_verb/other\n"
			"  - Usa noun_verb si la misma palabra puede ser sustantivo Y verbo.\n"
			"  - Si es un ADJETIVO (aunque venga de una forma verbal usada como adjetivo/sustantivo), pos debe ser 'adjective' (NO verb).\n"
			"  - Si es una forma CONJUGADA o IMPERATIVO de un verbo, pos DEBE ser 'verb'.\n"
			"  - Si es un ADVERBIO (ej: tard, très, souvent), pos debe ser 'other' (sin artículo).\n"
			"- lemma: si pos incluye verb y puedes inferirlo, el infinitivo (ej: 'sommes' -> 'être', 'mange' -> 'manger').\n\n"
			"Pistas importantes (FR):\n"
			"- Un verbo conjugado normalmente NO lleva artículo (no es 'le/la/les <verbo>').\n"
			"- Ejemplos: 'mange' (sin contexto) es típicamente forma de 'manger' => verb.\n"
			"- Ejemplo: 'sommes' => verb (être).\n\n"
			f"Idioma: {language.upper()} ({lang_name})\n"
			f"Palabra: {palabra}\n"
		)
		resp_pos = _call_openai_chat(
			api_key,
			[{"role": "user", "content": pos_prompt}],
			max_tokens=80,
			response_format={"type": "json_object"},
		)
		raw_pos = resp_pos.get("choices", [{}])[0].get("message", {}).get("content", "")
		parsed_pos = _extract_json_from_text(str(raw_pos)) or {}
		debug["pos_raw_response"] = parsed_pos
		pos_field = parsed_pos.get("pos") if "pos" in parsed_pos else parsed_pos.get("part_of_speech")
		lemma = (parsed_pos.get("lemma") or "").strip()
		is_noun, is_verb, is_adjective, pos = _parse_pos_field(pos_field)
		# If this token is known to be ambiguous (noun+verb), force noun_verb mode so we
		# store both the noun meaning and the full verb paradigm.
		if (language or "").lower() == "fr" and _is_fr_ambiguous_noun_verb_token(palabra) and is_verb and not is_noun:
			is_noun = True
			pos = "noun_verb"
			if not lemma:
				lemma = _fr_default_lemma_for_ambiguous_token(palabra)
		debug["pos_parsed"] = {"pos": pos, "is_noun": is_noun, "is_verb": is_verb, "is_adjective": is_adjective, "lemma": lemma}
		# Fallback: if model says noun but token looks like a French conjugated verb form,
		# do a second, very explicit verb-check. This avoids forcing an article.
		fallback_trigger = (language or "").lower() == "fr" and is_noun and not is_verb and _looks_like_fr_conjugated_verb_form(palabra)
		debug["fallback_verb_check_triggered"] = bool(fallback_trigger)
		if fallback_trigger:
			verb_check_prompt = (
				"Devuélveme SOLO JSON válido con: \"is_verb\" (boolean) y opcional \"lemma\".\n"
				"- is_verb: true si esta palabra, sin contexto, puede ser una forma conjugada/imperativo de un verbo.\n"
				"- lemma: si is_verb=true, el infinitivo.\n"
				"Ejemplos: 'mange' -> {is_verb:true, lemma:'manger'} ; 'livre' -> {is_verb:false}.\n\n"
				f"Palabra: {palabra}\n"
			)
			resp_check = _call_openai_chat(
				api_key,
				[{"role": "user", "content": verb_check_prompt}],
				max_tokens=80,
				response_format={"type": "json_object"},
			)
			raw_check = resp_check.get("choices", [{}])[0].get("message", {}).get("content", "")
			parsed_check = _extract_json_from_text(str(raw_check)) or {}
			debug["fallback_verb_check_raw_response"] = parsed_check
			if bool(parsed_check.get("is_verb")):
				lemma = (parsed_check.get("lemma") or lemma or "").strip()
				# If the token is known to be ambiguous (noun+verb), keep noun and add verb.
				if _is_fr_ambiguous_noun_verb_token(palabra):
					is_verb = True
					is_noun = True
					pos = "noun_verb"
				else:
					# Otherwise treat it as verb-only to avoid forcing articles like "le mange".
					is_verb = True
					is_noun = False
					pos = "verb"

		# Safety override: common adverbs must not be treated as nouns/verbs.
		if (language or "").lower() == "fr" and _is_fr_common_adverb(palabra):
			debug["pos_override"] = {"reason": "common_adverb", "word": palabra, "from": {"pos": pos, "is_noun": is_noun, "is_verb": is_verb}}
			pos = "other"
			is_noun = False
			is_verb = False
			is_adjective = False
			lemma = ""

		is_noun_and_verb = is_noun and is_verb
		verb_infinitive = lemma if is_verb else ""
		debug["final_pos"] = {"pos": pos, "is_noun": is_noun, "is_verb": is_verb, "is_adjective": is_adjective, "is_noun_and_verb": is_noun_and_verb, "lemma": verb_infinitive}

		# FR nouns: store singular by default.
		# Requirement: do NOT add regular plural noun forms; keep only singular,
		# unless the plural is irregular (we don't try to singularize irregulars).
		if (language or "").strip().lower() == "fr" and is_noun and not is_verb and not is_noun_and_verb:
			plural_original = (palabra or "").strip()
			singular_candidate = _fr_singular_candidate_for_plural_noun(plural_original)
			# If we can safely derive a singular candidate, switch to storing the singular.
			if singular_candidate and singular_candidate != plural_original:
				debug["normalized_plural_to_singular"] = {"from": plural_original, "to": singular_candidate}
				palabra = singular_candidate

		if is_noun:
			# 1b-noun) translation + article
			gender_fr = ""
			noun_prompt = (
				"Devuélveme únicamente JSON válido con campos: "
				"\"translation\" (traducción al español, máximo 3 palabras, SIN artículo/determinante: no uses 'un/una/el/la/los/las'), "
				"\"article\" (ARTÍCULO DEFINIDO EN EL IDIOMA ORIGINAL si es un sustantivo; si no aplica, cadena vacía), "
				"\"base_article\" (SOLO FR: artículo definido subyacente SIN elisión: le/la/les; para l' debe ser le o la; si no aplica, cadena vacía), "
				"y \"gender\" (solo para FR sustantivos singulares: 'm' o 'f'. Si no aplica, cadena vacía). "
				f"La palabra es '{palabra}' en {lang_name}. {articles_hint} Devuelve sólo el JSON sin explicaciones."
			)
			resp_noun = _call_openai_chat(
				api_key,
				[{"role": "user", "content": noun_prompt}],
				max_tokens=120,
				response_format={"type": "json_object"},
			)
			raw_noun = resp_noun.get("choices", [{}])[0].get("message", {}).get("content", "")
			parsed_noun = _extract_json_from_text(str(raw_noun))
			if parsed_noun:
				translation = _normalize_spanish_noun_translation((parsed_noun.get("translation") or "").strip())
				raw_article = (parsed_noun.get("article") or "")
				raw_article_norm = str(raw_article).strip().lower()
				article = _normalize_article(raw_article or "", language)
				if (language or "").lower() == "fr":
					base_article_fr = _normalize_fr_base_article(parsed_noun.get("base_article") or parsed_noun.get("article_base"))
					gender_from_base = "m" if base_article_fr == "le" else ("f" if base_article_fr == "la" else "")
					gender_fr = gender_from_base or _normalize_fr_gender(parsed_noun.get("gender") or parsed_noun.get("genre"))
					if not gender_fr and raw_article_norm in {"le", "la"}:
						gender_fr = "m" if raw_article_norm == "le" else "f"
				# Enforce French elision for definite articles when the noun begins with a vowel/h.
				# This prevents incorrect phrases like "le été".
				if (language or "").lower() == "fr":
					art = (article or "").strip().lower()
					if art in {"le", "la"} or (article or "").strip().lower() in {"le", "la"}:
						# Normalize to include a trailing space for consistency.
						article = f"{art} "
						if not gender_fr:
							gender_fr = "m" if art == "le" else "f"
					if (article or "").strip().lower() in {"le", "la"} and _starts_with_french_elision_sound(palabra):
						article = "l'"

				# Retry: some responses incorrectly omit the article for common nouns.
				# We retry once with a stricter prompt to get a definite article.
				if (language or "").lower() == "fr" and is_noun and not (article or "").strip():
					article_retry_prompt = (
						"Devuélveme SOLO JSON válido con campos: \"article\", \"base_article\" y \"gender\".\n"
						"- article: ARTÍCULO DEFINIDO francés EXACTAMENTE uno de: le, la, l', les (o cadena vacía si NO aplica).\n"
						"- base_article: SOLO FR: le/la/les (sin elisión). Para article=l' debe ser le o la.\n"
						"- gender: solo si es singular: 'm' o 'f' (si no aplica, cadena vacía).\n"
						"- Si la palabra empieza por vocal o h muet y es singular, debe ser l'.\n"
						"NO devuelvas un/une/des.\n\n"
						f"Palabra (FR): {palabra}\n"
						"Devuelve SOLO el JSON."
					)
					resp_retry = _call_openai_chat(
						api_key,
						[{"role": "user", "content": article_retry_prompt}],
						max_tokens=60,
						response_format={"type": "json_object"},
					)
					raw_retry = resp_retry.get("choices", [{}])[0].get("message", {}).get("content", "")
					parsed_retry = _extract_json_from_text(str(raw_retry)) or {}
					if parsed_retry:
						article = _normalize_article(parsed_retry.get("article") or "", language)
						base_retry = _normalize_fr_base_article(parsed_retry.get("base_article") or parsed_retry.get("article_base"))
						gender_from_base_retry = "m" if base_retry == "le" else ("f" if base_retry == "la" else "")
						gender_retry = _normalize_fr_gender(parsed_retry.get("gender") or parsed_retry.get("genre"))
						gender_fr = gender_from_base_retry or (gender_retry or gender_fr)
						if (article or "").strip().lower() in {"le", "la"} and _starts_with_french_elision_sound(palabra):
							article = "l'"

				# Safety override: some French adverbs get misclassified as nouns and receive a
				# definite article (often l'). If the Spanish translation is an adverb in -mente,
				# treat this token as OTHER (no article) to avoid storing "l'<word>".
				if (language or "").strip().lower() == "fr":
					tr_low = (translation or "").strip().lower()
					art_low = (article or "").strip().lower()
					if tr_low.endswith("mente") and art_low in {"le", "la", "les", "l'"}:
						debug["pos_override"] = {
							"reason": "spanish_translation_looks_adverb_mente",
							"word": palabra,
							"translation": translation,
							"article": article,
						}
						pos = "other"
						is_noun = False
						is_adjective = False
						gender_fr = ""
						article = ""
				debug["noun_details"] = {"translation": translation, "article": article, "gender": gender_fr}

		elif is_adjective:
			# 1b-adjective) translation + feminine form
			adj_prompt = (
				"Devuélveme únicamente JSON válido con campos: "
				"\"translation\" (traducción al español, máximo 3 palabras, SIN artículo/determinante), "
				"\"feminine\" (forma femenina singular en el idioma original; si no existe o es igual, cadena vacía), "
				"y opcional \"feminine_translation\" (traducción al español de la forma femenina si cambia; si no cambia, puede ir vacío).\n\n"
				"Regla: si la palabra es un ADJETIVO, responde como adjetivo (no como verbo aunque venga de una forma verbal usada como adjetivo).\n\n"
				f"Idioma: {language.upper()} ({lang_name})\n"
				f"Adjetivo: {palabra}\n"
				"Devuelve SOLO el JSON."
			)
			resp_adj = _call_openai_chat(
				api_key,
				[{"role": "user", "content": adj_prompt}],
				max_tokens=140,
				response_format={"type": "json_object"},
			)
			raw_adj = resp_adj.get("choices", [{}])[0].get("message", {}).get("content", "")
			parsed_adj = _extract_json_from_text(str(raw_adj)) or {}
			translation = _normalize_spanish_noun_translation((parsed_adj.get("translation") or "").strip())
			feminine = (parsed_adj.get("feminine") or "").strip()
			feminine_translation = _normalize_spanish_noun_translation((parsed_adj.get("feminine_translation") or "").strip())
			debug["adjective_details"] = {"translation": translation, "feminine": feminine, "feminine_translation": feminine_translation}

			default_fem = _default_feminine_by_suffix(language, palabra)
			fem_norm = _normalize_user_text(language, feminine) if feminine else ""
			if fem_norm:
				# Only add if feminine is genuinely distinct and irregular vs the simple suffix rule.
				if fem_norm != _normalize_user_text(language, palabra):
					if default_fem and _normalize_user_text(language, default_fem) != fem_norm:
						tr_f = feminine_translation or translation
						extra_entries.append({"word": fem_norm, "association": (f"{fem_norm}, {tr_f}" if tr_f else None), "ipa": None})

		elif not is_verb:
			# 1b-other) translation only (adverb/adjective/other): no article.
			other_prompt = (
				"Devuélveme únicamente JSON válido con un campo: \"translation\".\n"
				"- translation: traducción al español (máximo 3 palabras), SIN artículo/determinante (no uses un/una/el/la/los/las).\n\n"
				f"Idioma: {language.upper()} ({lang_name})\n"
				f"Palabra: {palabra}\n"
				"Devuelve SOLO el JSON."
			)
			resp_other = _call_openai_chat(
				api_key,
				[{"role": "user", "content": other_prompt}],
				max_tokens=80,
				response_format={"type": "json_object"},
			)
			raw_other = resp_other.get("choices", [{}])[0].get("message", {}).get("content", "")
			parsed_other = _extract_json_from_text(str(raw_other)) or {}
			translation = _normalize_spanish_noun_translation((parsed_other.get("translation") or "").strip())
			debug["other_details"] = {"translation": translation}

		if is_verb and expand_verbs:
			# 1b-verb) full paradigm
			verb_prompt = (
				"Devuélveme SOLO JSON válido con campos: \"tense\", opcional \"infinitive_translation\" y \"forms\".\n"
				"- tense: etiqueta corta del tiempo (p.ej. present, imparfait, futur, passé composé).\n"
				"- infinitive_translation: traducción al español del infinitivo (máx 3 palabras) si puedes.\n"
				"- forms: lista de objetos con: pronoun, phrase, translation.\n"
				"  - phrase: frase en el idioma original (pronombre + forma verbal) si aplica.\n"
				"  - translation: traducción al español de ESA frase completa.\n"
				"    - Incluye el pronombre sujeto en español si phrase lo incluye (ej: 'je suis' -> 'yo soy', 'nous sommes' -> 'nosotros somos').\n"
				"    - Máx 4 palabras (normalmente 2-3).\n"
				"    - SIN artículo/determinante (no uses un/una/el/la/los/las).\n\n"
				f"Idioma: {language.upper()} ({lang_name})\n"
				f"Entrada: {palabra}\n"
				f"Infinitivo/lemma sugerido (puede estar vacío): {lemma or ''}\n"
				"Devuelve SOLO el JSON."
			)
			resp_verb = _call_openai_chat(
				api_key,
				[{"role": "user", "content": verb_prompt}],
				max_tokens=420,
				response_format={"type": "json_object"},
			)
			raw_verb = resp_verb.get("choices", [{}])[0].get("message", {}).get("content", "")
			parsed_verb = _extract_json_from_text(str(raw_verb))
			if parsed_verb and isinstance(parsed_verb, dict):
				tense = (parsed_verb.get("tense") or "").strip()
				verb_inf_translation = (parsed_verb.get("infinitive_translation") or "").strip()
				debug["verb_details"] = {"tense": tense, "infinitive_translation": verb_inf_translation}
				forms = parsed_verb.get("forms")
				if isinstance(forms, list):
					for item in forms:
						if not isinstance(item, dict):
							continue
						pron = (item.get("pronoun") or "").strip()
						phrase_item = (item.get("phrase") or "").strip()
						tr_item = (item.get("translation") or "").strip()
						if phrase_item:
							verb_entries.append({"pronoun": pron, "phrase": phrase_item, "translation": tr_item})

			# Add an explicit infinitive entry (lemma) when available.
			# This is appended to keep backward-compatible primary entry behavior.
			inf = (verb_infinitive or "").strip()
			if inf:
				inf_tr = (verb_inf_translation or "").strip()
				tagged_tr = (f"{inf_tr} (infinitivo)" if inf_tr else "(infinitivo)").strip()
				verb_entries.append({"pronoun": "infinitive", "phrase": inf, "translation": tagged_tr})

			# If no explicit forms were provided, fall back to a single entry.
			if not verb_entries:
				verb_entries.append({"pronoun": "", "phrase": palabra, "translation": ""})

		elif is_verb and not expand_verbs:
			# Phrase-token mode: do NOT explode conjugations. Insert only the token itself.
			verb_entries.append({"pronoun": "", "phrase": palabra, "translation": ""})

	# For pure verbs we keep the original behavior: we don't build an article phrase.
	if is_verb and not is_noun_and_verb:
		phrase_for_ipa = palabra
		phrase_for_storage = palabra
	else:
		phrase_for_ipa = _build_phrase(article, palabra)
		phrase_for_storage = _maybe_append_fr_gender_marker(language, phrase_for_ipa, gender_fr if (language or "").lower() == "fr" else "")
	debug["noun_phrase"] = phrase_for_ipa
	debug["noun_phrase_storage"] = phrase_for_storage

	# 2) IPA
	if api_key and (not is_verb or is_noun_and_verb):
		ipa_system = "Responde SOLO con JSON válido."
		examples = _FR_IPA_REFERENCE_EXAMPLES if (language or "").lower() == "fr" else ""
		ipa_prompt = (
			"Devuélveme SOLO JSON válido con EXACTAMENTE estos campos: \"phrase\" y \"ipa\".\n"
			"NO añadas texto extra.\n\n"
			"Formato IPA (importante):\n"
			"- Por defecto separa palabras con un espacio (p. ej. /ʒə sɥi/).\n"
			"- Usa '‿' SOLO cuando haya enlace real en esa frontera (encadenamiento o liaison), nunca como separador fijo.\n"
			"- NO pongas espacios alrededor de '‿'.\n"
			"- Si aparece consonante de enlace, escríbela justo antes del '‿' que introduce la palabra derecha (p. ej. /vu z‿ɛt/, /il z‿ɔ̃/).\n"
			"- Reglas (FR): la IPA se decide por sonidos reales, nunca por ortografía.\n"
			"  - Cada frontera entre palabras se evalúa de forma independiente y secuencial.\n"
			"  - Si la palabra derecha empieza por consonante fonética, no puede haber enlace.\n"
			"  - Vocal+vocal produce encadenamiento (‿), nunca consonante.\n"
			"  - Liaison solo si existe una consonante latente fonológicamente activa.\n"
			"  - Ningún sonido aparece si no existe en el inventario fonológico activo del morfema en ese contexto.\n\n"
			+ (examples + "\n" if examples else "")
			+ f"cual es el ipa de: {phrase_for_ipa}\n"
			"Devuelve la transcripción IPA envuelta en barras (/.../).\n\n"
			+ f"Idioma: {language.upper()}\n"
			+ f"Palabra: {palabra}\n"
			+ f"Artículo propuesto (puede ser vacío): {article or ''}\n"
			+ f"Frase propuesta: {phrase_for_ipa}\n\n"
			+ "Devuelve SOLO el JSON."
		)
		# For nouns (including noun+verb tokens) we store the noun form alone,
		# so we only request IPA for the noun phrase (article + noun).
		resp = _call_openai_chat(
			api_key,
			[
				{"role": "system", "content": ipa_system},
				{"role": "user", "content": ipa_prompt},
			],
			max_tokens=120,
			response_format={"type": "json_object"},
		)
		raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
		parsed2 = _extract_json_from_text(str(raw))
		if parsed2 and isinstance(parsed2, dict):
			maybe_phrase = parsed2.get("phrase")
			maybe_ipa = parsed2.get("ipa")
			candidate_phrase = maybe_phrase.strip() if isinstance(maybe_phrase, str) else ""
			if candidate_phrase and _accept_phrase_correction(language, phrase_for_ipa, article, palabra, candidate_phrase):
				phrase_for_ipa = candidate_phrase
				phrase_for_storage = _maybe_append_fr_gender_marker(language, phrase_for_ipa, gender_fr if (language or "").lower() == "fr" else "")
			if isinstance(maybe_ipa, str) and maybe_ipa.strip():
				ipa_word = _simple_ipa_from_text(maybe_ipa)
		else:
			ipa_word = _simple_ipa_from_text(str(raw))

		# Note: We intentionally avoid local validation/rewrites here; prompt-only.

		# If we have extra derived entries (e.g., irregular adjective feminine), fetch IPA for them too.
		if extra_entries:
			for entry in extra_entries:
				w = (entry.get("word") or "").strip()
				if not w:
					continue
				entry["ipa"] = _get_phrase_ipa(api_key, language, w)

	if api_key and is_verb:
		ipa_system = "Responde SOLO con JSON válido."
		examples = _FR_IPA_REFERENCE_EXAMPLES if (language or "").lower() == "fr" else ""

		for entry in verb_entries:
			phrase_for_ipa = (entry.get("phrase") or "").strip()
			if not phrase_for_ipa:
				continue

			ipa_prompt = (
				"Devuélveme SOLO JSON válido con EXACTAMENTE estos campos: \"phrase\" y \"ipa\".\n"
				"NO añadas texto extra.\n\n"
				"Formato IPA (importante):\n"
				"- Por defecto separa palabras con un espacio (p. ej. /ʒə sɥi/).\n"
				"- Usa '‿' SOLO cuando haya enlace real en esa frontera (encadenamiento o liaison), nunca como separador fijo.\n"
				"- NO pongas espacios alrededor de '‿'.\n"
				"- Si aparece consonante de enlace, escríbela justo antes del '‿' que introduce la palabra derecha (p. ej. /vu z‿ɛt/, /il z‿ɔ̃/).\n"
				"- Reglas (FR): la IPA se decide por sonidos reales, nunca por ortografía.\n"
				"  - Cada frontera entre palabras se evalúa de forma independiente y secuencial.\n"
				"  - Si la palabra derecha empieza por consonante fonética, no puede haber enlace.\n"
				"  - Vocal+vocal produce encadenamiento (‿), nunca consonante.\n"
				"  - Liaison solo si existe una consonante latente fonológicamente activa.\n"
				"  - Ningún sonido aparece si no existe en el inventario fonológico activo del morfema en ese contexto.\n\n"
				+ (examples + "\n" if examples else "")
				+ f"cual es el ipa de: {phrase_for_ipa}\n"
				"Devuelve la transcripción IPA envuelta en barras (/.../).\n\n"
				f"Idioma: {language.upper()}\n"
				f"Frase propuesta: {phrase_for_ipa}\n\n"
				"Devuelve SOLO el JSON."
			)
			resp = _call_openai_chat(
				api_key,
				[
					{"role": "system", "content": ipa_system},
					{"role": "user", "content": ipa_prompt},
				],
				max_tokens=120,
				response_format={"type": "json_object"},
			)
			raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
			parsed2 = _extract_json_from_text(str(raw))
			entry_ipa: Optional[str] = None
			candidate_phrase = ""
			if parsed2 and isinstance(parsed2, dict):
				maybe_phrase = parsed2.get("phrase")
				maybe_ipa = parsed2.get("ipa")
				candidate_phrase = maybe_phrase.strip() if isinstance(maybe_phrase, str) else ""
				if candidate_phrase and _accept_verb_phrase_correction(language, phrase_for_ipa, candidate_phrase):
					phrase_for_ipa = candidate_phrase
				if isinstance(maybe_ipa, str) and maybe_ipa.strip():
					entry_ipa = _simple_ipa_from_text(maybe_ipa)
			else:
				entry_ipa = _simple_ipa_from_text(str(raw))

			entry["phrase"] = phrase_for_ipa
			# Note: We intentionally avoid local validation/rewrites here; prompt-only.
			entry["ipa"] = entry_ipa or ""

	# Fallback IPA via requests.get (tests monkeypatch this)
	if not ipa_word and (not is_verb and not is_noun_and_verb):
		try:
			lang_code = "fr" if language == "fr" else "de"
			resp = requests.get(f"https://example.com/api/entries/{lang_code}/{palabra}", timeout=5)
			if resp.status_code == 200:
				data = resp.json()
				if isinstance(data, list):
					for entry in data:
						for ph in entry.get("phonetics", []):
							if ph.get("text"):
								ipa_word = ph.get("text")
								break
						if ipa_word:
							break
		except Exception:
			pass

	if (not is_verb or is_noun_and_verb) and translation:
		association = f"{phrase_for_storage}, {translation}"

	if is_verb:
		# For verbs we generate one association per entry.
		for entry in verb_entries:
			tr_item = (entry.get("translation") or translation or "").strip()
			entry["translation"] = tr_item
			entry["association"] = f"{entry.get('phrase')}, {tr_item}" if tr_item else ""
		# Keep backward compatible top-level fields (first entry)
		if verb_entries:
			if not is_noun_and_verb:
				association = verb_entries[0].get("association") or None
				ipa_word = (verb_entries[0].get("ipa") or None) if not ipa_word else ipa_word

	queries: List[Tuple[str, Tuple[Any, ...]]] = []
	reject_enabled = bool((rejected_words_table or "").strip()) and (user_id is not None)
	table = ""
	if reject_enabled:
		if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", str(rejected_words_table).strip()):
			raise ValueError("Invalid rejected_words_table identifier")
		table = str(rejected_words_table).strip()
		insert_sql = (
			"INSERT INTO words (word, used, association, state, list_id, successes, \"IPA_word\", added)\n"
			"SELECT %s, FALSE, %s, 'New', %s, 0, %s, TRUE\n"
			f"WHERE NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s)\n"
			"ON CONFLICT (word, list_id) DO NOTHING;"
		)
	else:
		insert_sql = (
			"INSERT INTO words (word, used, association, state, list_id, successes, \"IPA_word\", added)\n"
			"        VALUES (%s, FALSE, %s, 'New', %s, 0, %s, TRUE)\n"
			"        ON CONFLICT (word, list_id) DO NOTHING;"
		)

	if not is_verb and not is_noun_and_verb:
		if reject_enabled:
			queries.append((insert_sql, (palabra, association, list_id, ipa_word, user_id, language_norm, palabra)))
		else:
			queries.append((insert_sql, (palabra, association, list_id, ipa_word)))

		# Extra derived entries (e.g., irregular adjective feminine)
		if extra_entries:
			for entry in extra_entries:
				w = (entry.get("word") or "").strip()
				if not w:
					continue
				assoc_extra = entry.get("association")
				ipa_extra = entry.get("ipa")
				if reject_enabled:
					queries.append((insert_sql, (w, assoc_extra, list_id, ipa_extra, user_id, language_norm, w)))
				else:
					queries.append((insert_sql, (w, assoc_extra, list_id, ipa_extra)))

		if ipa_word:
			upd_ipa = (
				'UPDATE words SET "IPA_word" = %s\n'
				'            WHERE word = %s AND list_id = %s AND "IPA_word" IS NULL'
			)
			if reject_enabled:
				upd_ipa = upd_ipa + f"\n              AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s);"
				queries.append((upd_ipa, (ipa_word, palabra, list_id, user_id, language_norm, palabra)))
			else:
				upd_ipa = upd_ipa + ";"
				queries.append((upd_ipa, (ipa_word, palabra, list_id)))

		if association:
			upd_assoc = (
				"UPDATE words SET association = %s\n"
				"            WHERE word = %s AND list_id = %s AND (association IS NULL OR association = '' OR association LIKE '%%,%%,%%')"
			)
			if reject_enabled:
				upd_assoc = upd_assoc + f"\n              AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s);"
				queries.append((upd_assoc, (association, palabra, list_id, user_id, language_norm, palabra)))
			else:
				upd_assoc = upd_assoc + ";"
				queries.append((upd_assoc, (association, palabra, list_id)))

		# Homophones linking (noun-only). This must be included in the returned SQL
		# so external callers that only execute `queries` still get the behavior.
		_append_homophone_link_queries(queries, list_id=list_id, word=palabra, ipa_word=ipa_word)
	else:
		# noun+verb: insert the noun as a normal noun row, then one row per pronoun phrase.
		if is_noun_and_verb:
			if reject_enabled:
				queries.append((insert_sql, (palabra, association, list_id, ipa_word, user_id, language_norm, palabra)))
			else:
				queries.append((insert_sql, (palabra, association, list_id, ipa_word)))

			if ipa_word:
				upd_ipa = (
					'UPDATE words SET "IPA_word" = %s\n'
					'            WHERE word = %s AND list_id = %s AND "IPA_word" IS NULL'
				)
				if reject_enabled:
					upd_ipa = upd_ipa + f"\n              AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s);"
					queries.append((upd_ipa, (ipa_word, palabra, list_id, user_id, language_norm, palabra)))
				else:
					upd_ipa = upd_ipa + ";"
					queries.append((upd_ipa, (ipa_word, palabra, list_id)))

			if association:
				upd_assoc = (
					"UPDATE words SET association = %s\n"
					"            WHERE word = %s AND list_id = %s AND (association IS NULL OR association = '' OR association LIKE '%%,%%,%%')"
				)
				if reject_enabled:
					upd_assoc = upd_assoc + f"\n              AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.user_id = %s AND r.language = %s AND r.palabra = %s);"
					queries.append((upd_assoc, (association, palabra, list_id, user_id, language_norm, palabra)))
				else:
					upd_assoc = upd_assoc + ";"
					queries.append((upd_assoc, (association, palabra, list_id)))

			# Homophones linking for the noun row in noun+verb mode.
			_append_homophone_link_queries(queries, list_id=list_id, word=palabra, ipa_word=ipa_word)

		# One row per pronoun phrase.
		for entry in verb_entries:
			phrase_item = (entry.get("phrase") or "").strip()
			assoc_item = (entry.get("association") or "").strip() or None
			ipa_item = (entry.get("ipa") or "").strip() or None
			if not phrase_item:
				continue
			if reject_enabled:
				queries.append((insert_sql, (phrase_item, assoc_item, list_id, ipa_item, user_id, language_norm, phrase_item)))
			else:
				queries.append((insert_sql, (phrase_item, assoc_item, list_id, ipa_item)))

	if debug_enabled:
		print("[WORD_ADDER_DEBUG]", json.dumps(debug, ensure_ascii=False))

	return {
		"word": palabra,
		"list_id": list_id,
		"pos": pos,
		"is_verb": is_verb,
		"is_noun": is_noun,
		"is_noun_and_verb": is_noun_and_verb,
		"verb_infinitive": verb_infinitive,
		"tense": tense,
		"entries": verb_entries if is_verb else [],
		"association": association,
		"ipa_word": ipa_word,
		"debug": debug,
		"queries": queries,
	}


def add_word(
	conn,
	cur,
	palabra: str,
	list_id: Optional[int] = None,
	language: Optional[str] = None,
	openai_api_key: Optional[str] = None,
	user_id: Optional[Any] = None,
	rejected_words_table: Optional[str] = None,
) -> Dict[str, Any]:
	if list_id is None or language is None:
		raise ValueError("add_word requires list_id and language")

	_ = conn  # kept for backward compatibility; caller controls commit

	actions = prepare_word_actions(
		palabra=palabra,
		list_id=list_id,
		language=language,
		openai_api_key=openai_api_key,
		user_id=user_id,
		rejected_words_table=rejected_words_table,
	)

	for sql, params in actions.get("queries", []):
		cur.execute(sql, params)

	primary_word = palabra
	if actions.get("is_verb") and not actions.get("is_noun_and_verb"):
		# For pure verbs, the primary inserted word is the first phrase.
		entries = actions.get("entries") or []
		if isinstance(entries, list) and entries:
			primary_word = (entries[0].get("phrase") or palabra).strip() or palabra

	word_db_id = None
	try:
		# Homophones: if we can detect same IPA in the list, append the homophone word
		# at the end of association for BOTH the new word and the existing word.
		_link_homophones_in_db(
			cur=cur,
			list_id=list_id,
			word=primary_word,
			ipa_word=(actions.get("ipa_word") or ""),
		)

		cur.execute("SELECT id FROM words WHERE word = %s AND list_id = %s;", (primary_word, list_id))
		row = cur.fetchone()
		word_db_id = row[0] if (row and len(row) >= 1) else None
	except Exception:
		word_db_id = None

	return {
		"word_db_id": word_db_id,
		"association": actions.get("association"),
		"ipa_word": actions.get("ipa_word"),
		"is_verb": bool(actions.get("is_verb")),
		"runtime_signature": (
			getattr(sys.modules.get("frequency_db_utils"), "runtime_signature", lambda: None)()
			if sys.modules.get("frequency_db_utils")
			else None
		),
	}


def _should_link_homophones_for_word(word: str) -> bool:
	# Homophones are stored in `association`, but we must preserve the invariant
	# that the *base* association is always: "phrase, translation".
	# If we link homophones, we add them as a suffix using a non-comma delimiter:
	#   "phrase, translation | homófonos: w1; w2"
	# This avoids corrupting the primary comma separator.
	val = os.getenv("WORD_ADDER_LINK_HOMOPHONES", "").strip().lower()
	if val in {"0", "false", "no", "off"}:
		return False
	# Avoid linking for multi-word phrases (verb phrases, etc.).
	# Homophone behavior is intended for the base "word" entries.
	if not (word and isinstance(word, str)):
		return False
	if " " in word.strip():
		return False
	# Only link for canonical stored tokens (no edge punctuation / no contraction prefix variants).
	# This prevents linking junk legacy rows like "d'erreurs." as if they were real words.
	canon = _canonical_word_for_homophones("fr", word)
	return canon == word.strip()


def _canonical_word_for_homophones(language: str, word: str) -> str:
	"""Return the canonical form used to decide homophone-link eligibility.

	We do NOT rewrite DB values here; we only use this to decide whether a stored
	word should participate in homophone linking.

	Rules:
	- Normalize apostrophes and strip edge punctuation.
	- FR: drop conservative contraction prefixes d'/l' (these are not distinct words).
	- Lowercase for FR/DE.
	"""
	lang = (language or "").strip().lower()
	s = _normalize_user_text(lang, word or "")
	if lang == "fr":
		s = _fr_drop_contraction_prefix(s)
	return s


_HOMOPHONE_SECTION_SEPARATOR = " | homófonos: "
_HOMOPHONE_ITEM_SEPARATOR = "; "


def _append_homophone_to_association(association: Optional[str], homophone_word: str) -> str:
	"""Append a homophone word to association without breaking `phrase, translation`.

	Format:
	- Base stays unchanged: "phrase, translation"
	- Homophones go in a suffix: "... | homófonos: w1; w2"
	"""
	assoc = (association or "").strip()
	h = (homophone_word or "").strip()
	if not h:
		return assoc
	if not assoc:
		return h

	base = assoc
	items: List[str] = []
	if _HOMOPHONE_SECTION_SEPARATOR in assoc:
		base, tail = assoc.split(_HOMOPHONE_SECTION_SEPARATOR, 1)
		base = base.strip()
		for part in (tail or "").split(";"):
			p = part.strip()
			if p:
				items.append(p)

	# Avoid duplicates.
	if h in items:
		return assoc
	items.append(h)
	return f"{base}{_HOMOPHONE_SECTION_SEPARATOR}{_HOMOPHONE_ITEM_SEPARATOR.join(items)}"


def _append_assoc_token(association: Optional[str], token: str) -> str:
	# Backward-compat shim: homophone linking now uses a dedicated safe format.
	return _append_homophone_to_association(association, token)


def _link_homophones_in_db(cur, list_id: int, word: str, ipa_word: str) -> None:
	"""If there are homophones (same IPA) in the list, link them via association.

	To preserve the invariant `phrase, translation`, homophones are added in a
	suffix section:
		"phrase, translation | homófonos: w1; w2"
	"""
	if not ipa_word:
		return
	# Only canonical words participate in linking.
	if _canonical_word_for_homophones("fr", word) != word:
		return
	ipa = str(ipa_word).strip()
	if not ipa:
		return
	# Only link against other single-token words (avoid verb phrases).
	cur.execute(
		"""
		SELECT word, association
		FROM words
		WHERE list_id = %s
		  AND \"IPA_word\" = %s
		  AND word <> %s
		  AND word NOT LIKE '%% %%';
		""".strip(),
		(list_id, ipa, word),
	)
	candidates = cur.fetchall() or []
	if not candidates:
		return

	# Fetch current association once.
	cur.execute(
		"SELECT association FROM words WHERE list_id = %s AND word = %s;",
		(list_id, word),
	)
	row = cur.fetchone()
	current_assoc = row[0] if (row and len(row) >= 1) else None

	for other_word, other_assoc in candidates:
		other_w = (other_word or "").strip()
		if not other_w or other_w == word:
			continue
		# Skip non-canonical variants like d'erreurs.
		if _canonical_word_for_homophones("fr", other_w) != other_w:
			continue

		new_current_assoc = _append_homophone_to_association(current_assoc, other_w)
		if new_current_assoc != (current_assoc or "").strip():
			cur.execute(
				"UPDATE words SET association = %s WHERE word = %s AND list_id = %s;",
				(new_current_assoc, word, list_id),
			)
			current_assoc = new_current_assoc

		new_other_assoc = _append_homophone_to_association(other_assoc, word)
		if new_other_assoc != (other_assoc or "").strip():
			cur.execute(
				"UPDATE words SET association = %s WHERE word = %s AND list_id = %s;",
				(new_other_assoc, other_w, list_id),
			)


def _append_homophone_link_queries(
	queries: List[Tuple[str, Tuple[Any, ...]]],
	*,
	list_id: int,
	word: str,
	ipa_word: Optional[str],
) -> None:
	"""Append SQL statements that link homophones (same IPA) via `association`.

	This is designed to work even when the caller does NOT use `add_word()` and
	only executes `prepare_word_actions(...)["queries"]`.

	Behavior:
	- For `word`: append all other single-token words in the same list that share
	  the same `IPA_word`.
	- For each other homophone word: append `word`.
	- Avoid duplicates by treating the homophone section as a ';'-separated list.
	"""
	if not ipa_word:
		return
	if not _should_link_homophones_for_word(word):
		return
	ipa = str(ipa_word).strip()
	if not ipa:
		return

	# 1) Update the current word's association to include all *missing* homophones.
	# We avoid duplicates using regexp_split_to_array on the homophone section tail.
	queries.append(
		(
			"""
			UPDATE words w1
			SET association = CASE
				WHEN w1.association IS NULL OR w1.association = '' THEN
					(
						SELECT string_agg(w2.word, '; ')
						FROM words w2
						WHERE w2.list_id = w1.list_id
						  AND w2.\"IPA_word\" = w1.\"IPA_word\"
						  AND w2.word <> w1.word
						  AND w2.word NOT LIKE '%% %%'
						  -- Exclude non-lexical variants (contraction prefixes / trailing punctuation)
						  AND w2.word NOT LIKE 'd''%%'
						  AND w2.word NOT LIKE 'l''%%'
						  AND w2.word NOT LIKE '%%.'
						  AND w2.word NOT LIKE '%%,'
						  AND w2.word NOT LIKE '%%;'
						  AND w2.word NOT LIKE '%%:'
					)
				WHEN position(' | homófonos: ' in w1.association) > 0 THEN
					w1.association || '; ' || (
						SELECT string_agg(w2.word, '; ')
						FROM words w2
						WHERE w2.list_id = w1.list_id
						  AND w2.\"IPA_word\" = w1.\"IPA_word\"
						  AND w2.word <> w1.word
						  AND w2.word NOT LIKE '%% %%'
						  AND w2.word NOT LIKE 'd''%%'
						  AND w2.word NOT LIKE 'l''%%'
						  AND w2.word NOT LIKE '%%.'
						  AND w2.word NOT LIKE '%%,'
						  AND w2.word NOT LIKE '%%;'
						  AND w2.word NOT LIKE '%%:'
						  AND NOT (
							w2.word = ANY(
								regexp_split_to_array(
									CASE
										WHEN position(' | homófonos: ' in w1.association) > 0 THEN split_part(w1.association, ' | homófonos: ', 2)
										ELSE ''
									END,
									'\\s*;\\s*'
								)
							)
						  )
					)
				ELSE
					w1.association || ' | homófonos: ' || (
						SELECT string_agg(w2.word, '; ')
						FROM words w2
						WHERE w2.list_id = w1.list_id
						  AND w2.\"IPA_word\" = w1.\"IPA_word\"
						  AND w2.word <> w1.word
						  AND w2.word NOT LIKE '%% %%'
						  AND w2.word NOT LIKE 'd''%%'
						  AND w2.word NOT LIKE 'l''%%'
						  AND w2.word NOT LIKE '%%.'
						  AND w2.word NOT LIKE '%%,'
						  AND w2.word NOT LIKE '%%;'
						  AND w2.word NOT LIKE '%%:'
						  AND NOT (
							w2.word = ANY(
								regexp_split_to_array(
									CASE
										WHEN position(' | homófonos: ' in w1.association) > 0 THEN split_part(w1.association, ' | homófonos: ', 2)
										ELSE ''
									END,
									'\\s*;\\s*'
								)
							)
						  )
					)
			END
			WHERE w1.list_id = %s
			  AND w1.word = %s
			  AND w1.\"IPA_word\" = %s
			  AND EXISTS (
				SELECT 1
				FROM words w2
				WHERE w2.list_id = w1.list_id
				  AND w2.\"IPA_word\" = w1.\"IPA_word\"
				  AND w2.word <> w1.word
				  AND w2.word NOT LIKE '%% %%'
				  AND w2.word NOT LIKE 'd''%%'
				  AND w2.word NOT LIKE 'l''%%'
				  AND w2.word NOT LIKE '%%.'
				  AND w2.word NOT LIKE '%%,'
				  AND w2.word NOT LIKE '%%;'
				  AND w2.word NOT LIKE '%%:'
				  AND NOT (
					w2.word = ANY(
						regexp_split_to_array(
							CASE
								WHEN position(' | homófonos: ' in COALESCE(w1.association, '')) > 0 THEN split_part(COALESCE(w1.association, ''), ' | homófonos: ', 2)
								ELSE ''
							END,
							'\\s*;\\s*'
						)
					)
				  )
			  );
			""".strip(),
			(list_id, word, ipa),
		)
	)

	# 2) Update all other homophones' associations to include the current word.
	queries.append(
		(
			"""
			UPDATE words w
			SET association = CASE
				WHEN w.association IS NULL OR w.association = '' THEN %s
				WHEN position(' | homófonos: ' in w.association) > 0 THEN w.association || '; ' || %s
				ELSE w.association || ' | homófonos: ' || %s
			END
			WHERE w.list_id = %s
			  AND w.\"IPA_word\" = %s
			  AND w.word <> %s
			  AND w.word NOT LIKE '%% %%'
			  AND w.word NOT LIKE 'd''%%'
			  AND w.word NOT LIKE 'l''%%'
			  AND w.word NOT LIKE '%%.'
			  AND w.word NOT LIKE '%%,'
			  AND w.word NOT LIKE '%%;'
			  AND w.word NOT LIKE '%%:'
			  AND NOT (
				%s = ANY(
					regexp_split_to_array(
						CASE
							WHEN position(' | homófonos: ' in COALESCE(w.association, '')) > 0 THEN split_part(COALESCE(w.association, ''), ' | homófonos: ', 2)
							ELSE ''
						END,
						'\\s*;\\s*'
					)
				)
			  );
			""".strip(),
			(word, word, word, list_id, ipa, word, word),
		)
	)


__all__ = ["add_word", "prepare_word_actions"]
