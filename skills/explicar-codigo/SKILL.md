---
name: explicar-codigo
description: Explica código, flujos o la arquitectura de un proyecto. Úsala cuando pidan entender qué hace un archivo, cómo funciona una parte o cómo está organizado un proyecto.
triggers: [explic, "que hace", "como funciona", arquitectura, "entender el", "entender este", "analiza el proyecto", "analiza este"]
---

# Explicar código y proyectos

- Respeta el alcance pedido. Si solo solicitan explicación o análisis, trabaja en modo lectura: no edites archivos ni ejecutes acciones que cambien el proyecto.
- Para explicar un proyecto, empieza por el README y los manifiestos relevantes; identifica el punto de entrada y sigue un flujo representativo hasta los módulos principales.
- Limita la exploración inicial a entre 5 y 8 archivos clave y hasta 3 búsquedas concretas. Amplía solo si una pregunta específica sigue sin respuesta; no recorras ni vuelques todo el repositorio.
- Para explicar una función o archivo, lee primero esa unidad y sigue únicamente las dependencias necesarias para entender su propósito y flujo.
- Adapta los comandos al shell disponible. En Windows PowerShell, no uses opciones de CMD como `dir /b /s`; usa cmdlets de PowerShell o búsquedas dirigidas.
- Usa nombres y descripciones de skills disponibles para decidir si hace falta otra skill. No cargues skills irrelevantes.
- No repitas una búsqueda o llamada de herramienta fallida sin cambiar el método. Si falla de nuevo, detente y explica la limitación.
- Presenta primero el propósito general y luego el flujo o responsabilidades principales. Distingue hechos observados de inferencias.
- Sustenta las afirmaciones con archivos, símbolos o fragmentos concretos. No inventes componentes, relaciones, efectos secundarios ni errores.
- Si faltan archivos o información, explica qué no se puede confirmar en vez de seguir explorando sin un objetivo claro.
- Termina con un resumen breve y, si corresponde, una mejora sugerida claramente separada de la descripción del comportamiento actual.
