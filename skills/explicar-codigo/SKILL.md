---
name: explicar-codigo
description: Explica código, flujos o la arquitectura de un proyecto. Úsala cuando pidan entender qué hace un archivo, cómo funciona una parte o cómo está organizado un proyecto.
triggers: [explic, "que hace", "como funciona", arquitectura, "entender el", "entender este", "analiza el proyecto", "analiza este"]
---

# Explicar código y proyectos

- Respeta el alcance pedido. Si solo solicitan explicación o análisis, trabaja en modo lectura: no edites archivos ni ejecutes acciones que cambien el proyecto.
- Para explicar un proyecto, empieza por el README y los manifiestos relevantes; identifica el punto de entrada y sigue un flujo representativo hasta los módulos principales.
- Para una pregunta puntual, empieza por los archivos o símbolos relevantes y sigue solo las dependencias necesarias. Usa búsquedas concretas y lee los rangos necesarios; amplía si falta información.
- Para una explicación general del proyecto, revisa inicialmente hasta 8 archivos clave y haz hasta 3 búsquedas concretas. Amplía si una pregunta específica sigue sin respuesta; no recorras ni vuelques todo el repositorio.
- Adapta los comandos al shell disponible. En Windows PowerShell, no uses opciones de CMD como `dir /b /s`; usa cmdlets de PowerShell o búsquedas dirigidas.
- Usa nombres y descripciones de skills disponibles para decidir si hace falta otra skill. No cargues skills irrelevantes.
- No repitas una búsqueda o llamada de herramienta fallida sin cambiar el método. Si falla de nuevo, detente y explica la limitación.
- Presenta primero el propósito general y luego el flujo o responsabilidades principales. Distingue hechos observados de inferencias.
- Sustenta las afirmaciones con archivos, símbolos o fragmentos concretos. No inventes componentes, relaciones, efectos secundarios ni errores.
- Si faltan archivos o información, explica qué no se puede confirmar en vez de seguir explorando sin un objetivo claro.
- Termina con un resumen breve y, si corresponde, una mejora sugerida claramente separada de la descripción del comportamiento actual.
