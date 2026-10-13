# AI Importación

Aplicación independiente para localizar, registrar y revisar materiales musicales publicados en Internet Archive. Su base de datos es propia: no replica el esquema de OSAP y **no escribe en OSAP ni en Storage**.

## Funciones del primer bloque

- Búsqueda en Internet Archive por título, creador, materia y descripción.
- Persistencia del identificador estable, URL de origen, campos de catálogo y metadatos originales recibidos.
- Historial de búsquedas y relación entre cada búsqueda y sus resultados.
- Catálogo local con filtro y ficha de cada elemento.
- Estado de revisión de derechos explícito. Ver o descargar un archivo no implica permiso para redistribuirlo.
- API de comprobación `GET /health`.

## Requisitos y arranque

Python 3.11 o posterior.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\\Scripts\\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Abrir http://127.0.0.1:8000. La base de datos se crea en `data/ai-importacion.sqlite3`. Para cambiar la ruta, definir `AI_IMPORTACION_DB` antes de arrancar.

## Pruebas

```bash
python -m pytest -q
```

Las pruebas no necesitan una conexión real a Internet Archive; la búsqueda HTTP se prueba con una respuesta simulada. La aplicación en ejecución consulta el endpoint público `https://archive.org/advancedsearch.php`.

## Límites deliberados

- La búsqueda inicial se restringe a elementos `mediatype:texts` y a los campos de metadatos indicados. No garantiza encontrar todos los materiales musicales ni todos los formatos.
- Este bloque no descarga archivos binarios ni inspecciona su formato. El inventario de ficheros, la descarga controlada, los checksums y la detección de duplicados son una fase posterior.
- Se conservan los metadatos originales de la búsqueda; todavía no se crean obras, compositores, representaciones ni recursos OSAP.
- La futura exportación debe usar los contratos de OSAP API y Storage, ser idempotente, disponer de dry-run y registrar resultados. No se deben insertar filas directamente en sus bases de datos.

## Modelo local

- `search_runs`: consulta, expresión enviada, número solicitado, estado, totales y marcas de tiempo.
- `archive_items`: identificador de Archive.org, campos visibles, JSON original, trazabilidad, revisión editorial y estado de derechos.
- `search_run_items`: asociación entre una búsqueda y los elementos devueltos.

Los identificadores de Archive.org son únicos en el catálogo local. Si una nueva búsqueda devuelve el mismo identificador, se actualizan los metadatos y se conserva el primer momento de captación.
