# Servidor de Agentes Locales

API local para ejecutar modelos GGUF con [llama.cpp](https://github.com/ggml-org/llama.cpp) y conectarlos a clientes compatibles con la API de OpenAI. FastAPI administra los agentes y las solicitudes; no necesitas Ollama.

Esta guía está pensada para empezar en Windows. Los nombres de las rutas y algunos pasos pueden variar en otros sistemas.

## Qué ofrece

- Agentes configurados mediante YAML y ejecución local de modelos GGUF, sin Ollama.
- Chat compatible con OpenAI, con respuestas normales o streaming.
- Búsqueda opcional mediante SearXNG y skills locales que el modelo puede solicitar.
- Avisos al acercarse al límite de contexto y registro del consumo de la última inferencia.

Empieza por [Inicio rápido](#inicio-rápido); después puedes consultar [Búsqueda web](#búsqueda-web), [Skills locales](#skills-locales) y [Contexto y consumo](#contexto-y-consumo).

## Qué necesitas

- Windows 11 de 64 bits.
- Python 3.13 (versión utilizada en las pruebas; otras versiones no se han verificado).
- `llama-server` de llama.cpp y un modelo compatible en formato GGUF.
- Una GPU compatible con Vulkan es opcional; acelera la generación si tu compilación y tu equipo la admiten.
- Para búsquedas web: una instancia SearXNG accesible desde el equipo.

> Los modelos GGUF pueden ocupar varios GB. Comprueba que tienes espacio en disco y memoria suficientes para el modelo elegido.

## Inicio rápido

### 1. Descarga el proyecto

Abre PowerShell y ejecuta:

```powershell
git clone https://github.com/Game150/servidor-agente-local.git
cd servidor-agente-local
```

### 2. Coloca llama.cpp y el modelo

Descarga una compilación de [llama.cpp para Windows](https://github.com/ggml-org/llama.cpp/releases) que incluya `llama-server.exe`. Extrae la carpeta dentro de la carpeta del proyecto; su nombre debe empezar por `llama`, por ejemplo:

```text
servidor-agente-local/
├── llama-b12292-bin-win-vulkan-x64/
│   └── llama-server.exe
└── ...
```

También puedes guardar `llama-server.exe` en el `PATH` o indicar su ubicación exacta en `LLAMA_SERVER_PATH`, en el archivo `.env`.

Crea la carpeta `models` y coloca dentro el archivo `.gguf` que quieras usar:

```powershell
New-Item -ItemType Directory -Force .\models
```

El proyecto no incluye el modelo: debes conseguir y descargar por separado un GGUF compatible. El agente incluido está configurado para un archivo llamado `qwen3.5-4b-Q4_K_M.gguf`. Si el archivo descargado tiene otro nombre, cambia el campo `modelo` en `config/agentes/desarrollador.yaml` por el nombre del archivo sin `.gguf`.

### 3. Prepara la configuración

Crea tu archivo `.env` local:

```powershell
Copy-Item .env.example .env
```

Abre `.env` y revisa estos valores:

```env
MODELS_DIR=models
LLAMA_SERVER_PATH=
LLAMA_HOST=127.0.0.1
LLAMA_PORT=8080
API_KEY=
SEARXNG_URL=http://127.0.0.1:8888
```

- `MODELS_DIR`: carpeta con tus archivos `.gguf`. Acepta una ruta absoluta, por ejemplo `D:\Modelos`, además de `models`.
- `LLAMA_SERVER_PATH`: opcional. Déjalo vacío si el ejecutable está dentro de una carpeta `llama*` del proyecto o en el `PATH`.
- `LLAMA_HOST` y `LLAMA_PORT`: dirección interna de llama.cpp. Déjalos como están salvo que necesites cambiarla.
- `API_KEY`: opcional para uso local. Si la defines, todos los endpoints salvo `/` requieren el encabezado `Authorization: Bearer TU_CLAVE`. Usa una clave propia y no compartas ni subas `.env`.
- `SEARXNG_URL`: dirección base de tu instancia SearXNG, sin `/search`. El valor mostrado arriba es un **ejemplo** para una instancia en el mismo equipo; debes añadirlo o descomentarlo en `.env` y ajustarlo. Si no lo defines, el código usa `http://192.168.10.254:8888`, una dirección de red local que probablemente no corresponda a tu instalación.

El servidor y llama.cpp escuchan en `127.0.0.1` por defecto; así solo aceptan conexiones locales.
Reinicia la API después de cambiar `.env`; se lee al iniciar el proceso. Las variables de entorno ya definidas en el sistema tienen prioridad sobre `.env`.

### 4. Instala las dependencias de Python

Desde la carpeta del proyecto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No hace falta activar el entorno virtual.

### 5. Arranca el servidor

Haz doble clic en `iniciar-servidor.bat`, o ejecuta desde PowerShell:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Deja abierta la ventana mientras uses la API. Para detenerla, pulsa `Ctrl+C`.

Comprueba que está activo:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
```

La documentación interactiva de FastAPI está en <http://127.0.0.1:8000/docs>.

### 6. Envía un mensaje de prueba

El identificador del agente incluido es `desarrollador`. La primera conversación inicia el agente y carga su modelo automáticamente; no es necesario iniciar el motor por separado.

```powershell
$headers = @{}
# Si configuraste API_KEY en .env, descomenta la siguiente línea y reemplaza el valor:
# $headers.Authorization = "Bearer TU_CLAVE"

$body = @{
  model = "desarrollador"
  messages = @(@{ role = "user"; content = "Responde únicamente: API lista" })
  stream = $false
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/v1/chat/completions `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

La primera respuesta puede tardar mientras se carga el modelo. Las siguientes reutilizan el proceso activo.

## Conecta una aplicación cliente

La API ofrece los endpoints `GET /v1/models` y `POST /v1/chat/completions`. No implementa todas las rutas ni todas las funciones de cada proveedor compatible con OpenAI.

Usa estos valores en clientes que acepten un proveedor OpenAI-compatible:

| Campo | Valor |
|---|---|
| URL base | `http://127.0.0.1:8000/v1` |
| Modelo | `desarrollador` |
| API key | La misma clave de `API_KEY`, si configuraste una |

Si un cliente pide la URL completa de completions en lugar de la URL base, usa `http://127.0.0.1:8000/v1/chat/completions`.

### Continue

En el archivo `config.yaml` de Continue, puedes configurar un modelo así:

```yaml
name: Servidor Agente Local
version: 1.0.0
schema: v1
models:
  - name: Agente Desarrollador local
    provider: openai
    model: desarrollador
    apiBase: http://127.0.0.1:8000/v1
    apiKey: local
    roles:
      - chat
```

Si definiste `API_KEY`, cambia `apiKey: local` por la misma clave. Si la dejaste vacía, puedes quitar la línea `apiKey`.

Empieza probando el chat normal. El uso de herramientas desde modos agente depende también de la versión y configuración de Continue.

### Extensiones de Chrome

La extensión debe permitir configurar un endpoint OpenAI-compatible y conectarse a servicios locales. Usa los valores de la tabla anterior; si solicita la URL completa, usa la ruta `/v1/chat/completions`. Concede a la extensión el permiso de acceder al servidor local si Chrome lo solicita.

Chrome por sí solo no envía mensajes a esta API: necesitas una extensión que admita endpoints personalizados. Otras extensiones pueden tener requisitos distintos; consulta su documentación.

## Búsqueda web

El agente puede decidir si necesita buscar información externa usando la herramienta `buscar_en_web`. El flujo funciona con solicitudes normales y con streaming:

1. El modelo solicita una búsqueda.
2. El servidor consulta SearXNG.
3. Los resultados se agregan a la conversación y el modelo prepara la respuesta.

Para probarlo desde tu cliente, intenta:

> Busca en la web la versión estable más reciente de Python. Usa fuentes oficiales e incluye la versión, la fecha de publicación y los enlaces. No respondas de memoria.

La búsqueda requiere que `SEARXNG_URL` apunte a una instancia activa y accesible. Además, la versión de llama.cpp y la plantilla de chat incluida en el modelo deben admitir tool calling. La compatibilidad puede variar entre modelos y clientes; si el modelo no invoca la herramienta, revisa sus registros y configuración.

Si administras SearXNG, verifica que `json` esté habilitado en `search.formats` junto a los formatos que necesites. Consulta la [configuración de búsqueda de SearXNG](https://docs.searxng.org/admin/settings/settings_search.html). Puedes comprobar tu instancia desde PowerShell, reemplazando la dirección por la tuya:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8888/search?q=python&format=json"
```

La API no instala ni inicia SearXNG. Sin una instancia accesible, el chat normal puede funcionar, pero las búsquedas que el modelo solicite fallarán.

El servidor incorpora como máximo tres resultados y limita su tamaño. Los trata como información externa no confiable, no como instrucciones. Las búsquedas y cargas de skills comparten un máximo de tres llamadas y tres iteraciones de herramientas por solicitud.

En streaming, si ocurre un error después de iniciar la respuesta, el estado HTTP ya puede ser `200`; el error se envía dentro del flujo SSE antes de `[DONE]`. Por eso, un `200 OK` en la consola no demuestra por sí solo que la búsqueda haya tenido éxito.

## Skills locales

Las skills son instrucciones reutilizables para tareas específicas; no necesitan npm. El servidor busca carpetas con un archivo `SKILL.md` dentro de `skills/`. Incluye `skills/explicar-codigo/SKILL.md` como ejemplo. Puedes cambiar la carpeta raíz mediante `SKILLS_DIR` en `.env`.

Para crear una skill, añade una carpeta cuyo nombre coincida con `name`:

```text
skills/
└── revisar-codigo/
    └── SKILL.md
```

```markdown
---
name: revisar-codigo
description: Revisa código y busca errores cuando el usuario pide una revisión.
---

# Revisar código

Comprueba el comportamiento y explica los problemas encontrados con evidencia.
```

El nombre debe usar letras minúsculas, números o guiones, sin guiones consecutivos ni al principio o final. La descripción es obligatoria. El servidor lee la configuración en cada solicitud y muestra al modelo el nombre y la descripción; puede decidir cargar las instrucciones con `cargar_skill`, tanto en streaming como sin streaming. Puedes pedirle en el chat «usa la skill revisar-codigo», aunque la decisión sigue dependiendo del modelo y su soporte para tool calling.

Para seleccionar una skill de forma explícita desde una integración propia, agrega el campo opcional `skills` al JSON de `POST /v1/chat/completions`:

```json
{
  "model": "desarrollador",
  "messages": [{"role": "user", "content": "Revisa este código"}],
  "skills": ["revisar-codigo"]
}
```

Este campo es una extensión propia de la API, no parte del formato estándar OpenAI; los clientes que no permitan campos personalizados pueden usar la selección automática. Se admiten hasta tres nombres explícitos. Si falta una skill seleccionada o un `SKILL.md` es inválido, la solicitud devuelve un error.

Solo se carga el contenido de `SKILL.md`; no se ejecutan scripts ni se leen automáticamente archivos de `scripts/`, `references/` o `assets/`. Esto es un subconjunto del formato [Agent Skills](https://agentskills.io/specification), no una implementación completa. Instala solo skills de confianza: sus instrucciones pasan al modelo.

Al cargar instrucciones de una skill, la consola del servidor muestra `Skill cargada: nombre`, indicando si la solicitó el modelo o se seleccionó explícitamente. No se registra el contenido de la skill ni la conversación. La presencia de una skill en el catálogo, por sí sola, no genera este mensaje.

### Dónde conseguir skills

- [Agent Skills](https://agentskills.io/home): explicación del formato y [especificación](https://agentskills.io/specification) para crear o revisar una skill.
- [Anthropic Skills](https://github.com/anthropics/skills/tree/main/skills): colección de ejemplos organizados en carpetas con `SKILL.md`. Están pensados para Claude y algunas skills necesitan otras funciones que este servidor no tiene.
- [OpenAI Plugins](https://github.com/openai/plugins): ejemplos de plugins de Codex, algunos con carpetas de skills. Están diseñados para Codex y pueden depender de sus herramientas o integraciones.
- [skills.sh](https://skills.sh/): directorio para descubrir skills de distintos autores. Comprueba el repositorio de origen: su presencia en el directorio no garantiza seguridad ni compatibilidad.

Son fuentes para **descubrir y revisar**, no catálogos de skills garantizadas para este servidor. Los instaladores o comandos de otros agentes no instalan automáticamente skills en este proyecto; no necesitas npm para usarlas aquí.

### Cómo instalar una skill encontrada

1. Abre su repositorio de origen y revisa `SKILL.md`, la licencia y los requisitos. Usa solo contenido de confianza: las instrucciones influyen en las respuestas del modelo.
2. Descarga el repositorio o la carpeta de la skill y copia esa carpeta a `skills/` dentro de este proyecto. Por ejemplo, `skills/mi-skill/SKILL.md`, **sin una carpeta extra entre `skills/` y `mi-skill/`**.
3. Comprueba que `name` coincida exactamente con `mi-skill`, que haya `description` y texto de instrucciones, y que el archivo esté codificado en UTF-8. El servidor admite hasta 50 skills y cada `SKILL.md` puede ocupar hasta 32.000 bytes. El nombre tiene un máximo de 64 caracteres y la descripción, de 1.024.
4. Pide una tarea relacionada desde tu cliente y observa la consola. La carga automática depende de la decisión del modelo; si quieres probarla, menciona su nombre. Un log `Skill cargada` confirma que se incorporaron sus instrucciones, no que la respuesta sea correcta.

Las skills se vuelven a leer en cada solicitud, por lo que copiar o editar una skill no requiere reiniciar la API. Sin embargo, cambiar `SKILLS_DIR` en `.env` sí requiere reiniciarla. Algunas skills externas indican que el agente lea archivos adicionales, use herramientas específicas o ejecute código: esas capacidades no las aporta este servidor, así que la skill puede requerir adaptación o no funcionar como espera su autor. Revisa también su licencia antes de copiarla o redistribuirla.

Git ignora las skills locales agregadas dentro de `skills/`; el archivo de ejemplo `skills/explicar-codigo/SKILL.md` sí forma parte del proyecto. Las reglas de `.gitignore` no dejan de rastrear archivos que ya estén versionados.

## Contexto y consumo

El contexto es el espacio disponible para la entrada y la generación del modelo. Los mensajes, el archivo adjunto que el cliente envíe, las instrucciones del agente y los resultados de herramientas pueden ocupar parte de ese espacio. Los tokens de salida pueden incluir razonamiento, no solo el texto visible.

### Avisos en la respuesta

Al finalizar una respuesta de texto, la API puede añadir un aviso cuando el consumo informado por llama.cpp alcanza el **80 %** del contexto por slot, o un aviso más urgente desde el **95 %**. Funciona con y sin streaming. Por ejemplo:

> ⚠️ Contexto cerca del límite (aproximadamente 80 %). Considera resumir la conversación o iniciar un chat nuevo.

El cálculo usa `ctx_size / parallel` como referencia y suma entrada y generación de la inferencia final. Es aproximado: la configuración efectiva puede diferir si se alteran argumentos del motor. No se añade a respuestas JSON estructuradas, a llamadas de herramientas ni cuando faltan los datos necesarios. Tampoco garantiza avisar si el motor rechaza una solicitud demasiado grande antes de generar la respuesta.

### Cómo leer el log

Al terminar la respuesta, la consola puede mostrar una línea como esta, sin incluir la conversación:

```text
INFO:     Consumo de inferencia final: entrada=700 salida=100 total=800 contexto_por_slot=16384 ocupacion=4.9% motivo=stop
```

- `entrada` incluye los tokens del prompt; `salida`, los generados por el motor.
- `total` es el consumo informado para la última inferencia. Si hubo llamadas a herramientas, no suma por separado todas las inferencias intermedias.
- `contexto_por_slot` y `ocupacion` sirven como referencia aproximada.
- `motivo=stop` indica una finalización normal; `motivo=length` señala que la generación terminó por un límite y puede explicar una respuesta cortada, aunque no demuestra que se haya llenado el contexto.
- Si llama.cpp no proporciona un dato, aparece `no disponible`. Los errores o cancelaciones pueden impedir que llegue la línea final.

Si una respuesta queda cortada, consulta el motivo y prueba pedir una respuesta más corta o por partes. También puedes aumentar el límite de salida del cliente —por ejemplo, `maxTokens` en Continue— si hay espacio suficiente; el agente usa `max_tokens: 2048` por defecto y el cliente puede sobrescribirlo.

## Configura otro agente o modelo

Los agentes son archivos YAML dentro de `config/agentes/`. El campo `id` es el nombre que envía el cliente como `model`; el campo `modelo` debe coincidir con el nombre de un archivo `.gguf` dentro de `MODELS_DIR`, sin la extensión.

Ejemplo mínimo:

```yaml
id: desarrollador
nombre: Agente Desarrollador
descripcion: Asistente de desarrollo.

modelo: qwen3.5-4b-Q4_K_M

system_prompt: |
  Responde en español, de forma clara y directa.

parametros:
  temperature: 0.3
  top_p: 0.9
  max_tokens: 2048
  forzar: false

motor:
  ctx_size: 16384
  gpu_layers: 99
  parallel: 1
```

Puedes cambiar el `id`, las instrucciones, los parámetros y el nombre del GGUF para crear otros agentes.

## Endpoints disponibles

| Método | Endpoint | Función |
|---|---|---|
| GET | `/` | Comprobar que la API está activa |
| GET | `/models` | Listar archivos GGUF disponibles |
| GET | `/agents` | Listar agentes configurados |
| GET | `/agents/{agent_id}` | Consultar la configuración de un agente |
| GET | `/v1/models` | Listar agentes para clientes OpenAI-compatible |
| POST | `/agents/{agent_id}/start` | Precargar un agente (opcional) |
| GET | `/engine/status` | Consultar el estado del motor |
| POST | `/engine/stop` | Detener el modelo activo |
| POST | `/v1/chat/completions` | Generar una respuesta; admite streaming |

Si configuraste `API_KEY`, agrega `Authorization: Bearer TU_CLAVE` a las solicitudes, excepto a `GET /`.

## Solución de problemas

- **La API no arranca:** confirma que instalaste las dependencias en `.venv` y que el puerto `8000` está libre.
- **No encuentra llama.cpp:** comprueba que `llama-server.exe` esté en una carpeta `llama*`, en el `PATH` o en la ruta indicada por `LLAMA_SERVER_PATH`.
- **No aparece el agente o modelo:** consulta `GET /agents` y `GET /models`. Revisa que el valor `modelo` del YAML coincida con el nombre del GGUF y que `MODELS_DIR` apunte a la carpeta correcta.
- **Error 401:** configura la misma clave en `.env` y en la aplicación cliente, con el prefijo `Bearer`.
- **Error de conexión desde una extensión:** verifica que Uvicorn siga activo, que la URL use el puerto `8000` y que la extensión tenga permiso para conectarse a `127.0.0.1`.
- **Falla la búsqueda web:** comprueba que SearXNG esté en ejecución, que `SEARXNG_URL` sea accesible desde este equipo y que su búsqueda responda en JSON.
- **Error al cargar una skill:** revisa la carpeta y el mensaje de error: cualquier `SKILL.md` inválido puede hacer que falle la solicitud. Comprueba los campos, el nombre, la codificación y los límites explicados arriba.
- **El modelo no usa la herramienta:** confirma que el cliente envíe solicitudes a `/v1/chat/completions` y que el modelo, la plantilla de chat y la configuración del cliente admitan tool calling.
- **El primer mensaje tarda:** normalmente el modelo se está cargando. Consulta `GET /engine/status` y el archivo `logs/llama-server.log`.

## Rendimiento

Los parámetros del bloque `motor` permiten ajustar el uso de recursos:

- `ctx_size`: tamaño del contexto. Aumentarlo consume más memoria.
- `gpu_layers`: cantidad de capas que llama.cpp intenta ejecutar en la GPU. `99` intenta descargar tantas como sea posible.
- `parallel`: cantidad de solicitudes que el motor puede atender en paralelo; más paralelismo puede consumir más memoria y reducir el contexto disponible por solicitud.

Cambia un valor por vez y compara el resultado. No hay una configuración óptima universal: depende del modelo, su cuantización y el hardware.

## Seguridad y alcance

Por defecto, la API solo escucha en `127.0.0.1`, es decir, en el propio equipo. No la expongas directamente a Internet. Si configuras `API_KEY`, usa una clave privada y evita compartir el archivo `.env`.

El proyecto está en desarrollo. Su compatibilidad OpenAI se limita a los endpoints descritos en esta guía.
