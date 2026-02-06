# Despliegue de submódulos privados (Render / CI)

Este documento explica cómo permitir que el entorno de despliegue (por ejemplo Render) pueda clonar submódulos privados, y proporciona snippets de `build` para que la fase de compilación inicialice los submódulos antes de instalar dependencias.

## Resumen

- Añade la clave privada (deploy key) del submódulo como un *secret* en tu servicio de despliegue.
- En el `build` del servicio, escribe la clave a un fichero, ajusta `GIT_SSH_COMMAND` para usarla y ejecuta:

```
git submodule update --init --recursive
```

## Instrucciones para Render (Linux build)

1. En el dashboard de Render (o tu CI), crea una secret/env var llamada `MYTOOLS_DEPLOY_KEY` que contenga la clave privada SSH (PEM) del deploy key (sin passphrase). Ejemplo: `-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----`.

2. Configura el *Build Command* con este bloque (añádelo **antes** de la instalación de dependencias):

```bash
# Guardar la clave privada si se proporcionó
if [ -n "$MYTOOLS_DEPLOY_KEY" ]; then
  mkdir -p ~/.ssh
  printf '%s' "$MYTOOLS_DEPLOY_KEY" > ~/.ssh/mytools_deploy_key
  chmod 600 ~/.ssh/mytools_deploy_key
  export GIT_SSH_COMMAND='ssh -i ~/.ssh/mytools_deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no'
fi

# Inicializar submódulos
git submodule update --init --recursive

# Continuar con el resto del build (ejemplo)
pip install -r requirements.txt
```

Notas:
- `StrictHostKeyChecking=no` evita que el build falle por verificación de host; para mayor seguridad añade la huella del host al `~/.ssh/known_hosts` en vez de deshabilitarlo.
- Asegúrate de que la clave no tiene passphrase o que el sistema puede desbloquearla automáticamente.

## Alternativa: usar token HTTPS (si no quieres usar SSH)

Si prefieres usar un token (por ejemplo `GIT_TOKEN`) en vez de claves SSH, puedes forzar que Git use HTTPS con credenciales en la URL:

```bash
if [ -n "$GIT_TOKEN" ]; then
  git config --global url."https://$GIT_TOKEN@github.com/".insteadOf "https://github.com/"
fi
git submodule update --init --recursive
```

Este método funciona sin claves SSH, pero ten cuidado con la exposición del token en logs — usa secrets y permisos mínimos (repos privados sólo si es necesario).

## Recomendaciones de seguridad

- Limita el alcance de la deploy key en GitHub al repositorio exigido (no darle acceso push si no hace falta).
- Rota la clave periódicamente y monitoriza los accesos.

---

Archivo creado por el script de automatización. Si quieres que añada este documento al PR del submódulo o lo incluya en otro PR, dímelo y lo subo automáticamente.
