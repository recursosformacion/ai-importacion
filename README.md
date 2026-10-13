# AI Importación

Aplicación independiente para localizar, registrar y revisar materiales musicales publicados en Internet Archive. Su base de datos es propia: no replica el esquema de OSAP y **no escribe en OSAP ni en Storage**.

## Funciones del primer bloque

- Búsqueda en Internet Archive por título, creador, materia y descripción.
- Persistencia del identificador estable, URL de origen, campos de catálogo y metadatos originales recibidos.
- Historial de búsquedas y relación entre cada búsqueda y sus resultados.
- Catálogo local con filtro y ficha de cada elemento.
- Estado de revisión de derechos explícito. Ver o descargar un archivo no implica permiso para redistribuirlo.
- API de comprobación `GET /health`.
- Análisis manual del alcance de cada elemento: obra única, varias obras, colección/volumen, no musical o pendiente.
- Registro de obras candidatas dentro de un elemento, con compositor, fichero, páginas/localizador y evidencia (índice, portada, OCR, etc.). Dos candidatas sugieren una recopilación, sin sustituir una decisión editorial explícita.
- Extracción conservadora de líneas candidatas desde ficheros OCR `.txt` y texto extraíble de PDF descargados. Las sugerencias se revisan y aceptan manualmente; no se convierten automáticamente en obras.
- Esta fase es solo de catalogación: no crea ni exporta obras OSAP.

## Requisitos y arranque

Python 3.11 o posterior.

Configuración opcional: `AI_IMPORTACION_DOWNLOAD_DIR` cambia el directorio de descarga; `AI_IMPORTACION_MAX_DOWNLOAD_BYTES` cambia el límite por fichero (por defecto 100 MiB).

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

- La búsqueda consulta metadatos de Internet Archive (título, creador, materias y descripción) y, por defecto, abarca todos los tipos de material; se puede limitar a `mediatype:texts`. No lee el contenido de todos los PDF ni garantiza encontrar todos los materiales musicales. La extracción local solo analiza texto OCR descargado o texto que pueda extraerse de un PDF.
- El inventario de ficheros ya consulta la API de metadatos de Archive.org y registra nombre, formato, tamaño, MD5/SHA-1 de origen y URL. Los formatos candidatos (PDF, MusicXML/MXL, MIDI, ZIP, formatos de notación, imágenes de páginas y algunos ficheros de texto OCR) se pueden descargar individualmente con límite de tamaño y SHA-256 local. Las descargas quedan fuera de Git, en `downloads/`.
- Se conservan los metadatos originales de la búsqueda; todavía no se crean obras, compositores, representaciones ni recursos OSAP.
- La futura exportación debe usar los contratos de OSAP API y Storage, ser idempotente, disponer de dry-run y registrar resultados. No se deben insertar filas directamente en sus bases de datos.

## Modelo local

- `search_runs`: consulta, expresión enviada, número solicitado, estado, totales y marcas de tiempo.
- `archive_items`: identificador de Archive.org, campos visibles, JSON original, trazabilidad, revisión editorial y estado de derechos.
- `archive_files`: inventario por elemento, formato, tamaño, checksums y estado/ruta de descarga local.
- `archive_content_assessments`: clasificación y justificación del alcance del elemento.
- `archive_item_contents`: unidades/obras candidatas localizadas dentro del elemento, con evidencia y localizador.
- `archive_content_suggestions`: líneas detectadas en texto/OCR, pendientes de aceptación humana.
- `search_run_items`: asociación entre una búsqueda y los elementos devueltos.

Los identificadores de Archive.org son únicos en el catálogo local. Si una nueva búsqueda devuelve el mismo identificador, se actualizan los metadatos y se conserva el primer momento de captación.
