# MyAnki - LightApp

Aplicacion web ligera (Flask) para practicar vocabulario tipo Anki.

## Requisitos

- Python 3.10+
- PostgreSQL accesible via `DATABASE_URL`
- (Opcional) `OPENAI_API_KEY`
- (Opcional) credenciales Google TTS

## Instalacion local

```powershell
python -m venv .venv
.\activate_venv.ps1
pip install -r requirements.txt
```

## Variables de entorno

- `DATABASE_URL`
  - Ejemplo: `postgresql://USER:PASSWORD@HOST:5432/postgres?sslmode=require`
- `OPENAI_API_KEY` (opcional)
- `GOOGLE_APPLICATION_CREDENTIALS` o `GOOGLE_APPLICATION_CREDENTIALS_JSON` (opcional)

## Ejecutar

```powershell
python app_light.py
```

La app levanta por defecto en `http://127.0.0.1:5000`.

## Produccion (gunicorn)

```bash
gunicorn -w 2 -b 0.0.0.0:$PORT LightApp.app_light:app
```

