# MyAnki — LightApp

Aplicación web ligera (Flask) para practicar vocabulario tipo Anki: listas por usuario, palabra aleatoria con TTS, y utilidades para agregar palabras y gestionar el DB.

## Qué hace

- Autenticación básica (registro/login) por usuario.
- Manejo de **listas de palabras** ("active list" guardada en `users.last_list`).
- Vista principal con:
  - Selector de lista activa.
  - “Words table” (modal) con tabla de palabras de la lista activa y ordenado por columnas.
- Endpoints API para:
  - Obtener palabras (`/api/all_words`, `/api/inactive_words`).
  - Marcar palabras como agregadas (`/api/mark_word_added`).
  - Ajustar contador (`/api/update_counter`).
  - Obtener palabra aleatoria con peso por contador (`/api/random_word`).
  - TTS con Google Cloud (`/api/tts`).
  - Agregar palabra usando OpenAI + IPA (con soporte opcional de `mytools`) (`/api/add_word`).

## Stack

- Python + Flask
- PostgreSQL (`psycopg2`)
- OpenAI (`openai`)
- Google Cloud Text-to-Speech (`google-cloud-texttospeech`)
- `Flask-Cors` (opcional, pero recomendado en producción)
- `gunicorn` para despliegue

## Requisitos

- Python 3.10+ (recomendado)
- Una base de datos PostgreSQL accesible vía `DATABASE_URL`
- (Opcional) credenciales de Google Cloud Text-to-Speech
- (Opcional) `OPENAI_API_KEY` para traducción + IPA

## Instalación (local)

### 1) Crear entorno virtual

En Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Instalar dependencias

```powershell
pip install -r requirements.txt
```

Nota: `requirements.txt` incluye `-e ./mytools` (instalación editable del submódulo). Si tu carpeta `mytools/` no está inicializada, ejecuta:

```powershell
git submodule update --init --recursive
```

El submódulo `mytools` apunta a:
- https://github.com/eduardebh/MyTools

(Alternativa) Usa el script: `update_mytools.ps1`.

## Variables de entorno

Configura estas variables antes de ejecutar la app:

### Obligatorias

- `DATABASE_URL`
  - Ejemplo: `postgresql://USER:PASSWORD@HOST:5432/DBNAME`

### Recomendadas (funcionalidad extra)

- `OPENAI_API_KEY`
  - Se usa para traducción/asociación y para obtener IPA cuando no se delega a `mytools`.

- Google TTS (elige una opción)
  - `GOOGLE_APPLICATION_CREDENTIALS`: ruta a un JSON de service account (lo estándar en local).
  - `GOOGLE_APPLICATION_CREDENTIALS_JSON`: contenido del JSON como string (útil en Render/CI). La app lo escribe a un fichero temporal y setea `GOOGLE_APPLICATION_CREDENTIALS` automáticamente.

### CORS / Cookies (útil si hay frontend separado)

- `CORS_ORIGINS`
  - Por defecto: `*` (modo diagnóstico).
  - Recomendado en producción: un origen específico o una lista separada por comas.

- `SESSION_COOKIE_SAMESITE`
  - Por defecto: `None`.

- `SESSION_COOKIE_SECURE`
  - Por defecto: `True`.
  - En local con HTTP (sin HTTPS) puede que necesites `False`.

## Ejecutar

### Desarrollo (local)

```powershell
python app_light.py
```

La app levanta por defecto en `http://127.0.0.1:5000`.

### Producción (ejemplo con gunicorn)

```bash
gunicorn -w 2 -b 0.0.0.0:5000 app_light:app
```

### Render (start command)

En Render, asegúrate de **bindear al puerto** que Render expone en `PORT`.

```bash
gunicorn -w 2 -b 0.0.0.0:$PORT LightApp.app_light:app
```

Nota: este repo incluye un wrapper `LightApp/app_light.py` para soportar el formato `LightApp.app_light:app`.

## Migraciones

Hay un runner simple en `db_migrate.py` que aplica SQLs en `migrations/`.

- Ver estado:
  ```
  python db_migrate.py status
  ```
- Aplicar migraciones pendientes:
  ```
  python db_migrate.py apply
  ```

Más detalles en [DB_MIGRATIONS.md](DB_MIGRATIONS.md).

## Despliegue con submódulos

En despliegues/CI, asegúrate de inicializar los submódulos antes de instalar dependencias.

- Para submódulos públicos normalmente basta con:
  ```
  git submodule update --init --recursive
  ```
- Si algún submódulo fuese privado, revisa [DEPLOY_SUBMODULES.md](DEPLOY_SUBMODULES.md) para configurar access tokens / deploy keys.

## Tests

Si tienes `pytest` instalado:

```powershell
python -m pytest -q
```

## Troubleshooting

- **Pylance: Import "frequency_db_utils.word_adder" could not be resolved**
  - Asegúrate de haber instalado `-e ./mytools` (`pip install -r requirements.txt`).
  - Alternativa: la configuración de VS Code puede incluir `python.analysis.extraPaths` apuntando a `./mytools`.

- **Google TTS no funciona**
  - Verifica credenciales (`GOOGLE_APPLICATION_CREDENTIALS` o `GOOGLE_APPLICATION_CREDENTIALS_JSON`) y permisos de la service account.

---

Proyecto interno/experimental. Ajusta `app.secret_key` y configuración de seguridad antes de usar en producción.

