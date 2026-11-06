# Adquisición guiada de repositorios GitHub

## Objetivo

Convertir un repositorio GitHub descubierto en una fuente musical comprendida, con un plan de adquisición revisable y ejecutable. La adquisición no debe depender de que todos los repositorios tengan la misma estructura ni de escribir un descargador exclusivo para cada uno.

El motor será común. Cada repositorio tendrá una estrategia declarativa seleccionada a partir de su documentación, árbol y manifiestos.

## Flujo funcional

1. **Descubierto**: el repositorio cumple los criterios de descubrimiento y queda registrado.
2. **Inspeccionado**: se consultan sus metadatos, README, licencia declarada y árbol Git. El árbol puede estar truncado; esa condición debe conservarse.
3. **Comprendido**: se extraen hechos verificables de la documentación y de los ficheros de control: estructura de carpetas, manifiestos, formatos, comandos documentados, dependencias y licencias.
4. **Plan preparado**: se genera una propuesta de adquisición con reglas explícitas, recuentos y exclusiones. No descarga todavía.
5. **Aprobado**: el usuario revisa el plan y decide ejecutarlo.
6. **En adquisición**: el motor ejecuta las operaciones permitidas y registra el progreso por elemento.
7. **Adquirido / parcial / error**: cada fichero y cada operación conserva su resultado. Una interrupción permite reanudar sin repetir descargas ya verificadas.

Un repositorio descubierto no equivale a un repositorio listo para descargar. La inspección y la preparación del plan son pasos separados de la ejecución.

## Estrategias reutilizables

El campo strategy_type identifica el mecanismo de adquisición. No identifica un repositorio concreto.

| Estrategia | Uso |
|---|---|
| github_tree | Seleccionar ficheros directamente desde el árbol Git, aplicando filtros de ruta y extensión. |
| manifest | Leer un manifiesto del repositorio y resolver sus rutas o pares origen/destino. |
| directory_rules | Seleccionar carpetas o patrones conocidos, conservando rutas relativas. |
| release_assets | Adquirir assets publicados en Releases, cuando sean el artefacto oficial del corpus. |
| git_repository | Clonar o descargar un snapshot del repositorio cuando la estructura y el volumen lo justifiquen. |
| custom_adapter | Adaptador específico sólo cuando los mecanismos declarativos no basten. |

Se pueden combinar estrategias dentro de un mismo plan. Por ejemplo, OpenScore/Lieder puede utilizar manifest para data/corpus_conversion.json y directory_rules para los ficheros de scores/; los TSV/YAML de data/ pueden incorporarse como metadatos de catálogo.

## Qué debe comprender el analizador

La comprensión inicial no ejecuta código del repositorio. Lee texto y metadatos de forma limitada y segura:

- README principal y documentación enlazada que pertenezca al repositorio.
- Licencia declarada por GitHub y ficheros de licencia presentes.
- Árbol de rutas, extensiones, tamaños y SHA de Git.
- Manifiestos reconocidos (JSON, CSV/TSV, YAML y listas de rutas).
- Referencias a carpetas, patrones, formatos y herramientas externas.
- Instrucciones de conversión, registradas como requisitos humanos o tareas locales aprobables, nunca ejecutadas automáticamente.

La documentación es evidencia para construir el plan, no código confiable. No se ejecutan scripts, comandos de shell, instaladores ni plugins propuestos por un README. Las conversiones que necesiten herramientas locales tendrán una operación identificada, una herramienta permitida y parámetros revisables.

## Plan de adquisición

Cada plan debe guardar como mínimo:

- repositorio, rama y commit/tree SHA que se inspeccionaron;
- fecha de análisis y versión del analizador;
- estrategia o estrategias elegidas;
- documentación y manifiestos que justifican la selección;
- reglas de inclusión y exclusión;
- elementos previstos, rutas relativas, tamaños y SHA Git cuando estén disponibles;
- transformaciones requeridas, herramienta y estado de aprobación;
- licencia declarada, ficheros de licencia encontrados y advertencias;
- estado del plan: draft, approved, running, completed, partial, error o cancelled.

El plan debe ser reproducible: si cambia el commit o el árbol del repositorio, se marca como desactualizado y se exige regenerarlo o revisarlo antes de ejecutarlo.

## Registro de ejecución

La ejecución necesita estado por elemento, no sólo un estado global:

- pending, downloading, downloaded, skipped, too_large, error;
- ruta de origen y ruta local relativa;
- bytes descargados, SHA-256 local y SHA Git esperado;
- intentos, marcas de tiempo y error;
- operación de transformación asociada, si existe.

Se mantienen los límites actuales de tamaño y rutas seguras. Las descargas se escriben primero en un fichero temporal, se verifican y después se renombran. La ejecución debe poder reanudarse y no sobrescribir silenciosamente un fichero distinto.

## Caso de referencia: OpenScore/Lieder

El README de OpenScore/Lieder describe un corpus espejo de canciones del siglo XIX y una organización scores/<composer>/<set>/<song>. Los scores usan ficheros MuseScore .mscx con identificadores lc<id>; hay letras .txt y análisis armónicos analysis_automatic.rntxt.

El directorio data/ contiene metadatos tabulares y YAML (composers, corpus, scores, sets) y el manifiesto corpus_conversion.json. El README documenta el uso de MuseScore CLI con el comando mscore -j corpus_conversion.json para convertir en lote a MXL, y señala que corpus_conversion.py regenera el manifiesto.

Plan propuesto para este repositorio:

1. Leer y conservar README y licencia CC0 como evidencia de procedencia.
2. Interpretar corpus_conversion.json como manifiesto de conversión; validar que las rutas de entrada y salida permanecen dentro del repositorio.
3. Seleccionar los .mscx de scores/ y los ficheros auxiliares asociados mediante reglas de directorio.
4. Seleccionar los metadatos TSV/YAML de data/ como datos de catálogo, sin tratarlos como partituras.
5. Proponer la conversión a MXL como operación opcional, indicando que requiere MuseScore instalado. No ejecutar el comando del README automáticamente.
6. Conservar rutas relativas, nombres originales, SHA Git y licencia/procedencia.

La primera adquisición debe poder limitarse a descargar los ficheros originales. La conversión se plantea como una etapa separada para que el corpus original siempre quede preservado.

## Interfaz web prevista

En la ficha del repositorio, añadir una sección **Comprensión y adquisición** con:

- **Documentación**: README, licencia y documentos leídos.
- **Estructura**: carpetas relevantes, manifiestos y formatos encontrados.
- **Estrategia propuesta**: mecanismo seleccionado y explicación.
- **Plan**: número de elementos, bytes estimados, inclusiones, exclusiones y transformaciones.
- **Revisión**: aprobar, rechazar o regenerar el plan.
- **Ejecución**: progreso, errores, reanudación y resultados.

La pantalla no debe obligar al usuario a escribir extensiones o rutas para cada repositorio. Esos datos se deducen del análisis y se muestran para que pueda corregirlos.

## Implementación incremental

1. Añadir persistencia de documentación, análisis y planes sin alterar el descargador actual.
2. Implementar lectura de README/licencia y análisis del árbol con resultados trazables.
3. Implementar generador de planes para github_tree, manifest y directory_rules.
4. Añadir pantalla web de revisión del plan.
5. Implementar ejecución aprobada y reanudable reutilizando el control de descarga existente.
6. Añadir release_assets, snapshot Git y adaptadores especiales sólo cuando aparezcan repositorios que los necesiten.

## Criterios de aceptación

- Añadir un repositorio no inicia descargas.
- El sistema puede explicar qué documentación y qué rutas sustentan una estrategia.
- Se genera un plan visible sin ejecutar instrucciones arbitrarias del repositorio.
- El usuario puede revisar inclusiones, exclusiones y conversiones antes de aprobar.
- La ejecución respeta límites de tamaño, conserva rutas relativas y verifica SHA-256.
- Una ejecución interrumpida puede reanudarse sin perder resultados ni duplicar trabajo.
- Un cambio de commit invalida o marca como obsoleto el plan anterior.
- Ninguna etapa escribe en OSAP o Storage; el catálogo de adquisición sigue siendo independiente.
