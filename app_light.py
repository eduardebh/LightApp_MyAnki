

import os
# --- Soporte seguro para credenciales Google Cloud en Render ---
if 'GOOGLE_APPLICATION_CREDENTIALS_JSON' in os.environ:
    creds_path = '/tmp/gcloud_creds.json'
    with open(creds_path, 'w') as f:
        f.write(os.environ['GOOGLE_APPLICATION_CREDENTIALS_JSON'])
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = creds_path

# --- Soporte local: usar google-credentials.json si existe y no hay ADC configurado ---
try:
    if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        _here = os.path.dirname(os.path.abspath(__file__))
        _local_creds = os.path.join(_here, 'google-credentials.json')
        if os.path.isfile(_local_creds):
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = _local_creds
            print('[STARTUP] Using local Google credentials file: google-credentials.json')
except Exception as _e:
    print('[STARTUP] Warning: could not set local Google credentials file:', _e)
texttospeech = None
try:
    from google.cloud import texttospeech  # type: ignore
except Exception as e:
    # Allow app to run without Google Cloud TTS installed; frontend can use SpeechSynthesis.
    print('[STARTUP] Warning: google-cloud-texttospeech not available:', e)
import random
from flask import Flask, render_template, request, redirect, url_for, session as flask_session, jsonify
from dotenv import load_dotenv
import psycopg2
from werkzeug.security import check_password_hash, generate_password_hash

WORD_SELECTION_DECAY = 0.75


def word_selection_weight(counter_word):
    return 2 ** (-counter_word * WORD_SELECTION_DECAY)


openai = None
try:
    import openai
except Exception as e:
    print('[STARTUP] Warning: OpenAI SDK unavailable:', e)
# Guarded import: some deploys may fail to install Flask-Cors; avoid crashing the app.
HAS_FLASK_CORS = False
try:
    from flask_cors import CORS
    HAS_FLASK_CORS = True
except Exception as e:
    print('[STARTUP] Warning: flask_cors not available:', e)
    # Provide a no-op fallback so the rest of the code can still run in diagnostics mode.
    def CORS(app=None, resources=None, supports_credentials=False):
        print('[STARTUP] CORS() no-op called (flask_cors missing)')
        return None
    print('[STARTUP] NOTE: Install Flask-Cors (Flask-Cors==4.0.0) and set CORS_ORIGINS in environment for production.')

load_dotenv()


def _resolve_database_url():
    """Resolve DATABASE_URL from env or from the new Supabase project URL."""
    db_url = os.environ.get('DATABASE_URL') or os.environ.get('SUPABASE_DATABASE_URL')
    if db_url:
        return db_url

    project_url = os.environ.get('SUPABASE_PROJECT_URL') or os.environ.get('SUPABASE_URL')
    if not project_url:
        return None

    project_ref = (
        os.environ.get('SUPABASE_PROJECT_REF')
        or project_url.rstrip('/').split('://', 1)[-1].replace('.supabase.co', '')
    )
    # Use the Supabase pooler by default. It works on IPv4-only networks where
    # the direct database host may resolve only to unreachable IPv6 addresses.
    host = os.environ.get('SUPABASE_DB_HOST') or 'aws-0-eu-central-1.pooler.supabase.com'
    port = os.environ.get('SUPABASE_DB_PORT') or '6543'
    user = os.environ.get('SUPABASE_DB_USERNAME') or f'postgres.{project_ref}'
    password = (
        os.environ.get('SUPABASE_DB_PASSWORD')
        or os.environ.get('DB_PASSWORD')
        or os.environ.get('SUPABASE_PASSWORD')
    )
    if not password:
        return None

    db_url = f"postgresql://{user}:{password}@{host}:{port}/postgres?sslmode=require"
    os.environ['DATABASE_URL'] = db_url
    return db_url


app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'))
app.secret_key = 'supersecretkey-lightapp'  # Cambia esto en producción

# Session cookie settings: allow cross-site cookies when required by the frontend.
# For cross-site requests the cookie must be sent with SameSite=None and Secure=True.
# These can be overridden via environment variables in production if needed.
app.config.update({
    'SESSION_COOKIE_SAMESITE': os.environ.get('SESSION_COOKIE_SAMESITE', 'None'),
    'SESSION_COOKIE_SECURE': os.environ.get('SESSION_COOKIE_SECURE', 'True').lower() in ('1', 'true', 'yes'),
    'SESSION_COOKIE_HTTPONLY': True,
})

# Configure CORS for API endpoints. Set env var CORS_ORIGINS to a comma-separated list
# or a single origin. Default is '*' (all origins) for diagnostics — consider
# restricting this in production (e.g., set CORS_ORIGINS to 'https://myankiebh.onrender.com').
cors_origins = os.environ.get('CORS_ORIGINS', '*')
if cors_origins and ',' in cors_origins:
    cors_origins = [o.strip() for o in cors_origins.split(',')]
if HAS_FLASK_CORS:
    CORS(app, resources={r"/api/*": {"origins": cors_origins}}, supports_credentials=True)
else:
    # Still call the no-op to make intent explicit in logs
    CORS()

# Startup summary for Render logs
try:
    if isinstance(cors_origins, (list, tuple)):
        cors_display = ','.join(cors_origins)
    else:
        cors_display = str(cors_origins)
    print(f"[STARTUP] HAS_FLASK_CORS={HAS_FLASK_CORS} CORS_ORIGINS={cors_display}")
except Exception:
    print('[STARTUP] HAS_FLASK_CORS startup log failed')


# Forzar cabeceras CORS en todas las respuestas (incluye redirecciones)
@app.after_request
def add_cors_headers(response):
    try:
        origin = request.headers.get('Origin')
        # Normalize values that represent an absent origin. Some proxies or browser
        # privacy features send the literal string 'null' — treat that as no origin.
        if origin and origin.lower() != 'null':
            # If cors_origins is a list, only echo back allowed origins.
            allowed = False
            try:
                if cors_origins == '*':
                    allowed = True
                elif isinstance(cors_origins, (list, tuple)):
                    allowed = origin in cors_origins
                else:
                    allowed = origin == cors_origins
            except Exception:
                allowed = False

            if allowed:
                response.headers['Access-Control-Allow-Origin'] = origin
            else:
                # Do not echo back unapproved origins. Use a conservative default
                # (no Access-Control-Allow-Origin) so browsers will block unsafe cross-site requests.
                print(f"[CORS] Rejected Origin={origin} not in CORS_ORIGINS")
        else:
            # No origin present (or 'null') — fall back to '*' only for diagnostics.
            if cors_origins == '*':
                response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    except Exception:
        pass
    # Diagnostic prints for responses
    try:
        loc = response.headers.get('Location')
        aco = response.headers.get('Access-Control-Allow-Origin')
        print(f"[RES] {request.method} {request.path} -> status={response.status_code} ACO={aco} Location={loc}")
    except Exception:
        pass
    return response


@app.before_request
def log_request_info():
    try:
        meth = request.method
        path = request.path
        origin = request.headers.get('Origin')
        referer = request.headers.get('Referer')
        ua = request.headers.get('User-Agent')
        # Don't print cookie values; only names and lengths for diagnostics
        cookies = request.cookies
        cookie_info = {k: (len(v) if v is not None else 0) for k, v in cookies.items()}
        user_id = flask_session.get('user_id')
        print(f"[REQ] {meth} {path} Origin={origin} Referer={referer} User={user_id} UA='{(ua or '')[:60]}' Cookies={cookie_info}")
        # Special-case OPTIONS preflight logging
        if meth == 'OPTIONS':
            hdrs = {k: v for k, v in request.headers.items()}
            print(f"[REQ-OPTIONS] headers={list(hdrs.keys())}")
    except Exception as e:
        print('[REQ] logging exception:', e)


_LAST_TTS_VOICE = {}
_NATURAL_TTS_VOICES = {}
_NATURAL_TTS_VOICE_MARKERS = ('-Chirp3-HD-', '-Neural2-', '-Studio-', '-Wavenet-')


def _get_available_natural_voices(client, language_code):
    """Return the Google Cloud natural voices enabled for a language."""
    cached_voices = _NATURAL_TTS_VOICES.get(language_code)
    if cached_voices:
        return cached_voices[:]

    try:
        response = client.list_voices(language_code=language_code)
    except Exception as exc:
        print(f'[TTS] Could not list Google natural voices for {language_code}: {exc}')
        return []

    voices = sorted({
        voice.name
        for voice in response.voices
        if voice.name.startswith(f'{language_code}-')
        and any(marker in voice.name for marker in _NATURAL_TTS_VOICE_MARKERS)
    })
    if voices:
        _NATURAL_TTS_VOICES[language_code] = voices
    return voices[:]


def _shuffle_voices_without_repeating_last(voices, language_code):
    shuffled_voices = voices[:]
    random.shuffle(shuffled_voices)
    last_voice = _LAST_TTS_VOICE.get(language_code)
    if last_voice in shuffled_voices and len(shuffled_voices) > 1:
        shuffled_voices.remove(last_voice)
        shuffled_voices.append(last_voice)
    return shuffled_voices


@app.route('/favicon.ico')
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="30" fill="#23272f"/>'
           '<text x="32" y="43" font-size="34" text-anchor="middle" fill="#fde047">A</text></svg>')
    return app.response_class(svg, mimetype='image/svg+xml')


@app.route('/api/tts', methods=['POST'])
def api_tts():
    data = request.get_json()
    text = data.get('text', '').strip()
    raw_language = data.get('language', 'en-US')
    # Normalize language codes so callers can send 'fr', 'fr-fr', 'fr_FR', etc.
    def _normalize_lang(lang: str) -> str:
        try:
            if not lang:
                return 'en-US'
            s = str(lang).strip()
            if not s:
                return 'en-US'
            s = s.replace('_', '-').strip()
            low = s.lower()
            # Common 2-letter shorthands used by the app
            shorthand = {
                'fr': 'fr-FR',
                'de': 'de-DE',
                'es': 'es-ES',
                'en': 'en-US',
                'pt': 'pt-BR',
                'it': 'it-IT',
            }
            if low in shorthand:
                return shorthand[low]
            if '-' in s:
                parts = s.split('-', 1)
                lang_part = (parts[0] or '').lower()
                region_part = (parts[1] or '').upper()
                if lang_part and region_part:
                    return f'{lang_part}-{region_part}'
                if lang_part:
                    return shorthand.get(lang_part, f'{lang_part}-US')
            # Fallback: treat as language-only
            return shorthand.get(low, 'en-US')
        except Exception:
            return 'en-US'

    language = _normalize_lang(raw_language)
    ipa = (data.get('ipa') or '').strip() if isinstance(data, dict) else ''
    if not text:
        return jsonify({'success': False, 'error': 'No text provided'}), 400
    # Browser synthesis remains the fallback when Cloud TTS is unavailable.
    # Set DISABLE_GOOGLE_TTS=1 to explicitly disable Google Cloud TTS.
    disable_google_tts = os.environ.get('DISABLE_GOOGLE_TTS', '0').lower() in ('1', 'true', 'yes', 'on')
    if disable_google_tts:
        return jsonify({'success': False, 'error': 'Google Cloud TTS disabled'}), 200
    if texttospeech is None:
        return jsonify({'success': False, 'error': 'google-cloud-texttospeech not installed'}), 200
    try:
        print(f'[TTS] Requesting Google Cloud TTS for "{text}" ({language})...')
        client = texttospeech.TextToSpeechClient()
        # If IPA is provided, prefer SSML phoneme so the spoken audio can match liaison-aware IPA.
        if ipa:
            def _xml_escape(s: str) -> str:
                return (s.replace('&', '&amp;')
                         .replace('<', '&lt;')
                         .replace('>', '&gt;')
                         .replace('"', '&quot;')
                         .replace("'", '&apos;'))

            ssml = f'<speak><phoneme alphabet="ipa" ph="{_xml_escape(ipa)}">{_xml_escape(text)}</phoneme></speak>'
            synthesis_input = texttospeech.SynthesisInput(ssml=ssml)
        else:
            synthesis_input = texttospeech.SynthesisInput(text=text)

        # Prefer an effects profile optimized for phone speakers when the request comes from a mobile browser.
        ua = (request.headers.get('User-Agent') or '')
        ua_low = ua.lower()
        is_mobile = ('android' in ua_low) or ('iphone' in ua_low) or ('ipad' in ua_low) or ('ipod' in ua_low)

        audio_config_kwargs = {
            'audio_encoding': texttospeech.AudioEncoding.MP3,
        }
        if is_mobile:
            audio_config_kwargs['effects_profile_id'] = ['handset-class-device']
        audio_config = texttospeech.AudioConfig(**audio_config_kwargs)

        def _is_voice_not_available_error(err: Exception) -> bool:
            msg = (str(err) or '').lower()
            # Best-effort detection: Google returns InvalidArgument when a voice name is not supported.
            return ('invalid argument' in msg) or ('invalidargument' in msg) or ('voice' in msg and 'not' in msg)

        # Selección aleatoria de voz por idioma con fallback robusto para proyectos sin Neural2.
        voice_candidates_by_lang = {
            'fr-FR': [
                'fr-FR-Neural2-A', 'fr-FR-Neural2-B', 'fr-FR-Neural2-C', 'fr-FR-Neural2-D',
                'fr-FR-Neural2-E', 'fr-FR-Neural2-F', 'fr-FR-Neural2-G',
                'fr-FR-Standard-A', 'fr-FR-Standard-B', 'fr-FR-Standard-C', 'fr-FR-Standard-D', 'fr-FR-Standard-E'
            ],
            'de-DE': [
                'de-DE-Neural2-A', 'de-DE-Neural2-B', 'de-DE-Neural2-C', 'de-DE-Neural2-D',
                'de-DE-Neural2-F', 'de-DE-Neural2-G', 'de-DE-Neural2-H',
                'de-DE-Standard-A', 'de-DE-Standard-B', 'de-DE-Standard-C', 'de-DE-Standard-D'
            ],
            'en-US': [
                'en-US-Neural2-A', 'en-US-Neural2-B', 'en-US-Neural2-C', 'en-US-Neural2-D',
                'en-US-Standard-A', 'en-US-Standard-B', 'en-US-Standard-C', 'en-US-Standard-D'
            ],
            'es-ES': [
                'es-ES-Neural2-A', 'es-ES-Neural2-B', 'es-ES-Neural2-C',
                'es-ES-Standard-A', 'es-ES-Standard-B', 'es-ES-Standard-C'
            ],
            'it-IT': [
                'it-IT-Neural2-A', 'it-IT-Neural2-B', 'it-IT-Neural2-C',
                'it-IT-Standard-A', 'it-IT-Standard-B', 'it-IT-Standard-C'
            ],
            'pt-BR': [
                'pt-BR-Neural2-A', 'pt-BR-Neural2-B', 'pt-BR-Neural2-C',
                'pt-BR-Standard-A', 'pt-BR-Standard-B', 'pt-BR-Standard-C'
            ],
        }

        lang_code = language
        if language.startswith('fr'):
            lang_code = 'fr-FR'
        elif language.startswith('de'):
            lang_code = 'de-DE'
        elif language.startswith('en'):
            lang_code = 'en-US'
        elif language.startswith('es'):
            lang_code = 'es-ES'
        elif language.startswith('it'):
            lang_code = 'it-IT'
        elif language.startswith('pt'):
            lang_code = 'pt-BR'

        voice_candidates = voice_candidates_by_lang.get(lang_code, [])

        if voice_candidates:
            # Query the voices enabled in this Google project. This is especially
            # important for French, whose available natural voices vary by project.
            natural = _get_available_natural_voices(client, lang_code)
            if ipa:
                natural = [voice_name for voice_name in natural if '-Chirp3-HD-' not in voice_name]
            if not natural:
                natural = [v for v in voice_candidates if '-Standard-' not in v]
            standard = [v for v in voice_candidates if '-Standard-' in v]
            voice_candidates = _shuffle_voices_without_repeating_last(natural, lang_code) + standard
            last_voice_err: Exception | None = None
            response = None
            for voice_name in voice_candidates:
                voice = texttospeech.VoiceSelectionParams(language_code=lang_code, name=voice_name)
                try:
                    print(f'[TTS] GoogleCloud | Texto: "{text}" | Idioma: {language} | Voz: {voice_name} | mobile={is_mobile}')
                    response = client.synthesize_speech(
                        input=synthesis_input,
                        voice=voice,
                        audio_config=audio_config
                    )
                    if response and getattr(response, 'audio_content', None):
                        _LAST_TTS_VOICE[lang_code] = voice_name
                        break
                except Exception as e:
                    if _is_voice_not_available_error(e):
                        last_voice_err = e
                        continue
                    raise

            if not response or not getattr(response, 'audio_content', None):
                if last_voice_err is not None:
                    raise last_voice_err
                raise RuntimeError('No audio content from Google Cloud TTS')
        else:
            voice = texttospeech.VoiceSelectionParams(
                language_code=language,
                ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
            )
            print(f'[TTS] GoogleCloud | Texto: "{text}" | Idioma: {language} | Voz: NEUTRAL | mobile={is_mobile}')
            response = client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )

        audio_content = response.audio_content
        if audio_content:
            import base64
            audio_b64 = base64.b64encode(audio_content).decode('utf-8')
            print('[TTS] Google Cloud TTS success!')
            return jsonify({'success': True, 'audioContent': audio_b64})
        else:
            print('[TTS] Google Cloud TTS: No audioContent in response')
            return jsonify({'success': False, 'error': 'No audio content'}), 502
    except Exception as e:
        # Treat expected cloud-config failures as graceful fallback so the UI can keep working.
        msg = str(e) if e is not None else ''
        msg_low = msg.lower()
        fallback_markers = (
            'default credentials were not found',
            'billing_disabled',
            'requires billing to be enabled',
            'permission_denied',
        )
        if any(marker in msg_low for marker in fallback_markers):
            print('[TTS] Google Cloud unavailable; falling back to browser TTS')
            return jsonify({'success': False, 'error': f'Google Cloud TTS unavailable: {msg}'}), 200
        import traceback
        print(f'[TTS] Exception: {e}')
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Internal error: {e}'}), 500

def get_pg_conn():
    db_url = _resolve_database_url() or os.environ.get('DATABASE_URL')
    if not db_url:
        raise RuntimeError('DATABASE_URL not set in environment')
    try:
        # Mostrar la URL enmascarada para diagnóstico sin revelar credenciales
        def mask_db_url(url):
            import re
            if not url:
                return ''
            try:
                # Reemplaza la contraseña entre ':' y '@' por '***'
                return re.sub(r'(:\/\/[^:]+):[^@]+@', r"\1:***@", url)
            except Exception:
                return '***'
        try:
            print('[DB] Connecting to', mask_db_url(db_url))
        except Exception:
            pass
    except Exception:
        pass
    return psycopg2.connect(db_url)


@app.route('/api/db_health')
def api_db_health():
    """Endpoint público (sin autenticación) para comprobar si la app puede conectar a la base de datos.
    Devuelve JSON con {'ok': True} o {'ok': False, 'error': '...'} y enmascara la URL en la respuesta.
    """
    db_url = _resolve_database_url() or os.environ.get('DATABASE_URL')
    def mask_db_url(url):
        import re
        if not url:
            return ''
        try:
            return re.sub(r'(:\/\/[^:]+):[^@]+@', r"\1:***@", url)
        except Exception:
            return '***'
    if not db_url:
        return jsonify({'ok': False, 'error': 'DATABASE_URL not set in environment'}), 500
    try:
        print('[DB_HEALTH] Trying connection to', mask_db_url(db_url))
        conn = psycopg2.connect(db_url, connect_timeout=5)
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'db': mask_db_url(db_url)})
    except Exception as e:
        import traceback
        print('[DB_HEALTH] Exception connecting to DB:', e)
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e), 'db': mask_db_url(db_url)}), 500

# Endpoint para agregar una palabra a la lista activa
@app.route('/api/add_word', methods=['POST'])
def api_add_word():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    word = data.get('word', '')
    word = word.strip() if isinstance(word, str) else ''
    if not word:
        return jsonify({'success': False, 'error': 'No word provided'}), 400
    association = data.get('association', '')
    association = association.strip() if isinstance(association, str) else ''
    ipa_word = data.get('ipa_word', '')
    ipa_word = ipa_word.strip() if isinstance(ipa_word, str) else ''
    grammar_rule = data.get('grammar_rule', '')
    grammar_rule = grammar_rule.strip() if isinstance(grammar_rule, str) else ''
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Obtener lista activa
        cur.execute(
            'SELECT wl.id FROM users u '
            'JOIN word_lists wl ON wl.id = u.last_list AND wl.user_id = u.id '
            'WHERE u.id = %s',
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify({'success': False, 'error': 'No active list'}), 400
        active_list_id = row[0]
        # Upgrade existing databases before writing the grammar rule field.
        cur.execute('ALTER TABLE words ADD COLUMN IF NOT EXISTS grammar_rule TEXT')
        # Si la palabra ya existe y no se envía un valor, se conserva el actual.
        cur.execute('''
            INSERT INTO words (word, association, "IPA_word", grammar_rule, list_id, added, counter_word)
            VALUES (%s, %s, %s, %s, %s, TRUE, 0)
            ON CONFLICT (word, list_id) DO UPDATE SET
                association = CASE WHEN EXCLUDED.association <> '' THEN EXCLUDED.association ELSE words.association END,
                "IPA_word" = CASE WHEN EXCLUDED."IPA_word" <> '' THEN EXCLUDED."IPA_word" ELSE words."IPA_word" END,
                grammar_rule = CASE WHEN EXCLUDED.grammar_rule <> '' THEN EXCLUDED.grammar_rule ELSE words.grammar_rule END,
                added=TRUE,
                counter_word=0
        ''', (word, association, ipa_word, grammar_rule, active_list_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({
            'success': True,
            'word': word,
            'association': association,
            'IPA_word': ipa_word,
            'grammar_rule': grammar_rule,
        })
    except Exception as e:
        print('[ERROR] api_add_word:', e)
        return jsonify({'success': False, 'error': 'Internal error'}), 500

# Endpoint para obtener la association de una palabra de la lista activa
@app.route('/api/get_association', methods=['POST'])
def get_association():
    user_id = flask_session.get('user_id')
    if not user_id:
        return {'error': 'Not logged in'}, 401
    data = request.get_json()
    word = data.get('word')
    if not word:
        return {'error': 'No word provided'}, 400
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return {'error': 'No active list'}, 400
        active_list_id = row[0]
        cur.execute('SELECT association FROM words WHERE word = %s AND list_id = %s', (word, active_list_id))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return {'error': 'Association not found'}, 404
        return {'association': row[0]}
    except Exception as e:
        print('[ERROR] get_association:', e)
        return {'error': 'Internal error'}, 500

# Endpoint para obtener la Grammar_rule de una palabra de la lista activa
@app.route('/api/get_grammar_rule', methods=['POST'])
def get_grammar_rule():
    user_id = flask_session.get('user_id')
    if not user_id:
        return {'error': 'Not logged in'}, 401
    data = request.get_json(silent=True) or {}
    word = data.get('word')
    if not word:
        return {'error': 'No word provided'}, 400
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return {'error': 'No active list'}, 400
        active_list_id = row[0]
        cur.execute('SELECT * FROM words WHERE word = %s AND list_id = %s', (word, active_list_id))
        row = cur.fetchone()
        cols = [d[0].lower() for d in cur.description] if cur.description else []
        cur.close(); conn.close()
        if not row:
            return {'error': 'Word not found'}, 404
        rule = row[cols.index('grammar_rule')] if 'grammar_rule' in cols else ''
        return {'grammar_rule': rule or ''}
    except Exception as e:
        print('[ERROR] get_grammar_rule:', e)
        return {'error': 'Internal error'}, 500

# Endpoint para obtener todas las palabras de la lista activa (word, association, IPA_word)
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            error = 'Username and password required.'
        else:
            try:
                conn = get_pg_conn()
                cur = conn.cursor()
                cur.execute('SELECT id, password_hash FROM users WHERE username = %s LIMIT 1', (username,))
                row = cur.fetchone()
                if row and check_password_hash(row[1], password):
                    flask_session['user_id'] = row[0]
                    flask_session['username'] = username
                    cur.close(); conn.close()
                    return redirect(url_for('home'))
                else:
                    error = 'Invalid username or password.'
                    cur.close(); conn.close()
            except Exception as e:
                print('[ERROR] login DB error:', e)
                error = 'Internal error. See server logs.'
    return render_template('login.html', error=error)
@app.route('/api/all_words')
def api_all_words():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify([])
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify([])
        active_list_id = row[0]
        cur.execute('ALTER TABLE words ADD COLUMN IF NOT EXISTS grammar_rule TEXT')
        conn.commit()
        cur.execute(
            'SELECT id, word, association, "IPA_word", grammar_rule, counter_word '
            'FROM words WHERE list_id = %s',
            (active_list_id,),
        )
        words = [{
            'id': r[0],
            'word': r[1],
            'association': r[2],
            'IPA_word': r[3],
            'grammar_rule': r[4],
            'counter_word': r[5],
        } for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify(words)
    except Exception as e:
        print('[ERROR] api_all_words:', e)
        return jsonify([])


@app.route('/api/update_words', methods=['POST'])
def api_update_words():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json(silent=True) or {}
    updates = data.get('words')
    if not isinstance(updates, list) or not updates:
        return jsonify({'success': False, 'error': 'No word updates provided'}), 400

    normalized_updates = []
    seen_ids = set()
    seen_words = set()
    for update in updates:
        if not isinstance(update, dict):
            return jsonify({'success': False, 'error': 'Invalid word update'}), 400

        word_id = update.get('id')
        if isinstance(word_id, bool):
            return jsonify({'success': False, 'error': 'Invalid word ID'}), 400
        try:
            word_id = int(word_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Invalid word ID'}), 400

        word = update.get('word')
        association = update.get('association')
        ipa_word = update.get('IPA_word')
        grammar_rule = update.get('grammar_rule')
        if not all(isinstance(value, str) for value in (word, association, ipa_word, grammar_rule)):
            return jsonify({'success': False, 'error': 'Word values must be text'}), 400

        word = word.strip()
        if not word:
            return jsonify({'success': False, 'error': 'Word cannot be empty'}), 400
        if word_id in seen_ids:
            return jsonify({'success': False, 'error': 'A word was updated more than once'}), 400
        if word in seen_words:
            return jsonify({'success': False, 'error': 'Duplicate words are not allowed in a list'}), 400

        seen_ids.add(word_id)
        seen_words.add(word)
        normalized_updates.append({
            'id': word_id,
            'word': word,
            'association': association.strip(),
            'IPA_word': ipa_word.strip(),
            'grammar_rule': grammar_rule.strip(),
        })

    conn = None
    cur = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute(
            'SELECT wl.id FROM users u '
            'JOIN word_lists wl ON wl.id = u.last_list AND wl.user_id = u.id '
            'WHERE u.id = %s',
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return jsonify({'success': False, 'error': 'No active list'}), 400
        active_list_id = row[0]
        cur.execute('ALTER TABLE words ADD COLUMN IF NOT EXISTS grammar_rule TEXT')

        word_ids = [update['id'] for update in normalized_updates]
        cur.execute(
            'SELECT id, word FROM words WHERE list_id = %s AND id = ANY(%s)',
            (active_list_id, word_ids),
        )
        existing_words = {row[0]: row[1] for row in cur.fetchall()}
        if len(existing_words) != len(normalized_updates):
            return jsonify({'success': False, 'error': 'One or more words were not found in the active list'}), 404

        cur.execute(
            'SELECT id, word FROM words WHERE list_id = %s AND word = ANY(%s)',
            (active_list_id, [update['word'] for update in normalized_updates]),
        )
        word_owner_ids = {word: word_id for word_id, word in cur.fetchall()}
        for update in normalized_updates:
            owner_id = word_owner_ids.get(update['word'])
            if owner_id is not None and owner_id != update['id']:
                return jsonify({
                    'success': False,
                    'error': f'The word "{update["word"]}" already exists in the active list',
                }), 409

        for update in normalized_updates:
            cur.execute(
                'UPDATE words SET word = %s, association = %s, "IPA_word" = %s, grammar_rule = %s '
                'WHERE id = %s AND list_id = %s',
                (
                    update['word'],
                    update['association'],
                    update['IPA_word'],
                    update['grammar_rule'],
                    update['id'],
                    active_list_id,
                ),
            )
        conn.commit()
        return jsonify({'success': True, 'words': normalized_updates})
    except Exception as e:
        if conn:
            conn.rollback()
        print('[ERROR] api_update_words:', e)
        return jsonify({'success': False, 'error': 'Internal error'}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Endpoint para marcar una palabra como added=TRUE en la lista activa
@app.route('/api/mark_word_added', methods=['POST'])
def api_mark_word_added():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    data = request.get_json()
    word = data.get('word')
    if not word:
        return jsonify({'success': False, 'error': 'No word provided'}), 400
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify({'success': False, 'error': 'No active list'}), 400
        active_list_id = row[0]
        cur.execute('UPDATE words SET added = TRUE WHERE word = %s AND list_id = %s', (word, active_list_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        print('[ERROR] api_mark_word_added:', e)
        return jsonify({'success': False, 'error': 'Internal error'}), 500

# Endpoint para obtener palabras de la lista activa con added=FALSE
@app.route('/api/inactive_words')
def api_inactive_words():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify([])
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify([])
        active_list_id = row[0]
        cur.execute('SELECT word, association FROM words WHERE list_id = %s AND added = FALSE', (active_list_id,))
        words = [{'word': r[0], 'association': r[1]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify(words)
    except Exception as e:
        print('[ERROR] api_inactive_words:', e)
        return jsonify([])


# Endpoint to delete a word from the active list
@app.route('/api/delete_word', methods=['POST'])
def api_delete_word():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    word = (data.get('word') or '').strip()
    if not word:
        return jsonify({'success': False, 'error': 'No word provided'}), 400
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify({'success': False, 'error': 'No active list'}), 400
        active_list_id = row[0]

        cur.execute('DELETE FROM words WHERE word = %s AND list_id = %s', (word, active_list_id))
        deleted = int(cur.rowcount or 0)
        conn.commit()
        cur.close(); conn.close()

        if deleted <= 0:
            return jsonify({'success': False, 'error': 'Word not found'}), 404
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        print('[ERROR] api_delete_word:', e)
        return jsonify({'success': False, 'error': 'Internal error'}), 500

# Endpoint para modificar counter_word
@app.route('/api/update_counter', methods=['POST'])
def update_counter():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json()
    word = data.get('word')
    delta = int(data.get('delta', 0))
    if not word or delta == 0:
        return jsonify({'error': 'Invalid request'}), 400
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Solo modificar palabras de la lista activa del usuario
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify({'error': 'No active list'}), 400
        active_list_id = row[0]
        # Obtener el valor actual
        cur.execute('SELECT counter_word FROM words WHERE word = %s AND list_id = %s', (word, active_list_id))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({'error': 'Word not found'}), 404
        current = row[0]
        new_value = max(0, current + delta)

        # If the word reaches the success threshold, mark it as inactive for the "added" pool.
        # We only flip TRUE -> FALSE here; re-adding is handled explicitly by the existing endpoint.
        threshold = 100
        cur.execute(
            'UPDATE words\n'
            'SET counter_word = %s,\n'
            '    added = CASE WHEN %s >= %s THEN FALSE ELSE added END\n'
            'WHERE word = %s AND list_id = %s\n'
            'RETURNING counter_word, added',
            (new_value, new_value, threshold, word, active_list_id),
        )
        updated = cur.fetchone()
        updated_counter = updated[0] if updated else new_value
        updated_added = bool(updated[1]) if updated and updated[1] is not None else None
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'success': True, 'counter_word': updated_counter, 'added': updated_added})
    except Exception as e:
        print('[ERROR] update_counter:', e)
        return jsonify({'error': 'Internal error'}), 500

@app.route('/api/random_word', methods=['GET', 'POST'])
def api_random_word():
    user_id = flask_session.get('user_id')
    if not user_id:
        return {'error': 'Not logged in'}, 401
    active_list_id = None
    try:
        exclude_word = None
        if request.method == 'POST':
            data = request.get_json(silent=True)
            if data:
                exclude_word = data.get('exclude')
        else:
            exclude_word = request.args.get('exclude')

        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if row and row[0]:
            active_list_id = row[0]
        else:
            cur.execute('SELECT id FROM word_lists WHERE user_id = %s ORDER BY name ASC LIMIT 1', (user_id,))
            row = cur.fetchone()
            if row:
                active_list_id = row[0]
        if not active_list_id:
            cur.close(); conn.close()
            return {'error': 'No active list'}, 400
        cur.execute('SELECT id, word, counter_word, "IPA_word" FROM words WHERE list_id = %s AND added = TRUE', (active_list_id,))
        words = cur.fetchall()
        if not words:
            cur.close(); conn.close()
            return {'error': 'No words in list'}, 404
        # Obtener el language de la lista activa
        cur.execute('SELECT language FROM word_lists WHERE id = %s', (active_list_id,))
        lang_row = cur.fetchone()
        language = lang_row[0] if lang_row and lang_row[0] else 'de'
        # Priorizar los contadores bajos con una caída más gradual.
        weights = [word_selection_weight(w[2]) for w in words]
        # Exclude the previous word if possible
        filtered_words = words
        filtered_weights = weights
        if exclude_word and len(words) > 1:
            filtered_words = [w for w in words if w[1] != exclude_word]
            filtered_weights = [weights[i] for i, w in enumerate(words) if w[1] != exclude_word]
            if not filtered_words:
                filtered_words = words
                filtered_weights = weights
        selected = random.choices(filtered_words, weights=filtered_weights, k=1)[0]
        cur.close(); conn.close()
        return {'ipa_word': selected[3], 'language': language, 'word': selected[1]}
    except Exception as e:
        print('[ERROR] api_random_word:', e)
        return {'error': 'Internal error'}, 500



@app.route('/', methods=['GET', 'POST'])
def home():
    if not flask_session.get('user_id'):
        return redirect(url_for('login'))
    user_id = flask_session.get('user_id')
    active_list = None
    all_lists = []
    if not user_id:
        return redirect(url_for('login'))
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Get all lists for the user
        cur.execute('SELECT id, name, language FROM word_lists WHERE user_id = %s ORDER BY name ASC', (user_id,))
        all_lists = [{'id': row[0], 'name': row[1], 'language': row[2]} for row in cur.fetchall()]
        # Handle list selection
        if request.method == 'POST':
            new_list_id = request.form.get('active_list_id')
            if new_list_id:
                cur.execute('UPDATE users SET last_list = %s WHERE id = %s', (new_list_id, user_id))
                conn.commit()
        # Get active list
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        active_list_id = row[0] if row and row[0] else None
        if active_list_id:
            cur.execute('SELECT id, name, language FROM word_lists WHERE id = %s', (active_list_id,))
            row = cur.fetchone()
            if row:
                active_list = {'id': row[0], 'name': row[1], 'language': row[2]}
        cur.close(); conn.close()
    except Exception as e:
        print('[ERROR] home():', e)
    return render_template('base.html', user_id=user_id, active_list=active_list, all_lists=all_lists)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            error = 'Username and password required.'
        elif len(password) < 4:
            error = 'Password too short (min 4 chars).'
        else:
            try:
                conn = get_pg_conn()
                cur = conn.cursor()
                cur.execute('SELECT id FROM users WHERE username = %s LIMIT 1', (username,))
                if cur.fetchone():
                    error = 'Username already taken.'
                    cur.close(); conn.close()
                else:
                    pw_hash = generate_password_hash(password)
                    cur.execute('INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id', (username, pw_hash))
                    user_id = cur.fetchone()[0]
                    conn.commit()
                    cur.close(); conn.close()
                    flask_session['user_id'] = user_id
                    flask_session['username'] = username
                    return redirect(url_for('home'))
            except Exception as e:
                print('[ERROR] register DB error:', e)
                error = 'Internal error. See server logs.'
    return render_template('register.html', error=error)

@app.route('/logout')
def logout():
    for k in ('user_id','username'):
        if k in flask_session:
            flask_session.pop(k, None)
    return redirect(url_for('login'))


# Página para mostrar una palabra aleatoria y su IPA
@app.route('/random_word')
def random_word():
    user_id = flask_session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    # Also load list context so base.html can reflect active language
    active_list = None
    all_lists = []
    active_list_id = None
    selected_word = None
    ipa_word = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Lists for UI
        cur.execute('SELECT id, name, language FROM word_lists WHERE user_id = %s ORDER BY name ASC', (user_id,))
        all_lists = [{'id': row[0], 'name': row[1], 'language': row[2]} for row in cur.fetchall()]

        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if row and row[0]:
            active_list_id = row[0]
        else:
            cur.execute('SELECT id FROM word_lists WHERE user_id = %s ORDER BY name ASC LIMIT 1', (user_id,))
            row = cur.fetchone()
            if row:
                active_list_id = row[0]
        if not active_list_id:
            cur.close(); conn.close()
            return render_template('random_word.html', error='No active list', active_list=active_list, all_lists=all_lists)

        # Active list details (incl. language)
        cur.execute('SELECT id, name, language FROM word_lists WHERE id = %s', (active_list_id,))
        row = cur.fetchone()
        if row:
            active_list = {'id': row[0], 'name': row[1], 'language': row[2]}

        cur.execute('SELECT id, word, counter_word, "IPA_word" FROM words WHERE list_id = %s AND added = TRUE', (active_list_id,))
        words = cur.fetchall()
        if not words:
            cur.close(); conn.close()
            return render_template('random_word.html', error='No words in list', active_list=active_list, all_lists=all_lists)
        # Priorizar los contadores bajos con una caída más gradual.
        weights = [word_selection_weight(w[2]) for w in words]
        selected = random.choices(words, weights=weights, k=1)[0]
        selected_word = selected[1]
        ipa_word = selected[3]
        cur.close(); conn.close()
        return render_template('random_word.html', word=selected_word, ipa_word=ipa_word, active_list=active_list, all_lists=all_lists)
    except Exception as e:
        print('[ERROR] random_word:', e)
        return render_template('random_word.html', error='Internal error', active_list=active_list, all_lists=all_lists)

if __name__ == '__main__':
    # IMPORTANT: Do not auto-open the browser here.
    # On Windows, `run_lightapp.ps1` already opens the browser (once) via `open_lightapp_browser.ps1`.
    # Keeping an auto-open here causes double tabs.
    app.run(debug=True)
