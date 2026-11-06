# Ejecución local en Windows

La aplicación se ejecuta como servicio Python en el puerto **9500**. No necesita Docker ni cambios en Apache.

## Primer arranque y arranques posteriores

Desde el directorio del proyecto:

- Doble clic en \`scripts/start-local.bat\`, o
- Ejecuta \`.\scripts\start-local.ps1\` desde PowerShell.

El script crea \`.venv\` si no existe, instala/actualiza las dependencias de \`requirements.txt\`, crea los directorios persistentes y arranca Uvicorn.

```powershell
Set-Location D:\Proyectos\AI_OSAP\ai-importacion
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 9500
```

Abre la aplicación mediante la dirección Apache que ya tengas configurada, o directamente http://127.0.0.1:9500. Comprobación: http://127.0.0.1:9500/health

Para detenerla, pulsa Ctrl+C o cierra la ventana de consola.

## Datos persistentes

Los datos se guardan dentro del proyecto, independientemente del directorio desde el que se invoque el lanzador:

- SQLite: \`data/ai-importacion.sqlite3\`
- Archivos descargados: \`downloads/\`
- Entorno Python: \`.venv/\`

Estos directorios no se deben borrar al actualizar el código. La base de datos sigue siendo SQLite; no se conecta a las bases de datos de OSAP.

## Apache como proxy inverso (opcional)

Uvicorn escucha únicamente en \`127.0.0.1\`, por lo que no queda expuesto directamente a otros equipos. Apache puede reenviar las peticiones al puerto local 9500.

Activa los módulos \`mod_proxy\` y \`mod_proxy_http\`. En un VirtualHost dedicado, utiliza:

\`\`\`apache
ProxyPreserveHost On
ProxyPass        / http://127.0.0.1:9500/
ProxyPassReverse / http://127.0.0.1:9500/
\`\`\`

La aplicación debe ocupar la raíz de ese VirtualHost (por ejemplo, un subdominio propio), no una subruta como \`/ai-importacion/\`: la interfaz utiliza rutas absolutas desde \`/\`.

Si Apache sirve HTTPS, configura el certificado en ese VirtualHost como haces con el resto de tus sitios. Si el acceso va a ser posible desde otros equipos o desde Internet, añade autenticación en Apache y limita el acceso: la aplicación de investigación no incorpora todavía un sistema propio de usuarios.

No es necesario copiar el proyecto a \`htdocs\` ni modificar los archivos del sitio principal.

## OCR

El análisis y la extracción de texto se ejecutan localmente. Para OCR de PDF escaneados e imágenes instala Tesseract OCR para Windows, añade su directorio al \`PATH\` y comprueba que están instalados los idiomas configurados en \`AI_IMPORTACION_OCR_LANG\` (por defecto \`eng+spa\`). Si Tesseract no está instalado, el resto de la aplicación funciona, pero el OCR no estará disponible.
