import json
import os
import re
from pathlib import Path

import pytest

from frequency_db_utils import word_adder

TEST_USER_ID = 'user-123'

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'
LIST_PATH = SCRIPTS_DIR / 'List_test.txt'


def read_list_test(path):
    entries = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            parts = [p.strip() for p in s.split(',')]
            word = parts[0]
            lang = parts[1] if len(parts) > 1 and parts[1] else 'fr'
            list_id = int(parts[2]) if len(parts) > 2 and parts[2] else 1
            pos = parts[3].lower() if len(parts) > 3 and parts[3] else None
            entries.append((word, lang, list_id, pos))
    return entries


@pytest.mark.parametrize('entry', read_list_test(LIST_PATH))
def test_prepare_word_actions_flow(monkeypatch, entry):
    """Simula el flujo completo sin escribir en la BD.

    - Parchea `word_adder._call_openai_chat` para devolver (en orden):
      1) JSON con `translation` y `article`.
      2) IPA para la frase `article+word`.
    - Usa `prepare_word_actions` (no hace SQL) y verifica `association` e `ipa_word`.
    """
    palabra, language, list_id, pos = entry

    # Respuestas simuladas (se enrutan por el contenido del prompt)
    resp_pos = {
        'choices': [
            {'message': {'content': json.dumps({'pos': 'verb' if pos == 'verb' else 'noun'}, ensure_ascii=False)}}
        ]
    }

    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }

    resp_assoc_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'libro', 'article': 'le '}, ensure_ascii=False)}}]
    }

    resp_assoc_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'nous', 'phrase': f'nous {palabra}', 'translation': 'libro'}
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    # Provide a structurally-valid IPA for the produced phrase (we do not care about
    # phonetic correctness in this flow test).
    phrase_for_ipa = f"nous {palabra}" if pos == 'verb' else 'le ' + palabra
    # Match the new FR format: spaces between words by default; use '‿' only for real linking.
    fake_ipa = '/a b/' if ' ' in phrase_for_ipa else '/a/'
    resp_ipa = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'phrase': phrase_for_ipa,
                            'ipa': fake_ipa,
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        # Route by prompt signature to tolerate optional extra calls.
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'lemma' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_assoc_noun
        if '"tense"' in prompt and '"forms"' in prompt:
            return resp_assoc_verb
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    # Asegurarnos de que requests.get no haga llamadas reales
    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        # Si se llega a usar, devolver algo inofensivo
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    # Ejecutar la función que no escribe en BD
    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    # Comprobaciones básicas
    assert 'association' in result
    assert 'ipa_word' in result

    # association debe incluir la frase usada y la traducción 'libro'
    if pos == 'verb':
        expected_phrase = f"nous {palabra}"
    else:
        expected_phrase = 'le ' + palabra
        # En el código, para FR forzamos elisión le/la -> l' si aplica.
        if language == 'fr' and word_adder._starts_with_french_elision_sound(palabra):
                # En este test stub siempre devolvemos 'le ' como artículo, por lo que el género
                # inferido será masculino y se almacena con sufijo.
                expected_phrase = "l'" + palabra + " (m)"
    assert result['association'] == f"{expected_phrase}, libro"
    assert result['ipa_word']
    assert result['ipa_word'].startswith('/') and result['ipa_word'].endswith('/')

    # queries deben existir y contener el INSERT
    assert isinstance(result['queries'], list)
    assert any('INSERT INTO words' in q[0] for q in result['queries'])


def test_prepare_word_actions_adjective_regular_feminine_no_extra_entry(monkeypatch):
    """Si el femenino es regular (FR: +e), NO se agrega entrada adicional."""

    palabra = 'grand'
    language = 'fr'
    list_id = 10

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'adjective'}, ensure_ascii=False)}}]}
    resp_adj = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {'translation': 'grande', 'feminine': 'grande', 'feminine_translation': ''},
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    resp_ipa_main = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'grand', 'ipa': '/a/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"feminine_translation"' in prompt and 'Adjetivo:' in prompt:
            return resp_adj
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            return resp_ipa_main
        # Should not request extra IPA when no extra entries are created.
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase:' in prompt:
            raise RuntimeError('Unexpected extra IPA request for regular feminine')
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['grand']
    assert result['association'] == 'grand, grande'


def test_prepare_word_actions_adjective_irregular_feminine_adds_extra_entry(monkeypatch):
    """Si el femenino es irregular (no es solo +e), se agrega una entrada adicional."""

    palabra = 'beau'
    language = 'fr'
    list_id = 11

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'adjective'}, ensure_ascii=False)}}]}
    resp_adj = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {'translation': 'bonito', 'feminine': 'belle', 'feminine_translation': 'bonita'},
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    resp_ipa_main = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'beau', 'ipa': '/a/'}, ensure_ascii=False)}}]
    }
    resp_ipa_belle = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'belle', 'ipa': '/b/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"feminine_translation"' in prompt and 'Adjetivo:' in prompt:
            return resp_adj
        # Main IPA request uses 'Frase propuesta:'
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            return resp_ipa_main
        # Extra entry IPA request comes from _get_phrase_ipa and uses 'Frase:'
        if '"ipa"' in prompt and '"phrase"' in prompt and re.search(r"\nFrase:\s*belle\s*\n", prompt):
            return resp_ipa_belle
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['beau', 'belle']

    # Associations should match the stored word + Spanish translation.
    assert inserts[0][1][1] == 'beau, bonito'
    assert inserts[1][1][1] == 'belle, bonita'
    # Both should have IPA values.
    assert inserts[0][1][3]
    assert inserts[1][1][3]


def test_prepare_word_actions_sommes(monkeypatch):
    """Prueba específica para verbo conjugado: 'sommes' -> entradas para todos los pronombres."""

    palabra = 'sommes'
    language = 'fr'
    list_id = 22

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'être'}, ensure_ascii=False)}}]}

    assoc_json = json.dumps(
        {
            'tense': 'present',
            'forms': [
                {'pronoun': 'je', 'phrase': 'je suis', 'translation': 'soy'},
                {'pronoun': 'tu', 'phrase': 'tu es', 'translation': 'eres'},
                {'pronoun': 'il', 'phrase': 'il est', 'translation': 'es'},
                {'pronoun': 'elle', 'phrase': 'elle est', 'translation': 'es'},
                {'pronoun': 'on', 'phrase': 'on est', 'translation': 'es'},
                {'pronoun': 'nous', 'phrase': 'nous sommes', 'translation': 'somos'},
                {'pronoun': 'vous', 'phrase': 'vous êtes', 'translation': 'son'},
                {'pronoun': 'ils', 'phrase': 'ils sont', 'translation': 'son'},
                {'pronoun': 'elles', 'phrase': 'elles sont', 'translation': 'son'},
            ],
        },
        ensure_ascii=False,
    )
    resp_assoc = {'choices': [{'message': {'content': assoc_json}}]}

    # Sin agrupado esperamos 9 entradas (una por pronombre):
    # je/tu/il/elle/on/nous/vous/ils/elles
    resp_ipa_1 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'je suis', 'ipa': '/ʒə sɥi/'}, ensure_ascii=False)}}]
    }
    resp_ipa_2 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'tu es', 'ipa': '/ty ɛ/'}, ensure_ascii=False)}}]
    }
    resp_ipa_3 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'il est', 'ipa': '/il ɛ/'}, ensure_ascii=False)}}]
    }
    resp_ipa_4 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'elle est', 'ipa': '/ɛl ɛ/'}, ensure_ascii=False)}}]
    }
    resp_ipa_5 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'on est', 'ipa': '/ɔ̃ ɛ/'}, ensure_ascii=False)}}]
    }
    resp_ipa_6 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'nous sommes', 'ipa': '/nu sɔm/'}, ensure_ascii=False)}}]
    }

    resp_ipa_7 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'vous êtes', 'ipa': '/vu z‿ɛt/'}, ensure_ascii=False)}}]
    }
    resp_ipa_8 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'ils sont', 'ipa': '/il sɔ̃/'}, ensure_ascii=False)}}]
    }
    resp_ipa_9 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'elles sont', 'ipa': '/ɛl sɔ̃/'}, ensure_ascii=False)}}]
    }
    resp_ipa_inf = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'être', 'ipa': '/ɛtʁ/'}, ensure_ascii=False)}}]
    }

    responses = [
        resp_pos,
        resp_assoc,
        resp_ipa_1,
        resp_ipa_2,
        resp_ipa_3,
        resp_ipa_4,
        resp_ipa_5,
        resp_ipa_6,
        resp_ipa_7,
        resp_ipa_8,
        resp_ipa_9,
        resp_ipa_inf,
    ]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result['tense'] == 'present'
    assert result['association'] == 'je suis, soy'
    assert result['ipa_word'] == '/ʒə sɥi/'
    assert isinstance(result.get('entries'), list)
    assert len(result['entries']) == 10
    assert [e['phrase'] for e in result['entries']] == [
        'je suis',
        'tu es',
        'il est',
        'elle est',
        'on est',
        'nous sommes',
        'vous êtes',
        'ils sont',
        'elles sont',
        'être',
    ]


def test_prepare_word_actions_est(monkeypatch):
    """'est' (être, 3s) debe tratarse como verbo y expandir el paradigma + infinitivo."""

    palabra = 'est'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'être'}, ensure_ascii=False)}}]
    }

    resp_assoc = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'il', 'phrase': 'il est', 'translation': 'es'},
                                {'pronoun': 'nous', 'phrase': 'nous sommes', 'translation': 'somos'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    resp_ipa_1 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'il est', 'ipa': '/il ɛ/'}, ensure_ascii=False)}}]
    }
    resp_ipa_2 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'nous sommes', 'ipa': '/nu sɔm/'}, ensure_ascii=False)}}]
    }
    resp_ipa_inf = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'être', 'ipa': '/ɛtʁ/'}, ensure_ascii=False)}}]
    }

    responses = [resp_pos, resp_assoc, resp_ipa_1, resp_ipa_2, resp_ipa_inf]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result.get('is_noun_and_verb') is False
    assert result['pos'] == 'verb'
    assert result['verb_infinitive'] == 'être'
    assert result['tense'] == 'present'
    assert result['association'] == 'il est, es'
    assert result['ipa_word'] == '/il ɛ/'

    entries = result.get('entries') or []
    assert [e.get('phrase') for e in entries] == ['il est', 'nous sommes', 'être']

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['il est', 'nous sommes', 'être']


def test_prepare_word_actions_avoir(monkeypatch):
    """'avoir' (infinitivo) debe tratarse como verbo y expandir formas + infinitivo."""

    palabra = 'avoir'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'avoir'}, ensure_ascii=False)}}]
    }

    resp_assoc = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'je', 'phrase': "j'ai", 'translation': 'tengo'},
                                {'pronoun': 'nous', 'phrase': 'nous avons', 'translation': 'tenemos'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    resp_ipa_1 = {
        'choices': [{'message': {'content': json.dumps({'phrase': "j'ai", 'ipa': '/ʒe/'}, ensure_ascii=False)}}]
    }
    resp_ipa_2 = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'nous avons', 'ipa': '/nu z‿avɔ̃/'}, ensure_ascii=False)}}]
    }
    resp_ipa_inf = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'avoir', 'ipa': '/avwaʁ/'}, ensure_ascii=False)}}]
    }

    responses = [resp_pos, resp_assoc, resp_ipa_1, resp_ipa_2, resp_ipa_inf]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result.get('is_noun_and_verb') is False
    assert result['pos'] == 'verb'
    assert result['verb_infinitive'] == 'avoir'
    assert result['tense'] == 'present'
    assert result['association'] == "j'ai, tengo"
    assert result['ipa_word'] == '/ʒe/'

    entries = result.get('entries') or []
    assert [e.get('phrase') for e in entries] == ["j'ai", 'nous avons', 'avoir']

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ["j'ai", 'nous avons', 'avoir']


def test_etre_present_table_from_est(monkeypatch):
    """Validación (mock) contra la tabla de referencia: ÊTRE (présent)."""

    palabra = 'est'
    language = 'fr'
    list_id = 22

    ipa_by_phrase = {
        'je suis': '/ʒə sɥi/',
        'tu es': '/ty ɛ/',
        'il est': '/il ɛ/',
        'elle est': '/ɛl ɛ/',
        'on est': '/ɔ̃ ɛ/',
        'nous sommes': '/nu sɔm/',
        'vous êtes': '/vu z‿ɛt/',
        'ils sont': '/il sɔ̃/',
        'elles sont': '/ɛl sɔ̃/',
        'être': '/ɛtʁ/',
    }

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'être'}, ensure_ascii=False)}}]
    }
    resp_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'je', 'phrase': 'je suis', 'translation': 'soy'},
                                {'pronoun': 'tu', 'phrase': 'tu es', 'translation': 'eres'},
                                {'pronoun': 'il', 'phrase': 'il est', 'translation': 'es'},
                                {'pronoun': 'elle', 'phrase': 'elle est', 'translation': 'es'},
                                {'pronoun': 'on', 'phrase': 'on est', 'translation': 'es'},
                                {'pronoun': 'nous', 'phrase': 'nous sommes', 'translation': 'somos'},
                                {'pronoun': 'vous', 'phrase': 'vous êtes', 'translation': 'son'},
                                {'pronoun': 'ils', 'phrase': 'ils sont', 'translation': 'son'},
                                {'pronoun': 'elles', 'phrase': 'elles sont', 'translation': 'son'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"tense"' in prompt and '"forms"' in prompt:
            return resp_verb
        if '"ipa"' in prompt and '"phrase"' in prompt:
            # Verb-phrase IPA request includes: 'Frase propuesta: <...>'
            m = re.search(r"Frase propuesta:\s*(.+)", prompt)
            phrase = (m.group(1).strip() if m else '').strip()
            ipa = ipa_by_phrase.get(phrase)
            if not ipa:
                raise RuntimeError(f'Unexpected phrase for IPA: {phrase!r}')
            return {'choices': [{'message': {'content': json.dumps({'phrase': phrase, 'ipa': ipa}, ensure_ascii=False)}}]}
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result['pos'] == 'verb'
    assert result['tense'] == 'present'
    assert result['verb_infinitive'] == 'être'
    assert result['ipa_word'] == ipa_by_phrase['je suis']

    entries = result.get('entries') or []
    got = {e.get('phrase'): e.get('ipa') for e in entries}
    for phrase, expected_ipa in ipa_by_phrase.items():
        assert got.get(phrase) == expected_ipa


def test_avoir_present_table(monkeypatch):
    """Validación (mock) contra la tabla de referencia: AVOIR (présent)."""

    palabra = 'avoir'
    language = 'fr'
    list_id = 22

    ipa_by_phrase = {
        "j'ai": '/ʒe/',
        'tu as': '/ty a/',
        'il a': '/il a/',
        'elle a': '/ɛl a/',
        'on a': '/ɔ̃ a/',
        'nous avons': '/nu z‿avɔ̃/',
        'vous avez': '/vu z‿ave/',
        'ils ont': '/il z‿ɔ̃/',
        'elles ont': '/ɛl z‿ɔ̃/',
        'avoir': '/a.vwaʁ/',
    }

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'avoir'}, ensure_ascii=False)}}]
    }
    resp_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'je', 'phrase': "j'ai", 'translation': 'tengo'},
                                {'pronoun': 'tu', 'phrase': 'tu as', 'translation': 'tienes'},
                                {'pronoun': 'il', 'phrase': 'il a', 'translation': 'tiene'},
                                {'pronoun': 'elle', 'phrase': 'elle a', 'translation': 'tiene'},
                                {'pronoun': 'on', 'phrase': 'on a', 'translation': 'tiene'},
                                {'pronoun': 'nous', 'phrase': 'nous avons', 'translation': 'tenemos'},
                                {'pronoun': 'vous', 'phrase': 'vous avez', 'translation': 'tienen'},
                                {'pronoun': 'ils', 'phrase': 'ils ont', 'translation': 'tienen'},
                                {'pronoun': 'elles', 'phrase': 'elles ont', 'translation': 'tienen'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"tense"' in prompt and '"forms"' in prompt:
            return resp_verb
        if '"ipa"' in prompt and '"phrase"' in prompt:
            m = re.search(r"Frase propuesta:\s*(.+)", prompt)
            phrase = (m.group(1).strip() if m else '').strip()
            ipa = ipa_by_phrase.get(phrase)
            if not ipa:
                raise RuntimeError(f'Unexpected phrase for IPA: {phrase!r}')
            return {'choices': [{'message': {'content': json.dumps({'phrase': phrase, 'ipa': ipa}, ensure_ascii=False)}}]}
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result['pos'] == 'verb'
    assert result['tense'] == 'present'
    assert result['verb_infinitive'] == 'avoir'
    assert result['ipa_word'] == ipa_by_phrase["j'ai"]

    entries = result.get('entries') or []
    got = {e.get('phrase'): e.get('ipa') for e in entries}
    for phrase, expected_ipa in ipa_by_phrase.items():
        assert got.get(phrase) == expected_ipa


def test_prepare_word_actions_ete_direct_noun_verb(monkeypatch):
    """'été' puede ser noun_verb; debe insertar el sustantivo y además expandir el verbo (être)."""

    palabra = 'été'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun_verb', 'lemma': 'être'}, ensure_ascii=False)}}]
    }

    resp_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'verano', 'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]
    }

    resp_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'passé composé',
                            'forms': [
                                {'pronoun': 'je', 'phrase': "j'ai été", 'translation': 'he sido'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    resp_ipa_noun = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'été", 'ipa': '/l‿ete/'}, ensure_ascii=False)}}]
    }
    resp_ipa_1 = {
        'choices': [{'message': {'content': json.dumps({'phrase': "j'ai été", 'ipa': '/ʒe‿ete/'}, ensure_ascii=False)}}]
    }
    resp_ipa_inf = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'être', 'ipa': '/ɛtʁ/'}, ensure_ascii=False)}}]
    }

    responses = [resp_pos, resp_noun, resp_verb, resp_ipa_noun, resp_ipa_1, resp_ipa_inf]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['pos'] == 'noun_verb'
    assert result['is_noun_and_verb'] is True
    assert result['is_verb'] is True
    assert result['is_noun'] is True
    assert result['verb_infinitive'] == 'être'
    assert result['ipa_word'] == '/l‿ete/'

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['été', "j'ai été", 'être']


def test_prepare_word_actions_noun_and_verb_combined(monkeypatch):
    """Si la palabra puede ser sustantivo y verbo, el sustantivo se inserta SOLO
    (como sustantivo normal) y además se generan las entradas por pronombre del verbo.
    """

    palabra = 'marche'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [
            {'message': {'content': json.dumps({'pos': 'noun_verb', 'lemma': 'marcher'}, ensure_ascii=False)}}
        ]
    }

    resp_noun = {
        'choices': [
            {
                'message': {
                    'content': json.dumps({'translation': 'marcha', 'article': 'la '}, ensure_ascii=False)
                }
            }
        ]
    }

    resp_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'infinitive_translation': 'caminar',
                            'forms': [
                                {'pronoun': 'je', 'phrase': 'je marche', 'translation': 'camino'},
                                {'pronoun': 'nous', 'phrase': 'nous marchons', 'translation': 'caminamos'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    # Noun IPA call (noun phrase only)
    resp_ipa_noun = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'phrase': 'la marche',
                            'ipa': '/la maʁʃ/',
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    resp_ipa_je = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'je marche', 'ipa': '/ʒə maʁʃ/'}, ensure_ascii=False)}}]
    }
    resp_ipa_nous = {
        'choices': [
            {'message': {'content': json.dumps({'phrase': 'nous marchons', 'ipa': '/nu maʁʃɔ̃/'}, ensure_ascii=False)}}
        ]
    }

    resp_ipa_inf = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'marcher', 'ipa': '/maʁʃe/'}, ensure_ascii=False)}}]
    }

    responses = [resp_pos, resp_noun, resp_verb, resp_ipa_noun, resp_ipa_je, resp_ipa_nous, resp_ipa_inf]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result['is_noun'] is True
    assert result['is_noun_and_verb'] is True
    assert result['pos'] == 'noun_verb'
    assert result['verb_infinitive'] == 'marcher'
    assert result['ipa_word'] == '/la maʁʃ/'
    assert result['association'] == 'la marche, marcha'

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['marche', 'je marche', 'nous marchons', 'marcher']


def test_prepare_word_actions_mange_fallback_verb_check(monkeypatch):
    """Regresión: 'mange' es forma conjugada (manger) y NO debe forzarse a 'le mange'.

    Simula que el primer POS devuelve noun (equivocado), pero el segundo chequeo lo corrige a verb.
    """

    palabra = 'mange'
    language = 'fr'
    list_id = 22

    # 1) POS (mal): noun
    resp_pos_bad = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    # 1b) fallback verb-check: is_verb true
    resp_verb_check = {
        'choices': [{'message': {'content': json.dumps({'is_verb': True, 'lemma': 'manger'}, ensure_ascii=False)}}]
    }

    # 1c) verb paradigm
    resp_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'je', 'phrase': 'je mange', 'translation': 'como'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    # 2) IPA for the one verb entry
    resp_ipa = {
        'choices': [
            {'message': {'content': json.dumps({'phrase': 'je mange', 'ipa': '/ʒə mɑ̃ʒ/'}, ensure_ascii=False)}}
        ]
    }
    resp_ipa_inf = {
        'choices': [
            {'message': {'content': json.dumps({'phrase': 'manger', 'ipa': '/mɑ̃ʒe/'}, ensure_ascii=False)}}
        ]
    }

    responses = [resp_pos_bad, resp_verb_check, resp_verb, resp_ipa, resp_ipa_inf]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['is_verb'] is True
    assert result.get('is_noun_and_verb') is False
    assert result['pos'] == 'verb'
    assert result['verb_infinitive'] == 'manger'
    # Ensure we didn't treat it as noun/article phrase
    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['je mange', 'manger']


def test_prepare_word_actions_ete_when_noun_then_also_verb(monkeypatch):
    """Si 'été' viene como noun, pero el verb-check confirma verbo, debe tratarse como noun_verb:
    insertar el sustantivo solo (como sustantivo normal) + generar formas conjugadas.
    """

    palabra = 'été'
    language = 'fr'
    list_id = 22

    # 1) POS devuelve noun (caso problemático)
    resp_pos_noun = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    # 1b) verb-check confirma verbo
    resp_verb_check = {
        'choices': [{'message': {'content': json.dumps({'is_verb': True, 'lemma': 'être'}, ensure_ascii=False)}}]
    }

    # 1c) noun details (se deben pedir porque es noun_verb)
    resp_noun = {
        # Model might return 'le' incorrectly; we enforce elision in code.
        'choices': [{'message': {'content': json.dumps({'translation': 'verano', 'article': 'le '}, ensure_ascii=False)}}]
    }

    # 1d) verb paradigm
    resp_verb = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'passé composé',
                            'infinitive_translation': 'ser/estar',
                            'forms': [
                                {'pronoun': 'je', 'phrase': "j'ai été", 'translation': 'he sido'},
                                {'pronoun': 'nous', 'phrase': 'nous avons été', 'translation': 'hemos sido'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    # 2) noun IPA call (noun phrase only)
    resp_ipa_noun = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'phrase': "l'été",
                            'ipa': '/l‿ete/',
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    # 2) IPA per verb phrase
    resp_ipa_1 = {
        'choices': [{'message': {'content': json.dumps({'phrase': "j'ai été", 'ipa': '/ʒe‿ete/'}, ensure_ascii=False)}}]
    }
    resp_ipa_2 = {
        'choices': [
            {'message': {'content': json.dumps({'phrase': 'nous avons été', 'ipa': '/nu z‿avɔ̃‿z‿ete/'}, ensure_ascii=False)}}
        ]
    }

    # Extra response in case the code issues an extra IPA call.
    resp_ipa_2_retry = resp_ipa_2
    resp_ipa_inf = {
        'choices': [
            {'message': {'content': json.dumps({'phrase': 'être', 'ipa': '/ɛtʁ/'}, ensure_ascii=False)}}
        ]
    }

    responses = [
        resp_pos_noun,
        resp_verb_check,
        resp_noun,
        resp_verb,
        resp_ipa_noun,
        resp_ipa_1,
        resp_ipa_2,
        resp_ipa_2_retry,
        resp_ipa_inf,
    ]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['pos'] == 'noun_verb'
    assert result['is_noun_and_verb'] is True
    assert result['verb_infinitive'] == 'être'
    assert "l'été" in (result.get('association') or '')
    assert result['ipa_word'] == '/l‿ete/'

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['été', "j'ai été", 'nous avons été', 'être']


def test_add_word_links_homophones_both_sides(monkeypatch):
    """If a word shares the same IPA with an existing word in the list,
    append the homophone at the end of association for BOTH words.
    """

    class FakeCursor:
        def __init__(self):
            self.rows = {}  # (word, list_id) -> dict
            self._next_id = 1
            self._result = []

        def _ensure_row(self, word, list_id):
            key = (word, list_id)
            if key not in self.rows:
                self.rows[key] = {"id": self._next_id, "association": None, "ipa": None}
                self._next_id += 1
            return self.rows[key]

        def execute(self, sql, params=None):
            s = " ".join(str(sql).split())
            params = params or ()

            if "INSERT INTO words" in s:
                word, association, list_id, ipa = params
                key = (word, list_id)
                if key not in self.rows:
                    self.rows[key] = {"id": self._next_id, "association": association, "ipa": ipa}
                    self._next_id += 1
                self._result = []
                return

            if s.startswith('UPDATE words SET association ='):
                association, word, list_id = params
                row = self._ensure_row(word, list_id)
                row["association"] = association
                self._result = []
                return

            if 'UPDATE words SET "IPA_word"' in s:
                ipa, word, list_id = params
                row = self._ensure_row(word, list_id)
                row["ipa"] = ipa
                self._result = []
                return

            if s.startswith('SELECT id FROM words WHERE word ='):
                word, list_id = params
                row = self.rows.get((word, list_id))
                self._result = [(row["id"],)] if row else []
                return

            if s.startswith('SELECT association FROM words WHERE list_id ='):
                list_id, word = params
                row = self.rows.get((word, list_id))
                self._result = [(row["association"],)] if row else []
                return

            if s.startswith('SELECT word, association FROM words WHERE list_id ='):
                list_id, ipa, word = params
                out = []
                for (w, lid), row in self.rows.items():
                    if lid != list_id:
                        continue
                    if w == word:
                        continue
                    if " " in w:
                        continue
                    if row.get("ipa") == ipa:
                        out.append((w, row.get("association")))
                self._result = out
                return

            raise RuntimeError(f"Unexpected SQL in FakeCursor: {sql}")

        def fetchone(self):
            if not self._result:
                return None
            return self._result.pop(0)

        def fetchall(self):
            out = list(self._result)
            self._result = []
            return out

    fake_cur = FakeCursor()
    list_id = 22

    # Existing word in list with same IPA (homophone)
    fake_cur._ensure_row("vers", list_id)
    fake_cur.rows[("vers", list_id)]["ipa"] = "/vɛʁ/"
    fake_cur.rows[("vers", list_id)]["association"] = "vers, hacia"

    def fake_prepare_word_actions(*, palabra, list_id, language, openai_api_key=None, user_id=None, rejected_words_table=None):
        assert palabra == "verre"
        return {
            "queries": [
                (
                    "INSERT INTO words (word, used, association, state, list_id, successes, \"IPA_word\", added) VALUES (%s, FALSE, %s, 'New', %s, 0, %s, TRUE) ON CONFLICT (word, list_id) DO NOTHING;",
                    ("verre", "verre, vidrio", list_id, "/vɛʁ/"),
                )
            ],
            "is_verb": False,
            "is_noun_and_verb": False,
            "association": "verre, vidrio",
            "ipa_word": "/vɛʁ/",
        }

    monkeypatch.setattr(word_adder, "prepare_word_actions", fake_prepare_word_actions)

    out = word_adder.add_word(conn=None, cur=fake_cur, palabra="verre", list_id=list_id, language="fr", openai_api_key="fake", user_id=TEST_USER_ID)
    assert out["is_verb"] is False
    assert isinstance(out.get("runtime_signature"), dict)
    assert out["runtime_signature"].get("build_id")

    assert fake_cur.rows[("verre", list_id)]["association"] == "verre, vidrio | homófonos: vers"
    assert fake_cur.rows[("vers", list_id)]["association"] == "vers, hacia | homófonos: verre"


def test_prepare_word_actions_includes_homophone_sql_for_nouns(monkeypatch):
    """Regression: homophone-linking must run even when callers only execute
    `prepare_word_actions(...)["queries"]` (i.e., without calling `add_word`).

    We assert that noun flows include the SQL UPDATE statements that use
    `regexp_split_to_array` to append homophones.
    """

    palabra = 'amie'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    # Fallback verb-check should be false for this noun.
    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }

    resp_assoc_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'amiga', 'article': "l'", 'base_article': 'la', 'gender': 'f'}, ensure_ascii=False)}}]
    }
    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'amie", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_assoc_noun
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    sqls = [q[0] for q in result.get('queries', [])]

    # Must include homophone-linking updates (safe suffix format)
    assert any('regexp_split_to_array' in s for s in sqls)
    assert any('UPDATE words w1' in s for s in sqls)
    assert any('UPDATE words w' in s for s in sqls)
    assert any('homófonos' in s for s in sqls)

    # Must exclude non-lexical variants like d'erreurs. from homophone candidates.
    combined = "\n".join(sqls)
    assert "NOT LIKE 'd''%%'" in combined
    assert "NOT LIKE 'l''%%'" in combined
    assert "NOT LIKE '%%.'" in combined


def test_prepare_word_actions_does_not_include_homophone_sql_for_pure_verbs(monkeypatch):
    """Homophone linking is intended for base 'word' entries, not multi-word verb phrases."""

    palabra = 'sommes'
    language = 'fr'
    list_id = 22

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'être'}, ensure_ascii=False)}}]}
    resp_assoc = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'tense': 'present',
                            'forms': [
                                {'pronoun': 'nous', 'phrase': 'nous sommes', 'translation': 'somos'},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'nous sommes', 'ipa': '/nu sɔm/'}, ensure_ascii=False)}}]
    }
    resp_ipa_inf = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'être', 'ipa': '/ɛtʁ/'}, ensure_ascii=False)}}]
    }
    responses = [resp_pos, resp_assoc, resp_ipa, resp_ipa_inf]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    sqls = [q[0] for q in result.get('queries', [])]
    assert not any('regexp_split_to_array' in s for s in sqls)


def test_simple_ipa_from_text_strips_square_brackets():
    # No post-processing: bracketed IPA is rejected.
    assert word_adder._simple_ipa_from_text('[l‿a.mi]') is None
    assert word_adder._simple_ipa_from_text('/[l‿a.mi]/') is None
    assert word_adder._simple_ipa_from_text('  [l‿a.mi]  ') is None


def test_simple_ipa_from_text_normalizes_double_slashes():
    # No post-processing: malformed wrapping is rejected.
    assert word_adder._simple_ipa_from_text('/ɡʁɑ̃//') is None
    assert word_adder._simple_ipa_from_text('///ɡʁɑ̃///') is None


def test_prepare_word_actions_phrase_mode_inserts_tokens_and_phrase_mes_amis(monkeypatch):
    phrase = 'mes amis'
    language = 'fr'
    list_id = 22

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])

        # Phrase-level translation
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt) and 'translation' in prompt and 'frase' in prompt.lower():
            return {
                'choices': [{'message': {'content': json.dumps({'translation': 'mis amigos'}, ensure_ascii=False)}}]
            }

        # Phrase-level IPA
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt or 'Frase:' in prompt) and 'ipa' in prompt and '"phrase"' in prompt:
            return {
                'choices': [
                    {
                        'message': {
                            'content': json.dumps({'phrase': phrase, 'ipa': '/me z‿ami/'}, ensure_ascii=False)
                        }
                    }
                ]
            }

        # POS classification per token
        if '"pos"' in prompt and 'lemma' in prompt:
            if 'Palabra: mes' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'other'}, ensure_ascii=False)}}]}
            if 'Palabra: amis' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}

        # Token translation (other)
        if '"translation"' in prompt and '"article"' not in prompt and 'Palabra: mes' in prompt:
            return {
                'choices': [{'message': {'content': json.dumps({'translation': 'mis'}, ensure_ascii=False)}}]
            }

        # Token translation+article (noun)
        # Note: noun plurals are normalized to singular (amis -> ami).
        if '"translation"' in prompt and '"article"' in prompt and ("La palabra es 'amis'" in prompt or "La palabra es 'ami'" in prompt):
            return {
                'choices': [{'message': {'content': json.dumps({'translation': 'amigo', 'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]
            }

        # Token IPA requests
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            if 'Palabra: mes' in prompt:
                return {
                    'choices': [{'message': {'content': json.dumps({'phrase': 'mes', 'ipa': '/me/'}, ensure_ascii=False)}}]
                }
            if 'Palabra: amis' in prompt or 'Palabra: ami' in prompt:
                # For noun token, we store singular: "l'ami (m)"
                return {
                    'choices': [
                        {'message': {'content': json.dumps({'phrase': "l'ami", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}
                    ]
                }

        raise RuntimeError(f'Unexpected fake_call prompt: {prompt[:2000]}')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    # Avoid network fallback
    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(phrase, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result.get('is_phrase') is True
    assert result.get('word') == phrase
    assert result.get('ipa_word') == '/me z‿ami/'
    assert result.get('association') == 'mes amis, mis amigos'

    # Ensure INSERTs exist for phrase and for tokens
    inserts = [params for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    inserted_words = {p[0] for p in inserts}
    assert 'mes' in inserted_words
    assert 'ami' in inserted_words
    assert 'mes amis' in inserted_words


def test_prepare_word_actions_phrase_mode_handles_verb_token_and_phrase(monkeypatch):
    phrase = 'je mange'
    language = 'fr'
    list_id = 22

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])

        # Phrase-level translation
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt) and 'translation' in prompt and 'frase' in prompt.lower():
            return {
                'choices': [{'message': {'content': json.dumps({'translation': 'yo como'}, ensure_ascii=False)}}]
            }

        # Phrase-level IPA
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt or 'Frase:' in prompt) and 'ipa' in prompt and '"phrase"' in prompt:
            return {
                'choices': [
                    {
                        'message': {
                            'content': json.dumps({'phrase': phrase, 'ipa': '/ʒə mɑ̃ʒ/'}, ensure_ascii=False)
                        }
                    }
                ]
            }

        # POS classification per token
        if '"pos"' in prompt and 'lemma' in prompt:
            if 'Palabra: je' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'other'}, ensure_ascii=False)}}]}
            if 'Palabra: mange' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'manger'}, ensure_ascii=False)}}]}

        # Token translation (other)
        if '"translation"' in prompt and '"article"' not in prompt and 'Palabra: je' in prompt:
            return {
                'choices': [{'message': {'content': json.dumps({'translation': 'yo'}, ensure_ascii=False)}}]
            }

        # Verb paradigm (phrase mode should still expand conjugations)
        if '"tense"' in prompt and '"forms"' in prompt and f'Entrada: mange' in prompt:
            return {
                'choices': [
                    {
                        'message': {
                            'content': json.dumps(
                                {
                                    'tense': 'present',
                                    'forms': [{'pronoun': 'nous', 'phrase': 'nous mangeons', 'translation': 'comemos'}],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        # Token IPA requests
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            if 'Palabra: je' in prompt:
                return {
                    'choices': [{'message': {'content': json.dumps({'phrase': 'je', 'ipa': '/ʒə/'}, ensure_ascii=False)}}]
                }
            if 'Frase propuesta: nous mangeons' in prompt:
                return {
                    'choices': [
                        {'message': {'content': json.dumps({'phrase': 'nous mangeons', 'ipa': '/nu mɑ̃ʒɔ̃/'}, ensure_ascii=False)}}
                    ]
                }
            if 'Frase propuesta: manger' in prompt:
                return {
                    'choices': [
                        {'message': {'content': json.dumps({'phrase': 'manger', 'ipa': '/mɑ̃ʒe/'}, ensure_ascii=False)}}
                    ]
                }

        raise RuntimeError(f'Unexpected fake_call prompt: {prompt[:2000]}')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(phrase, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result.get('is_phrase') is True
    assert result.get('word') == phrase
    assert result.get('ipa_word') == '/ʒə mɑ̃ʒ/'
    assert result.get('association') == 'je mange, yo como'

    inserts = [params for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    inserted_words = {p[0] for p in inserts}
    # token inserts
    assert 'je' in inserted_words
    # verb logic produces a conjugated phrase insert
    assert 'nous mangeons' in inserted_words
    # and also inserts the infinitive
    assert 'manger' in inserted_words
    # full phrase insert
    assert 'je mange' in inserted_words


def test_prepare_word_actions_phrase_mode_dedupes_phrase_insert_when_already_in_verb_forms(monkeypatch):
    phrase = 'je mange'
    language = 'fr'
    list_id = 22

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])

        # Phrase-level translation
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt) and 'translation' in prompt and 'frase' in prompt.lower():
            return {'choices': [{'message': {'content': json.dumps({'translation': 'yo como'}, ensure_ascii=False)}}]}

        # Phrase-level IPA
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt or 'Frase:' in prompt) and 'ipa' in prompt and '"phrase"' in prompt:
            return {'choices': [{'message': {'content': json.dumps({'phrase': phrase, 'ipa': '/ʒə mɑ̃ʒ/'}, ensure_ascii=False)}}]}

        # POS per token
        if '"pos"' in prompt and 'lemma' in prompt:
            if 'Palabra: je' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'other'}, ensure_ascii=False)}}]}
            if 'Palabra: mange' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'verb', 'lemma': 'manger'}, ensure_ascii=False)}}]}

        # Token translation other
        if '"translation"' in prompt and '"article"' not in prompt and 'Palabra: je' in prompt:
            return {'choices': [{'message': {'content': json.dumps({'translation': 'yo'}, ensure_ascii=False)}}]}

        # Verb paradigm: includes the SAME phrase as one of the forms
        if '"tense"' in prompt and '"forms"' in prompt and 'Entrada: mange' in prompt:
            return {
                'choices': [
                    {
                        'message': {
                            'content': json.dumps(
                                {
                                    'tense': 'present',
                                    'forms': [
                                        {'pronoun': 'je', 'phrase': 'je mange', 'translation': 'yo como'},
                                        {'pronoun': 'nous', 'phrase': 'nous mangeons', 'translation': 'comemos'},
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

        # IPA for verb forms
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            if 'Palabra: je' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'phrase': 'je', 'ipa': '/ʒə/'}, ensure_ascii=False)}}]}
            if 'Frase propuesta: je mange' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'phrase': 'je mange', 'ipa': '/ʒə mɑ̃ʒ/'}, ensure_ascii=False)}}]}
            if 'Frase propuesta: nous mangeons' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'phrase': 'nous mangeons', 'ipa': '/nu mɑ̃ʒɔ̃/'}, ensure_ascii=False)}}]}
            if 'Frase propuesta: manger' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'phrase': 'manger', 'ipa': '/mɑ̃ʒe/'}, ensure_ascii=False)}}]}

        raise RuntimeError(f'Unexpected fake_call prompt: {prompt[:2000]}')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(phrase, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    inserts = [params for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    phrase_inserts = [p for p in inserts if p[0] == phrase and p[2] == list_id]
    assert len(phrase_inserts) == 1


def test_prepare_word_actions_rejected_words_guard_adds_not_exists(monkeypatch):
    """When user_id + rejected_words_table are provided, generated SQL must guard against rejected words."""

    palabra = 'ami'
    language = 'fr'
    list_id = 22

    # Minimal OpenAI stubs: noun + article + IPA
    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
    resp_noun = {'choices': [{'message': {'content': json.dumps({'translation': 'amigo', 'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]}
    resp_ipa = {'choices': [{'message': {'content': json.dumps({'phrase': "l'ami", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]}

    responses = [resp_pos, resp_noun, resp_ipa]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(
        palabra,
        list_id,
        language,
        openai_api_key='fake',
        user_id='user-123',
        rejected_words_table='palabras_rechazadas',
    )

    insert_queries = [(sql, params) for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    assert insert_queries, 'Expected at least one INSERT query'
    for sql, params in insert_queries:
        assert 'NOT EXISTS' in sql
        assert 'FROM palabras_rechazadas' in sql
        # Ensure user_id/language/word are included in params tail.
        assert params[-3:] == ('user-123', 'fr', params[0])


def test_prepare_word_actions_rejected_words_guard_default_table(monkeypatch):
    """If user_id is provided but rejected_words_table is omitted, default to 'palabras_rechazadas'."""

    palabra = 'ami'
    language = 'FR'  # intentionally uppercase to verify normalization
    list_id = 22

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
    resp_noun = {'choices': [{'message': {'content': json.dumps({'translation': 'amigo', 'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]}
    resp_ipa = {'choices': [{'message': {'content': json.dumps({'phrase': "l'ami", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]}
    responses = [resp_pos, resp_noun, resp_ipa]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(
        palabra,
        list_id,
        language,
        openai_api_key='fake',
        user_id='user-123',
        # rejected_words_table omitted on purpose
    )
    insert_queries = [(sql, params) for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    assert insert_queries
    assert any('FROM palabras_rechazadas' in sql and 'NOT EXISTS' in sql for (sql, _params) in insert_queries)
    # language should be normalized to lowercase for the DB check
    sql0, params0 = insert_queries[0]
    assert 'FROM palabras_rechazadas' in sql0
    assert 'NOT EXISTS' in sql0
    assert params0[-2] == 'fr'


def test_prepare_word_actions_noun_plural_is_normalized_to_singular(monkeypatch):
    """French nouns: by default we store only the singular.

    If the input looks like a regular plural (-s or -eaux), we normalize to a
    singular candidate and insert that instead of the plural.
    """

    palabra = 'livres'
    language = 'fr'
    list_id = 22

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
    # 'livres' ends with 'es' and triggers the fallback verb-check heuristic; ensure it's false.
    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }
    # After normalization, noun prompt is for 'livre' (singular), so article should be singular.
    resp_noun = {'choices': [{'message': {'content': json.dumps({'translation': 'libro', 'article': 'le '}, ensure_ascii=False)}}]}
    resp_ipa = {
        'choices': [
            {
                'message': {
                    'content': json.dumps({'phrase': 'le livre', 'ipa': '/a b/'}, ensure_ascii=False)
                }
            }
        ]
    }
    responses = [resp_pos, resp_verb_check_false, resp_noun, resp_ipa]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)

    assert result['word'] == 'livre'
    insert_queries = [(sql, params) for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    assert insert_queries
    _sql0, params0 = insert_queries[0]
    assert params0[0] == 'livre'
    assert params0[-3:] == (TEST_USER_ID, 'fr', 'livre')


def test_prepare_word_actions_noun_irregular_plural_allows_both(monkeypatch):
    """If the plural is irregular (e.g. ends with -aux), we do not attempt a
    singular guard and allow inserting the plural even if a true singular might
    already exist.

    This follows the requirement: for irregular noun plurals whose plural form
    is not safely derivable from the singular, keep both.
    """

    palabra = 'travaux'
    language = 'fr'
    list_id = 22

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
    # 'travaux' ends with 'aux' and should NOT trigger any singular candidate.
    resp_noun = {'choices': [{'message': {'content': json.dumps({'translation': 'trabajos', 'article': 'les'}, ensure_ascii=False)}}]}
    resp_ipa = {
        'choices': [
            {
                'message': {
                    'content': json.dumps({'phrase': 'les travaux', 'ipa': '/a b/'}, ensure_ascii=False)
                }
            }
        ]
    }
    responses = [resp_pos, resp_noun, resp_ipa]

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        if not responses:
            raise RuntimeError('No more fake responses')
        return responses.pop(0)

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    insert_queries = [(sql, params) for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    assert insert_queries
    sql0, params0 = insert_queries[0]

    # For irregular plurals (-aux), we do not attempt singularization.
    assert result['word'] == 'travaux'
    assert 'FROM palabras_rechazadas' in sql0
    assert params0[-3:] == (TEST_USER_ID, 'fr', palabra)


def test_prepare_word_actions_rejected_words_guard_requires_user_id():
    palabra = 'je'
    language = 'FR'
    list_id = 22

    with pytest.raises(ValueError, match='user_id is required'):
        word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake')


def test_prepare_phrase_actions_rejected_words_guard_adds_not_exists(monkeypatch):
    phrase = 'mes amis'
    language = 'fr'
    list_id = 22

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        # Phrase translation
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt) and 'translation' in prompt and 'frase' in prompt.lower():
            return {'choices': [{'message': {'content': json.dumps({'translation': 'mis amigos'}, ensure_ascii=False)}}]}
        # Phrase IPA
        if ('Frase completa:' in prompt or 'Frase propuesta:' in prompt or 'Frase:' in prompt) and 'ipa' in prompt and '"phrase"' in prompt:
            return {'choices': [{'message': {'content': json.dumps({'phrase': phrase, 'ipa': '/me z‿ami/'}, ensure_ascii=False)}}]}
        # Token POS
        if '"pos"' in prompt and 'lemma' in prompt:
            if 'Palabra: mes' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'other'}, ensure_ascii=False)}}]}
            if 'Palabra: amis' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
        # Token translation other
        if '"translation"' in prompt and '"article"' not in prompt and 'Palabra: mes' in prompt:
            return {'choices': [{'message': {'content': json.dumps({'translation': 'mis'}, ensure_ascii=False)}}]}
        # Token noun details
        # Note: noun plurals are normalized to singular (amis -> ami).
        if '"translation"' in prompt and '"article"' in prompt and ("La palabra es 'amis'" in prompt or "La palabra es 'ami'" in prompt):
            return {'choices': [{'message': {'content': json.dumps({'translation': 'amigo', 'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]}
        # Token IPA
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            if 'Palabra: mes' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'phrase': 'mes', 'ipa': '/me/'}, ensure_ascii=False)}}]}
            if 'Palabra: amis' in prompt or 'Palabra: ami' in prompt:
                return {'choices': [{'message': {'content': json.dumps({'phrase': "l'ami", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]}
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(
        phrase,
        list_id,
        language,
        openai_api_key='fake',
        user_id='user-123',
        rejected_words_table='palabras_rechazadas',
    )

    insert_queries = [(sql, params) for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    assert insert_queries
    # Phrase insert should also be guarded.
    phrase_insert = [q for q in insert_queries if q[1][0] == phrase]
    assert phrase_insert
    for sql, params in insert_queries:
        assert 'NOT EXISTS' in sql
        assert 'FROM palabras_rechazadas' in sql
def test_prepare_word_actions_fr_adverb_tard_no_article_even_if_model_wrong(monkeypatch):
    palabra = 'tard'
    language = 'fr'
    list_id = 22

    # Model wrongly says noun_verb; our override must force 'other'.
    resp_pos_wrong = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun_verb'}, ensure_ascii=False)}}]
    }

    # For non-noun/non-verb, we should call the translation-only prompt.
    resp_other_translation = {
        'choices': [{'message': {'content': json.dumps({'translation': 'tarde'}, ensure_ascii=False)}}]
    }

    # IPA should be for the bare word (no article).
    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'tard', 'ipa': '/taʁ/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos_wrong
        # We must NOT ask for article for 'tard'.
        if '"translation"' in prompt and '"article"' in prompt:
            raise RuntimeError('Should not request noun translation/article for adverb tard')
        if 'un campo: "translation"' in prompt:
            return resp_other_translation
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('pos') == 'other'
    assert result.get('is_noun') is False
    assert result.get('is_verb') is False
    assert result.get('association') == 'tard, tarde'
    assert result.get('ipa_word') == '/taʁ/'


def test_prepare_word_actions_fr_adjective_no_article(monkeypatch):
    palabra = 'grand'
    language = 'fr'
    list_id = 22

    resp_pos_adj = {
        'choices': [{'message': {'content': json.dumps({'pos': 'adjective'}, ensure_ascii=False)}}]
    }

    resp_adj_details = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {'translation': 'grande', 'feminine': 'grande', 'feminine_translation': ''},
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'grand', 'ipa': '/gʁɑ̃/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos_adj
        if '"translation"' in prompt and '"article"' in prompt:
            raise RuntimeError('Should not request noun translation/article for adjective')
        if '"feminine_translation"' in prompt and 'Adjetivo:' in prompt:
            return resp_adj_details
        if 'un campo: "translation"' in prompt:
            raise RuntimeError('Should not request generic translation-only prompt for adjective')
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('pos') == 'adjective'
    assert result.get('is_noun') is False
    assert result.get('is_verb') is False
    assert result.get('association') == 'grand, grande'
    assert result.get('ipa_word') == '/gʁɑ̃/'


def test_prepare_word_actions_fr_adverb_mente_translation_never_gets_article(monkeypatch):
    """Si un adverbio se confunde como sustantivo y viene con artículo (l'/le/la/les), se debe corregir.

    Heurística: si la traducción al español termina en '-mente', tratamos como 'other' y no usamos artículo.
    """

    palabra = 'rapidement'
    language = 'fr'
    list_id = 23

    # Simulate a wrong POS classification: noun.
    resp_pos_noun = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    # Simulate the noun-details call returning an (incorrect) definite article.
    resp_assoc_noun_wrong = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {'translation': 'rápidamente', 'article': "l'", 'base_article': 'le', 'gender': 'm'},
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }

    # IPA request should be for the bare word (no article).
    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': 'rapidement', 'ipa': '/a/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos_noun
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_assoc_noun_wrong
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('pos') == 'other'
    assert result.get('is_noun') is False
    assert result.get('association') == 'rapidement, rápidamente'

    inserts = [q for q in result['queries'] if 'INSERT INTO words' in q[0]]
    assert [ins[1][0] for ins in inserts] == ['rapidement']


def test_prepare_word_actions_noun_ipa_brackets_are_normalized(monkeypatch):
    palabra = 'amie'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    # Fallback verb-check should be false for this noun.
    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }

    resp_assoc_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'amiga', 'article': "l'", 'base_article': 'la', 'gender': 'f'}, ensure_ascii=False)}}]
    }

    # Return bracketed IPA to ensure we do NOT normalize it.
    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'amie", 'ipa': '[l‿a.mi]'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_assoc_noun
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('ipa_word') is None


def test_prepare_word_actions_noun_translation_strips_spanish_determiner(monkeypatch):
    palabra = 'ami'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    # Fallback verb-check should be false for this noun.
    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }

    # Model returns translation with a Spanish determiner; code should strip it.
    resp_assoc_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'un amigo', 'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]
    }

    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'ami", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_assoc_noun
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('association') == "l'ami (m), amigo"


def test_prepare_word_actions_fr_noun_retries_missing_article(monkeypatch):
    """If the model omits the French definite article for a noun, we retry once.

    This prevents associations like "ami, amigo" for common nouns.
    """

    palabra = 'ami'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }

    # First noun details response wrongly omits article.
    resp_noun_missing_article = {
        'choices': [{'message': {'content': json.dumps({'translation': 'amigo', 'article': ''}, ensure_ascii=False)}}]
    }

    # Retry returns definite article.
    resp_article_retry = {
        'choices': [{'message': {'content': json.dumps({'article': "l'", 'base_article': 'le', 'gender': 'm'}, ensure_ascii=False)}}]
    }

    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'ami", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_noun_missing_article
        if 'Devuélveme SOLO JSON válido con campos: "article", "base_article" y "gender"' in prompt:
            return resp_article_retry
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('association') == "l'ami (m), amigo"


def test_prepare_word_actions_queries_have_no_unescaped_percent_and_match_params(monkeypatch):
    """Regression: psycopg2 uses '%' for param interpolation.

    Any literal percent in SQL must be escaped as '%%'. Otherwise psycopg2 can
    raise IndexError: tuple index out of range during execute/mogrify.
    """

    palabra = 'amie'
    language = 'fr'
    list_id = 22

    resp_pos = {
        'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]
    }

    resp_verb_check_false = {
        'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]
    }

    resp_assoc_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'amiga', 'article': "l'", 'base_article': 'la', 'gender': 'f'}, ensure_ascii=False)}}]
    }

    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'amie", 'ipa': '/l‿ami/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            return resp_assoc_noun
        if '"ipa"' in prompt and '"phrase"' in prompt:
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    def fake_get(url, timeout=5):
        return DummyResp(status=404, data=[])

    monkeypatch.setattr(word_adder.requests, 'get', fake_get)

    def has_invalid_percent(sql: str) -> bool:
        i = 0
        while i < len(sql):
            if sql[i] != '%':
                i += 1
                continue
            # '%' must be followed by 's' (placeholder) or '%' (escaped literal).
            if i + 1 >= len(sql):
                return True
            nxt = sql[i + 1]
            if nxt in {'s', '%'}:
                i += 2
                continue
            return True
        return False

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    for sql, params in result.get('queries', []):
        assert has_invalid_percent(sql) is False
        assert sql.count('%s') == len(params)


def test_prepare_word_actions_normalizes_punctuation_and_d_contraction_single_token(monkeypatch):
    """Inputs like d’erreurs. should not create distinct entries nor keep d' as a word."""

    palabra = "d’erreurs."  # curly apostrophe + trailing punctuation
    language = 'fr'
    list_id = 22

    resp_pos = {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
    resp_verb_check_false = {'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]}
    # After normalization: d’erreurs. -> d'erreurs -> erreurs -> (singularized) erreur
    resp_noun = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {'translation': "error, d'erreurs", 'article': "l'", 'base_article': 'la', 'gender': 'f'},
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    resp_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'erreur", 'ipa': '/l‿eʁœʁ/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false
        if '"pos"' in prompt and 'lemma' in prompt:
            # POS is asked for the normalized token (erreurs)
            assert 'Palabra: erreurs' in prompt
            return resp_pos
        if '"translation"' in prompt and '"article"' in prompt:
            # Noun details should be asked for singularized token (erreur)
            assert "La palabra es 'erreur'" in prompt
            return resp_noun
        if '"ipa"' in prompt and '"phrase"' in prompt:
            assert "Frase propuesta: l'erreur" in prompt
            return resp_ipa
        raise RuntimeError('Unexpected fake_call prompt')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(palabra, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('word') == 'erreur'
    assert result.get('association') == "l'erreur (f), error"

    # Also ensure the association UPDATE query would fix previously-bad stored values like
    # "l'erreur (f), error, d'erreurs" by triggering when association has multiple commas.
    upd_assocs = [q for q in result.get('queries', []) if q[0].startswith('UPDATE words SET association')]
    assert upd_assocs
    assert "association LIKE '%%,%%,%%'" in upd_assocs[0][0]


def test_prepare_phrase_actions_normalizes_punctuation_and_d_contraction(monkeypatch):
    """Phrase punctuation should be normalized and d' contractions should not become separate word inserts."""

    phrase = "beaucoup d’erreurs."  # trailing punctuation + curly apostrophe
    language = 'fr'
    list_id = 22

    resp_phrase_translation = {
        'choices': [{'message': {'content': json.dumps({'translation': 'muchos errores'}, ensure_ascii=False)}}]
    }
    resp_phrase_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "beaucoup d'erreurs", 'ipa': '/a b/'}, ensure_ascii=False)}}]
    }

    # Token flows
    resp_pos_other = {'choices': [{'message': {'content': json.dumps({'pos': 'other'}, ensure_ascii=False)}}]}
    resp_other_translation = {'choices': [{'message': {'content': json.dumps({'translation': 'mucho'}, ensure_ascii=False)}}]}
    resp_other_ipa = {'choices': [{'message': {'content': json.dumps({'phrase': 'beaucoup', 'ipa': '/bo.ku/'}, ensure_ascii=False)}}]}

    resp_pos_noun = {'choices': [{'message': {'content': json.dumps({'pos': 'noun'}, ensure_ascii=False)}}]}
    resp_verb_check_false = {'choices': [{'message': {'content': json.dumps({'is_verb': False}, ensure_ascii=False)}}]}
    resp_noun = {
        'choices': [{'message': {'content': json.dumps({'translation': 'error', 'article': "l'", 'base_article': 'la', 'gender': 'f'}, ensure_ascii=False)}}]
    }
    resp_noun_ipa = {
        'choices': [{'message': {'content': json.dumps({'phrase': "l'erreur", 'ipa': '/l‿eʁœʁ/'}, ensure_ascii=False)}}]
    }

    def fake_call(api_key, messages, max_tokens=120, timeout=30, **kwargs):
        prompt = "\n".join([m.get('content', '') for m in (messages or [])])

        # Phrase-level translation/IPA use the normalized phrase.
        if 'Frase completa:' in prompt and '"translation"' in prompt:
            assert "Frase completa: beaucoup d'erreurs" in prompt
            return resp_phrase_translation
        if 'Frase:' in prompt and '"ipa"' in prompt and '"phrase"' in prompt:
            assert "Frase: beaucoup d'erreurs" in prompt
            return resp_phrase_ipa

        # Token POS classification
        if '"pos"' in prompt and 'lemma' in prompt:
            if 'Palabra: beaucoup' in prompt:
                return resp_pos_other
            if 'Palabra: erreurs' in prompt:
                return resp_pos_noun

        # Verb check should be called for noun path; return false.
        if '"is_verb"' in prompt and 'Ejemplos' in prompt:
            return resp_verb_check_false

        # Other translation
        if 'un campo: "translation"' in prompt and 'Palabra: beaucoup' in prompt:
            return resp_other_translation

        # Noun translation+article should be asked for singularized token (erreur)
        if '"translation"' in prompt and '"article"' in prompt:
            assert "La palabra es 'erreur'" in prompt
            return resp_noun

        # IPA for tokens
        if '"ipa"' in prompt and '"phrase"' in prompt and 'Frase propuesta:' in prompt:
            if 'Palabra: beaucoup' in prompt:
                return resp_other_ipa
            if 'Palabra: erreur' in prompt:
                return resp_noun_ipa

        raise RuntimeError(f'Unexpected fake_call prompt: {prompt[:400]}')

    monkeypatch.setattr(word_adder, '_call_openai_chat', fake_call)

    class DummyResp:
        def __init__(self, status=404, data=None):
            self.status_code = status
            self._data = data or []

        def json(self):
            return self._data

    monkeypatch.setattr(word_adder.requests, 'get', lambda url, timeout=5: DummyResp(status=404, data=[]))

    result = word_adder.prepare_word_actions(phrase, list_id, language, openai_api_key='fake', user_id=TEST_USER_ID)
    assert result.get('is_phrase') is True
    # Full phrase entry is stored in a TTS-safe form (no apostrophes/punctuation).
    assert result.get('word') == "beaucoup derreurs"

    inserts = [params for (sql, params) in result['queries'] if 'INSERT INTO words' in sql]
    inserted_words = {p[0] for p in inserts}
    assert "beaucoup derreurs" in inserted_words
    assert 'beaucoup' in inserted_words
    # erreurs should be normalized to singular noun 'erreur'
    assert 'erreur' in inserted_words
    # Do not insert contracted form as its own token.
    assert "d'erreurs" not in inserted_words
    assert "beaucoup d’erreurs." not in inserted_words


