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
- En MusicXML/MXL se prioriza `work-title` para identificar la composición. `movement-title` se conserva por separado y solo se usa como título principal cuando falta `work-title`. También se conservan los creadores por tipo (composer, arranger, etc.), derechos declarados, fuente indicada y datos de codificación.
- `source`: proveedor, repositorio, descripción, rama, licencia declarada, ruta y enlaces originales.
- `file`: ruta dentro del paquete, nombre original, formato, tamaño y SHA-256.
- `analysis`: tipo, estado, metadatos estructurales, error y fecha de análisis.
- `text_extraction`: estado y texto extraído localmente, cuando existe.
- `acquisition`: identificadores y fechas del inventario local de adquisición.

Al exportar, los ficheros MusicXML, MXL, XML y MIDI/KAR descargados se analizan si todavía no tienen un análisis vigente. Para MXL/MusicXML se aprovechan los campos de título, creadores y partes que ya extrae el analizador local.

## Límites y garantías

- Un JSON describe un recurso y exactamente un fichero.
- No se crean ni se resuelven entidades Work, Person, Source, Resource o File de OSAP.
- Los metadatos son observaciones del fichero o del repositorio; no equivalen a una identificación definitiva.
- La licencia se conserva como declaración del repositorio. No se afirma que esté verificada individualmente para cada fichero.
- No se fusionan obras ni se decide si un recurso pertenece a un Work existente.
- Los ficheros y los JSON se empaquetan juntos para que el módulo receptor pueda procesar el recurso sin depender de rutas absolutas del equipo que hizo la descarga.

## Uso desde la web

En la ficha de un repositorio GitHub, después de descargar ficheros, aparece **Preparar datos de los recursos**. La acción **Generar paquete JSON** descarga el ZIP anterior. Esta función prepara los datos; todavía no los envía ni los incorpora a OSAP.
