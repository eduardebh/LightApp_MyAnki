

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
import base64
import requests
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
import openai
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

# Optional integration with external helper repo 'mytools' (added as submodule)
# Preferred path: installed package `frequency_db_utils` (e.g., via `pip install -e ./mytools`).
# Fallback: if running from a source checkout without installation, temporarily add `./mytools` to sys.path.
HAS_MYTOOLS = False
add_word = None

def _try_import_add_word():
    import sys
    import os
    mytools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mytools')
    freq_utils_dir = os.path.join(mytools_dir, 'frequency_db_utils')
    # Add both mytools and frequency_db_utils to sys.path if needed
    if os.path.isdir(mytools_dir) and mytools_dir not in sys.path:
        sys.path.insert(0, mytools_dir)
    if os.path.isdir(freq_utils_dir) and freq_utils_dir not in sys.path:
        sys.path.insert(0, freq_utils_dir)
    try:
        try:
            from frequency_db_utils.word_adder import add_word as _add_word
            return _add_word
        except ModuleNotFoundError:
            from mytools.frequency_db_utils.word_adder import add_word as _add_word
            return _add_word
    except ModuleNotFoundError:
        # Try importing from mytools.frequency_db_utils if running as a submodule
        try:
            from mytools.frequency_db_utils.word_adder import add_word as _add_word
            return _add_word
        except ModuleNotFoundError:
            raise ImportError("Could not import 'add_word' from frequency_db_utils.word_adder or mytools.frequency_db_utils.word_adder")

try:
    add_word = _try_import_add_word()
    HAS_MYTOOLS = True
    print('[STARTUP] mytools helper available: frequency_db_utils.word_adder.add_word')

    # Runtime diagnostics: confirm exactly which copy of frequency_db_utils is imported.
    try:
        import sys as _sys
        import frequency_db_utils as _fdu  # type: ignore
        print('[STARTUP] frequency_db_utils.runtime_signature:', _fdu.runtime_signature())
        if '.venv' not in (_sys.executable or '').lower():
            print('[STARTUP] WARNING: app is not running from .venv:', _sys.executable)
    except Exception as _sig_e:
        print('[STARTUP] NOTE: could not read frequency_db_utils.runtime_signature:', _sig_e)
except Exception as e:
    try:
        import sys
        mytools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mytools')
        if os.path.isdir(mytools_dir) and mytools_dir not in sys.path:
            sys.path.insert(0, mytools_dir)
        add_word = _try_import_add_word()
        HAS_MYTOOLS = True
        print('[STARTUP] mytools helper available via ./mytools on sys.path: frequency_db_utils.word_adder.add_word')

        # Runtime diagnostics: confirm exactly which copy of frequency_db_utils is imported.
        try:
            import sys as _sys
            import frequency_db_utils as _fdu  # type: ignore
            print('[STARTUP] frequency_db_utils.runtime_signature:', _fdu.runtime_signature())
            if '.venv' not in (_sys.executable or '').lower():
                print('[STARTUP] WARNING: app is not running from .venv:', _sys.executable)
        except Exception as _sig_e:
            print('[STARTUP] NOTE: could not read frequency_db_utils.runtime_signature:', _sig_e)
    except Exception as e2:
        print('[STARTUP] mytools helper not available:', e2)
        HAS_MYTOOLS = False
        add_word = None


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


@app.route('/api/runtime_signature', methods=['GET'])
def api_runtime_signature():
    """Return runtime info to verify which mytools/frequency_db_utils is being used."""
    import sys
    import os
    sig = None
    err = None
    try:
        import frequency_db_utils as fdu  # type: ignore
        sig = fdu.runtime_signature()
    except Exception as e:
        err = repr(e)

    return jsonify({
        'success': sig is not None,
        'signature': sig,
        'error': err,
        'sys_executable': sys.executable,
        'cwd': os.getcwd(),
    })



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

        # Selección aleatoria de voz (con fallback robusto si Neural2 no está habilitado en el proyecto)
        if language.startswith('fr') or language.startswith('de'):
            if language.startswith('fr'):
                lang_code = 'fr-FR'
                voice_candidates = [
                    'fr-FR-Neural2-D', 'fr-FR-Neural2-E',
                    'fr-FR-Standard-A', 'fr-FR-Standard-B', 'fr-FR-Standard-C', 'fr-FR-Standard-D', 'fr-FR-Standard-E'
                ]
            else:
                lang_code = 'de-DE'
                voice_candidates = [
                    'de-DE-Neural2-A', 'de-DE-Neural2-B', 'de-DE-Neural2-C', 'de-DE-Neural2-D',
                    'de-DE-Standard-A', 'de-DE-Standard-B', 'de-DE-Standard-C', 'de-DE-Standard-D'
                ]

            random.shuffle(voice_candidates)
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
                        break
                except Exception as e:
                    # If the specific voice isn't available, try the next candidate.
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
        # Common local failure: missing ADC credentials.
        msg = str(e) if e is not None else ''
        if 'default credentials were not found' in msg.lower():
            # Return 200 so the frontend can seamlessly fallback to SpeechSynthesis.
            print('[TTS] Google Cloud credentials missing; falling back to browser TTS')
            return jsonify({'success': False, 'error': 'Google Cloud credentials missing'}), 200
        import traceback
        print(f'[TTS] Exception: {e}')
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Internal error: {e}'}), 500

def get_pg_conn():
    db_url = os.environ.get('DATABASE_URL')
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
    db_url = os.environ.get('DATABASE_URL')
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

# Endpoint para agregar una palabra a la lista activa, traducir al alemán y obtener IPA usando OpenAI
@app.route('/api/add_word', methods=['POST'])
def api_add_word():
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    data = request.get_json()
    word = data.get('word', '').strip()
    if not word:
        return jsonify({'success': False, 'error': 'No word provided'}), 400
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Obtener lista activa
        cur.execute('SELECT last_list FROM users WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.close(); conn.close()
            return jsonify({'success': False, 'error': 'No active list'}), 400
        active_list_id = row[0]
        # Obtener el idioma de la lista activa
        cur.execute('SELECT language FROM word_lists WHERE id = %s', (active_list_id,))
        lang_row = cur.fetchone()
        language = lang_row[0] if lang_row and lang_row[0] else 'de'
        openai.api_key = os.environ.get('OPENAI_API_KEY')
        # If the bundled helper is available, delegate the work to it.
        if HAS_MYTOOLS and callable(add_word):
            try:
                # Print provenance BEFORE calling add_word so we can see which copy is running even if add_word crashes.
                try:
                    import sys as _sys
                    import frequency_db_utils as _fdu  # type: ignore
                    try:
                        from frequency_db_utils import word_adder as _wa  # type: ignore
                        _wa_file = getattr(_wa, '__file__', None)
                    except Exception as _e:
                        _wa_file = {'error': repr(_e)}

                    _runtime_sig = None
                    try:
                        _runtime_sig = _fdu.runtime_signature()
                    except Exception as _e:
                        _runtime_sig = {'error': repr(_e)}

                    print('[api_add_word] mytools sys.executable:', _sys.executable)
                    print('[api_add_word] mytools frequency_db_utils.__file__:', getattr(_fdu, '__file__', None))
                    print('[api_add_word] mytools word_adder.__file__:', _wa_file)
                    print('[api_add_word] mytools runtime_signature:', _runtime_sig)
                    try:
                        print('[api_add_word] add_word.__module__:', getattr(add_word, '__module__', None))
                    except Exception:
                        pass
                except Exception as _e:
                    print('[api_add_word] NOTE: could not compute mytools provenance:', repr(_e))

                print('[api_add_word] Delegating to frequency_db_utils.add_word')
                result = add_word(
                    conn,
                    cur,
                    palabra=word,
                    list_id=active_list_id,
                    language=language,
                    openai_api_key=os.environ.get('OPENAI_API_KEY'),
                    user_id=user_id,
                )

                runtime_sig = None
                try:
                    if isinstance(result, dict):
                        runtime_sig = result.get('runtime_signature')
                except Exception:
                    runtime_sig = None
                if not runtime_sig:
                    try:
                        import frequency_db_utils as _fdu  # type: ignore
                        runtime_sig = _fdu.runtime_signature()
                    except Exception as _e:
                        runtime_sig = {'error': repr(_e)}
                try:
                    print('[api_add_word] freq_utils runtime_signature:', runtime_sig)
                except Exception:
                    pass

                conn.commit()
                cur.close(); conn.close()
                return jsonify({
                    'success': True,
                    'word': word,
                    'association': (result.get('association') if isinstance(result, dict) else None),
                    'IPA_word': (result.get('ipa_word') if isinstance(result, dict) else None),
                    'runtime_signature': runtime_sig,
                }), 200
            except Exception as e:
                import traceback
                print('[ERROR] api_add_word (mytools):', e)
                traceback.print_exc()

                # Diagnose common psycopg2 error: IndexError: tuple index out of range
                # This usually means SQL placeholder count doesn't match params length.
                try:
                    from frequency_db_utils import word_adder as _wa  # type: ignore
                    try:
                        _actions = _wa.prepare_word_actions(
                            palabra=word,
                            list_id=active_list_id,
                            language=language,
                            openai_api_key=os.environ.get('OPENAI_API_KEY'),
                            user_id=user_id,
                        )
                        _queries = _actions.get('queries', []) if isinstance(_actions, dict) else []
                        for _i, _qp in enumerate(_queries):
                            try:
                                _sql, _params = _qp
                            except Exception:
                                print('[ERROR] api_add_word (mytools) bad query tuple at index', _i, ':', repr(_qp))
                                continue

                            _ph = 0
                            try:
                                _ph = int(str(_sql).count('%s'))
                            except Exception:
                                _ph = -1
                            try:
                                _plen = len(_params) if _params is not None else 0
                            except Exception:
                                _plen = -1

                            if _ph >= 0 and _plen >= 0 and _ph != _plen:
                                print('[ERROR] api_add_word (mytools) placeholder mismatch at query index', _i)
                                print('[ERROR] api_add_word (mytools) placeholders=', _ph, 'params_len=', _plen)
                                print('[ERROR] api_add_word (mytools) sql=', repr(_sql))
                                print('[ERROR] api_add_word (mytools) params=', repr(_params))
                                break
                    except Exception as _diag_e:
                        print('[ERROR] api_add_word (mytools) diag failed:', repr(_diag_e))
                except Exception:
                    pass

                # Also print provenance on failure (in case it changed between startup and runtime).
                try:
                    import sys as _sys
                    import frequency_db_utils as _fdu  # type: ignore
                    try:
                        from frequency_db_utils import word_adder as _wa  # type: ignore
                        _wa_file = getattr(_wa, '__file__', None)
                    except Exception as _e:
                        _wa_file = {'error': repr(_e)}
                    try:
                        _runtime_sig = _fdu.runtime_signature()
                    except Exception as _e:
                        _runtime_sig = {'error': repr(_e)}
                    print('[ERROR] api_add_word (mytools) sys.executable:', _sys.executable)
                    print('[ERROR] api_add_word (mytools) frequency_db_utils.__file__:', getattr(_fdu, '__file__', None))
                    print('[ERROR] api_add_word (mytools) word_adder.__file__:', _wa_file)
                    print('[ERROR] api_add_word (mytools) runtime_signature:', _runtime_sig)
                except Exception:
                    pass
                # Fall through to the original implementation as a fallback
        # Traducción directa al español (máx 3 palabras) — indicar idioma origen (language de la lista activa)
        # Mapear códigos de idioma simples a nombres en español para que el prompt sea claro
        lang_code = (language or 'de').lower()
        lang_map = {
            'de': 'alemán', 'de-de': 'alemán',
            'fr': 'francés', 'fr-fr': 'francés',
            'en': 'inglés', 'en-us': 'inglés', 'en-gb': 'inglés',
            'es': 'español', 'es-es': 'español',
            'it': 'italiano', 'pt': 'portugués'
        }
        lang_name = lang_map.get(lang_code, language)
        prompt_translation = (
            f"Traduce la siguiente palabra del idioma {lang_name} al español. Responde SOLO en UNA LÍNEA y usa EXACTAMENTE este formato:\n"
            f"- Si es un sustantivo: '<artículo_original> <palabra>, <traducción_en_español>' (ejemplo: 'die Frau, la mujer')\n"
            f"- Si es un verbo conjugado: '<pronombre> <forma_conjugada>, <traducción_en_español>' (ejemplo: 'ich gehe, yo voy')\n"
            f"IMPORTANTE: Mantén EXACTAMENTE el artículo o el pronombre y la forma en el IDIOMA ORIGINAL en la PARTE IZQUIERDA antes de la coma. NO traduzcas esa parte. Traduce SOLO la parte después de la coma al español.\n"
            f"Si no aplica artículo o pronombre, escribe solo '<palabra>, <traducción>'. NO añadas explicaciones, etiquetas ni otros textos.\n"
            f"RESPUESTA de ejemplo correcta (no traduzcas la parte izquierda): 'ich gehe, yo voy'\n"
            f"RESPUESTA de ejemplo INCORRECTA (evitar): 'yo voy, yo voy'\n"
            f"Palabra: {word}"
        )
        response_tr = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant for language learning."},
                {"role": "user", "content": prompt_translation}
            ],
            max_tokens=30,
            temperature=0.3
        )
        # Limpiar traducción: tomar la primera línea y mantener el formato 'X, traducción'
        raw_translation = response_tr.choices[0].message.content.strip()
        # Quitar prefijos comunes y texto antes de ':' si existen
        prefixes = ["translation", "traducción", f"{lang_name.lower()}:", f"{lang_code}:"]
        cleaned = raw_translation.strip()
        cleaned_lower = cleaned.lower()
        for prefix in prefixes:
            if cleaned_lower.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                cleaned_lower = cleaned.lower()
        if ':' in cleaned:
            cleaned = cleaned.split(':', 1)[1].strip()
        # Tomar solo la primera línea
        first_line = cleaned.split('\n')[0].strip()
        # Normalizar espacios
        association = ' '.join(first_line.split())
        # Guardamos la asociación completa (ej. 'der Haus, casa' o 'ich gehe, yo voy')
        translation = association

        # Obtener el IPA de la palabra original en el idioma de la lista activa
        prompt_ipa = f"Provide the IPA transcription for the following word in {language}:\nWord: {word}\nFormat: <IPA>"
        response_ipa = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant for language learning."},
                {"role": "user", "content": prompt_ipa}
            ],
            max_tokens=30,
            temperature=0.3
        )
        # Limpiar IPA: quitar frases de relleno y dejar solo el IPA
        raw_ipa = response_ipa.choices[0].message.content.strip()
        # Buscar el primer bloque entre /.../ o [...] o después de ':'
        import re
        ipa_match = re.search(r'([/\[].*?[/\]])', raw_ipa)
        if ipa_match:
            ipa = ipa_match.group(1)
        else:
            # Si no hay /.../ o [...], tomar después de ':' o la primera palabra
            if ':' in raw_ipa:
                ipa = raw_ipa.split(':',1)[1].strip().split()[0]
            else:
                ipa = raw_ipa.split()[0]
        # Insertar palabra en la base de datos
        cur.execute('''
            INSERT INTO words (word, association, "IPA_word", list_id, added, counter_word, used, state, successes)
            VALUES (%s, %s, %s, %s, TRUE, 0, FALSE, 'New', 0)
            ON CONFLICT (word, list_id) DO UPDATE SET
                association=EXCLUDED.association,
                "IPA_word"=EXCLUDED."IPA_word",
                added=TRUE,
                used=EXCLUDED.used,
                state=EXCLUDED.state,
                successes=EXCLUDED.successes
        ''', (word, translation, ipa, active_list_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'success': True, 'word': word, 'association': translation, 'IPA_word': ipa})
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
        cur.execute('SELECT id, word, association, "IPA_word", counter_word FROM words WHERE list_id = %s', (active_list_id,))
        words = [{'id': r[0], 'word': r[1], 'association': r[2], 'IPA_word': r[3], 'counter_word': r[4]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify(words)
    except Exception as e:
        print('[ERROR] api_all_words:', e)
        return jsonify([])

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
        threshold = 15
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
        # Peso exponencial: 2^(-counter_word)
        weights = [2 ** (-w[2]) for w in words]
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
                    cur.execute('INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s) RETURNING id', (username, None, pw_hash))
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

        cur.execute('SELECT id, word, counter_word, IPA_word FROM words WHERE list_id = %s AND added = TRUE', (active_list_id,))
        words = cur.fetchall()
        if not words:
            cur.close(); conn.close()
            return render_template('random_word.html', error='No words in list', active_list=active_list, all_lists=all_lists)
        # Peso exponencial: 2^(-counter_word)
        weights = [2 ** (-w[2]) for w in words]
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
