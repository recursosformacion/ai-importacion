# AI Importación

Aplicación independiente para localizar, registrar y revisar materiales musicales publicados en Internet Archive. Su base de datos es propia: no replica el esquema de OSAP y **no escribe en OSAP ni en Storage**.

## Funciones del primer bloque

- Búsqueda en Internet Archive por título, creador, materia y descripción.
- Exploración global reanudable mediante la API de cursor de Internet Archive, con cinco familias de consultas, cursores persistidos y deduplicación de identificadores entre consultas.
- Persistencia del identificador estable, URL de origen, campos de catálogo y metadatos originales recibidos.
- Historial de búsquedas y relación entre cada búsqueda y sus resultados.
- Catálogo local con filtro y ficha de cada elemento.
- Estado de revisión de derechos explícito. Ver o descargar un archivo no implica permiso para redistribuirlo.
- API de comprobación `GET /health`.
- Análisis manual del alcance de cada elemento: obra única, varias obras, colección/volumen, no musical o pendiente.
- Registro de obras candidatas dentro de un elemento, con compositor, fichero, páginas/localizador y evidencia (índice, portada, OCR, etc.). Dos candidatas sugieren una recopilación, sin sustituir una decisión editorial explícita.
- Extracción conservadora de líneas candidatas desde ficheros OCR `.txt`, texto extraíble de PDF y OCR local opcional en las primeras cinco páginas de PDF escaneados. El OCR requiere Tesseract instalado en el sistema además de las dependencias Python. Las sugerencias se revisan y aceptan manualmente; no se convierten automáticamente en obras.
- Análisis local de MusicXML/MXL descargados: extrae título, creadores, partes/instrumentos y compases; guarda el resultado y los errores en SQLite, sin alterar los archivos ni escribir en OSAP.
- Esta fase es solo de catalogación: no crea ni exporta obras OSAP. El OCR se ejecuta localmente y no envía documentos a servicios externos.

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

- La búsqueda consulta metadatos de Internet Archive (título, creador, materias, descripción y formato) y, por defecto, abarca todos los tipos de material; se puede limitar a `mediatype:texts`. La exploración global recorre por cursor cinco familias de consultas y guarda su avance en SQLite; cada pulsación procesa un lote por consulta que no haya terminado. Los totales por familia pueden solaparse y no equivalen al número de partituras únicas. La cobertura no es exhaustiva: las consultas pueden omitir materiales mal descritos y el filtro puede aceptar falsos positivos. No lee el contenido de todos los PDF; la extracción local solo analiza texto OCR descargado o texto que pueda extraerse de un PDF.
- La pantalla **Captación integrada** (`/intake`) combina inventario y descarga en una ejecución controlada: inventaría un lote pequeño de candidatos aún no inventariados y descarga hasta el número seleccionado de archivos ya registrados, priorizando formatos editables de notación y PDF. No descarga en masa, respeta el límite por fichero y calcula SHA-256. Los formatos desconocidos siguen inventariándose, pero no se descargan automáticamente hasta que haya indicios que permitan clasificarlos.\n- El inventario de ficheros ya consulta la API de metadatos de Archive.org y registra nombre, formato, tamaño, MD5/SHA-1 de origen y URL. Los formatos candidatos (PDF, MusicXML/MXL, MIDI, ZIP, formatos de notación, imágenes de páginas y algunos ficheros de texto OCR) se pueden descargar individualmente con límite de tamaño y SHA-256 local. Las descargas quedan fuera de Git, en `downloads/`.
- Se conservan los metadatos originales de la búsqueda; todavía no se crean obras, compositores, representaciones ni recursos OSAP.
- La futura exportación debe usar los contratos de OSAP API y Storage, ser idempotente, disponer de dry-run y registrar resultados. No se deben insertar filas directamente en sus bases de datos.

## Modelo local

- `search_runs`: consulta, expresión enviada, número solicitado, estado, totales y marcas de tiempo.
- `global_discovery_queries`: familias de consulta, cursor de continuación, totales declarados y estado de avance.
- `global_discovery_seen`: identificadores únicos ya examinados por la exploración global, puntuación y motivo del filtro.
- `archive_items`: identificador de Archive.org, campos visibles, JSON original, trazabilidad, revisión editorial y estado de derechos.
- `archive_files`: inventario por elemento, formato, tamaño, checksums y estado/ruta de descarga local.
- `archive_content_assessments`: clasificación y justificación del alcance del elemento.
- `archive_item_contents`: unidades/obras candidatas localizadas dentro del elemento, con evidencia y localizador.
- `archive_content_suggestions`: líneas detectadas en texto/OCR, pendientes de aceptación humana.
- `search_run_items`: asociación entre una búsqueda y los elementos devueltos.

Los identificadores de Archive.org son únicos en el catálogo local. Si una nueva búsqueda devuelve el mismo identificador, se actualizan los metadatos y se conserva el primer momento de captación.
