# Adquisición de repositorios GitHub

## Objetivo

Un repositorio musical no se debe tratar como una lista indiferenciada de archivos. Antes de adquirirlo, AI Importación debe comprender su documentación, su estructura y las instrucciones de uso que afectan a la selección, conversión y atribución del material.

El flujo se realiza desde la web:

1. Registrar o descubrir el repositorio.
2. Inspeccionar su README y su árbol Git.
3. Detectar manifiestos, carpetas musicales, formatos y comandos documentados.
4. Preparar una estrategia preliminar de adquisición.
5. Revisar el plan antes de descargar.
6. Adquirir los ficheros preservando rutas y procedencia.
7. Analizar y revisar el material localmente.

La inspección y el plan no ejecutan comandos del README ni scripts descargados. Los comandos se muestran como documentación para una decisión explícita.

## Estrategias comunes

La estrategia es un tipo de adaptador común, no una implementación nueva para cada repositorio:

- `github_tree`: inventario del árbol y selección por extensiones.
- `manifest`: un manifiesto declara los ficheros de entrada/salida o los elementos del corpus.
- `directory_rules`: selección por carpetas y patrones, preservando rutas.
- `release_assets`: adquisición de activos publicados en Releases.
- `git_repository`: clonación controlada cuando el árbol y los archivos Git son la fuente adecuada.
- `custom_adapter`: reglas específicas reutilizando los servicios comunes de descarga, límites, hash, registro y análisis.

La detección automática sólo propone una estrategia. El usuario debe revisar el plan antes de ejecutarlo. Un repositorio puede combinar manifiestos y reglas de directorio.

## Referencias, atribución y licencia

La procedencia no es sólo la URL del repositorio. Se conservan referencias independientes asociadas al repositorio:

- Tipo: atribución, proyecto/colección, fuente original o licencia.
- Nombre de la entidad o proyecto que debe citarse.
- URL pública que debe acompañar a la referencia.
- Texto de atribución o condición de uso.
- Archivo y fragmento de documentación donde se encontró.
- Indicador de si la referencia debe conservarse como requisito.

El extractor busca párrafos del README relacionados con citas, créditos, agradecimientos, menciones y uso público. Extrae los enlaces de esos párrafos y, cuando el texto lo permite, el nombre del proyecto mencionado. La detección automática es una ayuda, no una interpretación jurídica: debe revisarse y se pueden añadir referencias manualmente.

Ejemplo de información que se debe conservar:

- Entidad: OpenScore String Quartets.
- URL: https://musescore.com/openscore-string-quartets
- Instrucción: mencionar OpenScore String Quartets en cualquier uso público de las partituras y proporcionar un enlace.
- Origen: README del repositorio.

La atribución se debe mantener junto a los recursos adquiridos y acompañar las futuras exportaciones o presentaciones del material. La licencia declarada por GitHub y las condiciones específicas del README se conservan como datos distintos.

## Persistencia inicial

La primera implementación añade:

- `github_repository_acquisition`: README, SHA, árbol inspeccionado, estrategia y plan preliminar.
- `github_repository_references`: entidades, URLs, instrucciones y origen documental.

No altera el catálogo de archivos existente ni descarga automáticamente el corpus. La ejecución de estrategias y la vinculación de sus resultados con el catálogo se incorporarán después de validar esta primera ficha de adquisición.
