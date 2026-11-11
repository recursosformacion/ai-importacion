# AI Importación

Aplicación independiente para localizar, registrar y revisar materiales musicales publicados en Internet Archive. Su base de datos es propia: no replica el esquema de OSAP y **no escribe en OSAP ni en Storage**.

## Funciones del primer bloque

- Búsqueda en Internet Archive por título, creador, materia y descripción.
- Exploración global reanudable mediante la API de cursor de Internet Archive, con seis familias de consultas, cursores persistidos y deduplicación de identificadores entre consultas.
- Persistencia del identificador estable, URL de origen, campos de catálogo y metadatos originales recibidos.
- Historial de búsquedas y relación entre cada búsqueda y sus resultados.
- Catálogo local paginado (50 elementos por página), con filtro y ficha de cada elemento.
- Estado de revisión de derechos explícito e historial de decisiones con URL de evidencia, base, jurisdicción y notas. Ver o descargar un archivo no implica permiso para redistribuirlo.
- API de comprobación `GET /health`.
- Desde la ficha de un elemento, **Buscar, descargar y analizar archivos** consulta el inventario de Internet Archive, descarga hasta tres archivos musicales prioritarios y encola el análisis compatible. El inventario y la descarga no requieren pasos manuales; los formatos sin analizador se indican en la ficha como pendientes.
- Análisis manual del alcance de cada elemento: obra única, varias obras, colección/volumen, no musical o pendiente.
- Registro de obras candidatas dentro de un elemento, con compositor, fichero, páginas/localizador y evidencia (índice, portada, OCR, etc.). Dos candidatas sugieren una recopilación, sin sustituir una decisión editorial explícita.
- Extracción de títulos candidatos desde ficheros OCR `.txt`, texto extraíble de PDF y OCR local opcional en las primeras cinco páginas de PDF escaneados. Se filtran fragmentos breves de OCR, créditos editoriales y muchas líneas con apariencia de letra en vez de título; aun así, los resultados son indicios y no se convierten automáticamente en obras. El OCR requiere Tesseract instalado en el sistema además de las dependencias Python.
- Análisis conservador de paquetes Capella `.capx`: busca títulos explícitos dentro del XML, con límites de tamaño. Los formatos binarios sin analizador específico permanecen identificados como pendientes.
- Análisis local de MusicXML/MXL descargados: valida la estructura de partitura y extrae título, creadores, partes/instrumentos y compases. También valida MIDI/KAR para registrar pistas, instrumentos, eventos de nota, duración, compás y tonalidad cuando están declarados. Los XML genéricos de Archive.org (metadatos, MARC, Dublin Core y ficheros auxiliares) no se consideran partituras ni errores de análisis; el XML solo se descarga automáticamente cuando los metadatos o el nombre indican que puede ser una partitura. Los resultados y errores quedan en SQLite, sin alterar los archivos ni escribir en OSAP.
- La cola de revisión muestra 50 casos por página e indica el total y el rango visible. Para mantener ágil el análisis local, se extrae texto de hasta 50 páginas por PDF y el OCR de PDF escaneados se limita a cinco. Sibelius, Finale y otros formatos binarios pueden reconocerse por extensión/metadatos y descargarse, pero requieren revisión mientras no haya un analizador específico; la aplicación no afirma que su contenido haya sido validado.
- La extracción de texto PDF usa `fontTools` junto con `pypdf` para interpretar mejor fuentes Type 1/CFF.
- Esta fase es solo de catalogación: no crea ni exporta obras OSAP. El OCR se ejecuta localmente y no envía documentos a servicios externos.
- La captación completa y el análisis automático se encolan en SQLite y se reanudan al iniciar la aplicación si un proceso anterior se cerró mientras estaban trabajando. La captación no espera al análisis/OCR: cada trabajo tiene un estado consultable y el análisis se ejecuta independientemente del ciclo de peticiones web.

## Requisitos y arranque

Python 3.11 o posterior.

Configuración opcional: `AI_IMPORTACION_DOWNLOAD_DIR` cambia el directorio de descarga; `AI_IMPORTACION_MAX_DOWNLOAD_BYTES` cambia el límite por fichero (por defecto 100 MiB); `AI_IMPORTACION_OCR_LANG` define los idiomas Tesseract (por defecto `eng`; usa, por ejemplo, `eng+spa` solo si instalaste ambos paquetes de idioma).

```bash
python -m venv .venv
# Windows PowerShell:
.venv\\Scripts\\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 9500
```

Abrir http://127.0.0.1:9500. La base de datos se crea en `data/ai-importacion.sqlite3`. Para cambiar la ruta, definir `AI_IMPORTACION_DB` antes de arrancar.

### OCR de PDF escaneados en Windows

El OCR es opcional y se ejecuta en el equipo local. `pip install -r requirements.txt` instala las bibliotecas Python, pero **no instala el ejecutable Tesseract**. Instala Tesseract OCR para Windows, añade la carpeta que contiene `tesseract.exe` al `PATH` y abre una nueva terminal. Comprueba la instalación con `tesseract --version`. Para verificar los idiomas disponibles, ejecuta `tesseract --list-langs`; configura `AI_IMPORTACION_OCR_LANG` solo con idiomas instalados. Si Tesseract no está disponible, la aplicación conserva el estado del PDF y lo indica en la ficha, sin enviar el documento a un servicio externo. El OCR procesa como máximo las primeras cinco páginas por PDF.

## Pruebas

```bash
python -m pytest -q
```

Las pruebas no necesitan una conexión real a Internet Archive; la búsqueda HTTP se prueba con una respuesta simulada. La aplicación en ejecución consulta el endpoint público `https://archive.org/advancedsearch.php`.

## Límites deliberados

- La búsqueda consulta metadatos de Internet Archive (título, creador, materias, descripción y formato) y, por defecto, abarca todos los tipos de material; se puede limitar a `mediatype:texts`. La exploración global recorre por cursor seis familias de consultas y guarda su avance en SQLite; cada pulsación procesa un lote por consulta que no haya terminado. Los totales por familia pueden solaparse y no equivalen al número de partituras únicas. La cobertura no es exhaustiva: las consultas pueden omitir materiales mal descritos y el filtro puede aceptar falsos positivos. No lee el contenido de todos los PDF; la extracción local solo analiza texto OCR descargado o texto que pueda extraerse de un PDF.
- La pantalla **Captación integrada** (`/intake`) combina inventario y descarga en una ejecución controlada: inventaría un lote pequeño de candidatos aún no inventariados y descarga hasta el número seleccionado de archivos ya registrados, priorizando formatos editables de notación y PDF. No descarga en masa, respeta el límite por fichero y calcula SHA-256. Los formatos desconocidos siguen inventariándose, pero no se descargan automáticamente hasta que haya indicios que permitan clasificarlos. Capella (.capx) se trata de forma conservadora como paquete ZIP: solo se leen miembros XML con límites de tamaño y solo se proponen títulos explícitos; Sibelius (.sib), Finale (.musx) y otros formatos binarios siguen pendientes de analizadores específicos.\n- El inventario de ficheros ya consulta la API de metadatos de Archive.org y registra nombre, formato, tamaño, MD5/SHA-1 de origen y URL. Los formatos candidatos (PDF, MusicXML/MXL, MIDI, ZIP, formatos de notación, imágenes de páginas y algunos ficheros de texto OCR) se pueden descargar individualmente con límite de tamaño y SHA-256 local. Las descargas quedan fuera de Git, en `downloads/`.
- Se conservan los metadatos originales de la búsqueda; todavía no se crean obras, compositores, representaciones ni recursos OSAP.
- La futura exportación debe usar los contratos de OSAP API y Storage, ser idempotente, disponer de dry-run y registrar resultados. No se deben insertar filas directamente en sus bases de datos.

## Modelo local

- `search_runs`: consulta, expresión enviada, número solicitado, estado, totales y marcas de tiempo.
- `global_discovery_queries`: familias de consulta, cursor de continuación, totales declarados y estado de avance.
- `global_discovery_seen`: identificadores únicos ya examinados por la exploración global, puntuación y motivo del filtro.
- `archive_items`: identificador de Archive.org, campos visibles, JSON original, trazabilidad, revisión editorial y estado de derechos.
- `archive_rights_reviews`: historial de decisiones de derechos, con evidencia y contexto de revisión.
- `capture_jobs`: estado durable de las ejecuciones de captación; las tareas en curso se reencolan al arrancar.
- `content_analysis_jobs`: cola durable de análisis automático, independiente de las descargas y con estado consultable.
- `archive_files`: inventario por elemento, formato, tamaño, checksums y estado/ruta de descarga local.
- `archive_content_assessments`: clasificación y justificación del alcance del elemento.
- `archive_item_contents`: unidades/obras candidatas localizadas dentro del elemento, con evidencia y localizador.
- `archive_content_suggestions`: líneas detectadas en texto/OCR, pendientes de aceptación humana.
- `search_run_items`: asociación entre una búsqueda y los elementos devueltos.

Los identificadores de Archive.org son únicos en el catálogo local. Si una nueva búsqueda devuelve el mismo identificador, se actualizan los metadatos y se conserva el primer momento de captación.


## Ecosistema OSAP

Este repositorio es una herramienta independiente del ecosistema **OSAP — Open Score Acquisition Platform**. Su base de datos es propia y el flujo actual de importación sirve para descubrir y revisar materiales; no escribe directamente en el catálogo de producción.

- **[OSAP core](https://github.com/recursosformacion/AI_GAMEPC2)** — motor principal de resolución de recursos musicales.
- [ai_recogeYouTube](https://github.com/recursosformacion/ai_recogeYouTube) — descubrimiento y revisión de grabaciones de YouTube.
- [AI_osap-auth](https://github.com/recursosformacion/AI_osap-auth) — servicio de identidad y autenticación.
- [AI_osap-storage](https://github.com/recursosformacion/AI_osap-storage) — servicio de datos y almacenamiento del catálogo.
- [AI_osap-support](https://github.com/recursosformacion/AI_osap-support) — soporte, donaciones y membresías.
- Sitio público: [openmusicrepository.com](https://openmusicrepository.com)

## Fuente adicional: GitHub

La aplicación incorpora un lector de repositorios públicos de GitHub, accesible en `/github`.

- Permite registrar un repositorio mediante `propietario/repositorio` o su URL de GitHub.
- Consulta los metadatos públicos del repositorio y conserva la licencia declarada por GitHub cuando existe. La licencia del repositorio no se considera automáticamente aplicable a cada fichero.
- Descarga en local una copia completa y superficial (último commit de la rama predeterminada) del repositorio, incluidos sus ficheros ordinarios; no ejecuta scripts del proyecto, no inicializa submódulos y omite la descarga de objetos Git LFS.
- Procesa localmente los ficheros musicales del clon y genera `resources.json` con ruta, formato, SHA, título identificado y datos de autoría cuando están disponibles. La carpeta completa queda en `data/github-repositories/<id>/`.
- Si falta un título explícito en MusicXML/MXL, utiliza Gemini `gemini-flash-lite-latest` para proponer título, compositor, arreglista e instrumentación a partir del contenido legible. Las propuestas se identifican en el JSON como procedentes de IA y no se presentan como datos verificados.
- Reconoce inicialmente MusicXML/XML, MXL, MIDI/KAR, PDF, Humdrum/Kern, LilyPond, ABC, MEI, MuseScore, Capella, Sibelius, Finale y ZIP.
- Las lecturas se pueden repetir para actualizar el inventario. GitHub informa cuando el árbol está truncado; en ese caso el resultado se presenta como parcial.
- El clon local y el JSON no se mezclan con las tablas de Internet Archive ni se exportan a OSAP automáticamente.
- La API pública de GitHub tiene límites de consulta. Se puede configurar opcionalmente `GITHUB_TOKEN` en el entorno del servicio para aumentar el límite; no se introduce el token en la interfaz.

### Primer uso de GitHub

1. Abre `/github`.
2. Añade, por ejemplo, `OpenScore/Lieder` o `OpenScore/StringQuartets`.
3. En la ficha, pulsa **Procesar repositorio**.
4. La aplicación consulta primero el árbol completo del repositorio y localiza los ficheros musicales.
5. Descarga en una única operación agrupada los blobs seleccionados mediante `git sparse-checkout` (no hace peticiones individuales por fichero ni descarga el resto del repositorio).
6. Procesa localmente esos ficheros y genera el manifiesto `resources.json` en `data/github-repositories/<id>/.osap-import/`.

La disponibilidad pública de un fichero no acredita permiso para redistribuirlo. Antes de incorporar contenidos hay que revisar la licencia aplicable y las condiciones del proyecto de origen.

### Configuración de Gemini

Crea `secrets.txt` en la raíz del proyecto a partir de `secrets_example.txt` y pega la clave obtenida en Google AI Studio. `secrets.txt` está excluido de Git. También se admite `GEMINI_API_KEY` o `GEMINI_API_KEYS` como variable de entorno, y varias claves separadas por comas. El modelo predeterminado es `gemini-flash-lite-latest`; el límite inicial es de 100 ficheros por ejecución para controlar el consumo. Si no hay clave o falla la identificación, el procesamiento local continúa y deja el título como candidato sin inventarlo.


## Derechos de redistribución de partituras MXL y LilyPond

La licencia se decide **por fichero**, nunca por la licencia general del repositorio o del elemento que lo contiene.

- Solo una licencia individual verificable **CC BY** o **CC0** permite conservar y servir públicamente un fichero .mxl o .ly.
- Se guardan la licencia original, su URL/evidencia, los créditos identificados y el crédito del transcriptor por separado.
- Licencias NC, ND, SA, restrictivas, dudosas o no verificables mantienen únicamente los metadatos y el enlace a la fuente. El fichero de trabajo se elimina después del análisis.
- Los ficheros GitHub se procesan en un clon temporal. Solo se copian a AI_IMPORTACION_DOWNLOAD_DIR los MXL/LilyPond cuya licencia individual permite redistribución; el clon temporal se elimina al terminar.
- Los ficheros Archive.org sin autorización individual se migran a estado external_only; sus copias locales antiguas se eliminan tras crear una copia de seguridad SQLite previa a la migración.
- Los endpoints de servicio local comprueban tanto la decisión de derechos como que la ruta esté dentro del almacenamiento configurado.

La licencia declarada por un repositorio, por Archive.org o por una página de catálogo no se convierte automáticamente en licencia del fichero. Cuando no hay evidencia individual suficiente, el comportamiento predeterminado es enlace externo.
