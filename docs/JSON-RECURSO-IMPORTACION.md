# Exportación JSON de recursos

La exportación GitHub genera un paquete ZIP a partir de los ficheros ya descargados. Cada fichero descargado representa, en esta fase, un recurso y obtiene un documento JSON independiente.

## Contenido del paquete

- `resources/<resource_key>.json`: un documento por recurso.
- `files/<resource_key>.<ext>`: el único fichero físico asociado al recurso.
- `manifest.json`: repositorio de origen, número de recursos incluidos y ficheros locales que no se pudieron encontrar.

La clave del recurso es determinista para la combinación de repositorio y ruta de origen. No es un identificador de OSAP.

## Datos incluidos

El documento utiliza el esquema `osap-resource-import/1.0` e incluye:

- `resource`: título, evidencia del campo de título utilizado, candidatos derivados del nombre del fichero, creadores e instrumentación cuando el análisis los haya podido extraer.
- En MusicXML/MXL se prioriza `work-title`; si falta, se utiliza el crédito visible `credit-type=title`, y después `movement-title`. Se leen también los créditos `composer`, `arranger`, `lyricist`, `poet`, `translator`, `artist` y `editor` como creadores tipificados, además de los elementos `creator` estándar. Se conservan por separado `work-title` y `movement-title`, así como derechos declarados, fuente indicada y datos de codificación. Esto permite extraer títulos y atribuciones de ficheros MuseScore que solo los incluyen en los créditos de página.
- `source`: proveedor, repositorio, descripción, rama, licencia declarada, ruta y enlaces originales.
- `file`: ruta dentro del paquete, nombre original, formato, tamaño y SHA-256.
- `analysis`: tipo, estado, metadatos estructurales, error y fecha de análisis.
- `text_extraction`: estado y texto extraído localmente, cuando existe.
- `acquisition`: identificadores y fechas del inventario local de adquisición.

Al exportar, los ficheros MusicXML, MXL, XML y MIDI/KAR descargados se analizan si todavía no tienen un análisis vigente. Para MXL/MusicXML se aprovechan los campos de título, creadores y partes que ya extrae el analizador local.

### Enriquecimiento automático con IA

Al generar el paquete JSON, si un recurso descargado carece de título o de creadores, ai-importacion intenta completar esos campos mediante Gemini Flash Lite. El análisis combina el nombre original del fichero, la descripción del repositorio, los metadatos estructurales ya extraídos y, cuando existe, un fragmento del texto extraído localmente.

La IA separa compositor, arreglista y otros roles; no debe confundir instrumentación, tonalidad, opus, software o editorial con el título o la autoría. Solo se incorporan como datos resueltos las respuestas con confianza igual o superior a 0,70. Las respuestas de menor confianza permanecen como candidatos con su procedencia. El nombre original del fichero se conserva siempre y nunca se copia directamente al campo `resource.title`.

Los resultados de IA se guardan por fichero y SHA-256 para no repetir consultas si el contenido no ha cambiado. Si el servicio no está configurado o falla, la exportación continúa y conserva los metadatos conocidos; no inventa un título ni un creador.

Configuración:
- `GEMINI_API_KEY`: una clave.
- `GEMINI_API_KEYS`: varias claves separadas por comas, punto y coma o espacios.
- También se admiten `GEMINI_API_KEY`, `GEMINI_API_KEYS` o `GOOGLE_API_KEY` en el fichero local ignorado por Git `secrets` o `secrets.txt`.
- `AI_IMPORTACION_GEMINI_MODEL`: modelo opcional; por defecto `gemini-flash-lite-latest`.

## Límites y garantías

- Un JSON describe un recurso y exactamente un fichero.
- No se crean ni se resuelven entidades Work, Person, Source, Resource o File de OSAP.
- Los metadatos son observaciones del fichero o del repositorio; no equivalen a una identificación definitiva.
- La licencia se conserva como declaración del repositorio. No se afirma que esté verificada individualmente para cada fichero.
- No se fusionan obras ni se decide si un recurso pertenece a un Work existente.
- Los ficheros y los JSON se empaquetan juntos para que el módulo receptor pueda procesar el recurso sin depender de rutas absolutas del equipo que hizo la descarga.

## Uso desde la web

En la ficha de un repositorio GitHub, después de descargar ficheros, aparece **Preparar datos de los recursos**. La acción **Generar paquete JSON** descarga el ZIP anterior. Esta función prepara los datos; todavía no los envía ni los incorpora a OSAP.


### Créditos incompletos y atribuciones conservadoras

El analizador no asigna automáticamente un rol a los créditos de página que carecen de `credit-type`. Puede utilizar uno de esos textos como título únicamente cuando sus palabras coinciden de forma suficiente con el nombre del fichero; el campo `title_source` identifica esta inferencia como `credit:untyped_filename_match`. Los demás textos se conservan en `analysis.metadata.untyped_credits` y, cuando parecen nombres, también como `resource.creator_candidates` con `type: null`. No se convierten en compositores por su posición o apariencia.

Los créditos tipificados se separan por líneas. Prefijos explícitos como `arr.`, `arranged` o `adapted` se interpretan como arreglista/adaptador, incluso si aparecen dentro de un crédito de compositor. Se eliminan duplicados del mismo rol cuando una forma abreviada está contenida en otra más completa (por ejemplo, `Mozart` y `Wolfgang Amadeus Mozart`); se conserva la forma más informativa. Los subtítulos de créditos se exponen en `analysis.metadata.subtitle`.

### Enlaces y fecha de descarga

`source.download_url` se completa con la URL `raw.githubusercontent.com` construida a partir del repositorio, rama y ruta cuando el inventario no proporcionó un enlace directo. `file.downloaded_at` registra la fecha UTC de la descarga para las nuevas descargas realizadas tras esta mejora. Para ficheros descargados anteriormente no se inventa una fecha: el valor puede seguir siendo `null` hasta que se descarguen de nuevo.
