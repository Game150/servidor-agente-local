# Servidor de Agentes Locales

API local para ejecutar modelos GGUF con [llama.cpp](https://github.com/ggml-org/llama.cpp) y conectarlos a clientes compatibles con la API de OpenAI. FastAPI administra los agentes y las solicitudes; no necesitas Ollama.

Esta guía está pensada para empezar en Windows. Los nombres de las rutas y algunos pasos pueden variar en otros sistemas.

## Qué necesitas

- Windows 11 de 64 bits.
- Python 3.13 o superior.
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

Descarga una compilación de llama.cpp para Windows que incluya `llama-server.exe`. Extrae la carpeta dentro de la carpeta del proyecto; su nombre debe empezar por `llama`, por ejemplo:

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

El agente incluido está configurado para un archivo llamado `qwen3.5-4b-Q4_K_M.gguf`. Si usas otro modelo, cambia el campo `modelo` en `config/agentes/desarrollador.yaml` por el nombre del archivo sin `.gguf`.

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
- `SEARXNG_URL`: dirección base de tu instancia SearXNG, sin `/search`. La búsqueda web requiere que SearXNG esté disponible y permita respuestas JSON. El valor de ejemplo debe reemplazarse si SearXNG no está en ese equipo.

El servidor y llama.cpp escuchan en `127.0.0.1` por defecto; así solo aceptan conexiones locales.

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

El servidor limita el número y tamaño de los resultados incorporados al contexto. Los trata como información externa no confiable, no como instrucciones.

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
