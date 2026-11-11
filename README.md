# Servidor de Agentes Locales

API local para administrar agentes de inteligencia artificial y ejecutar modelos GGUF mediante [llama.cpp](https://github.com/ggml-org/llama.cpp).

El proyecto utiliza FastAPI como capa de administración y comunicación, mientras que llama.cpp se encarga de la inferencia. Permite conectar clientes externos compatibles con la API de OpenAI, como CodeGPT, utilizando agentes configurados localmente.

## Funcionamiento

El servidor se divide en tres componentes principales:

- **FastAPI:** expone los endpoints para administrar modelos, agentes y el motor de inferencia. También proporciona una API compatible con OpenAI.
- **llama.cpp:** ejecuta los modelos GGUF y procesa las solicitudes de generación de texto.
- **Configuración de agentes:** define mediante archivos YAML el modelo que utiliza cada agente, sus instrucciones de sistema y sus parámetros de generación.

Cada agente funciona como un perfil independiente. Esto permite utilizar diferentes instrucciones y configuraciones sin tener que modificar el motor de inferencia.

El servidor administra el ciclo de vida de llama.cpp, permitiendo iniciar el modelo seleccionado y detenerlo cuando ya no se necesita. Al cerrar FastAPI normalmente, el motor también se detiene.

## Requisitos

- Windows 11 de 64 bits.
- Python 3.13 o superior compatible con las dependencias.
- llama.cpp para Windows con soporte Vulkan.
- Modelos GGUF compatibles.
- GPU compatible con Vulkan (opcional, pero recomendada para aceleración por hardware).

## Estructura del proyecto

```text
servidor-agente-local/
├── app/
│   ├── nucleo/
│   │   └── config.py
│   ├── servicios/
│   │   ├── registro_modelos.py
│   │   ├── registro_agentes.py
│   │   └── motor_inferencia.py
│   └── main.py
├── config/
│   └── agentes/
│       └── desarrollador.yaml
├── logs/
├── requirements.txt
├── .gitignore
└── LICENSE
```

## Instalación

Clonar el repositorio:

```powershell
git clone https://github.com/Game150/servidor-agente-local.git
cd servidor-agente-local
```

Crear y activar un entorno virtual:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Instalar las dependencias:

```powershell
pip install -r requirements.txt
```

### Configuración de llama.cpp

Descargar una compilación de llama.cpp para Windows con soporte Vulkan y extraerla en la raíz del proyecto, utilizando el siguiente nombre de directorio:

```text
llama-b12292-bin-win-vulkan-x64/
```

El ejecutable `llama-server.exe` debe quedar dentro de ese directorio.

### Configuración de modelos

Por defecto, el servidor busca modelos GGUF en:

```text
\\192.168.10.254\Home-Server\LLMs
```

La ubicación puede modificarse mediante la variable de entorno `MODELS_DIR`.

Es necesario que el equipo tenga acceso al directorio donde se almacenan los modelos.

## Configuración de agentes

Los agentes se definen mediante archivos YAML dentro de `config/agentes/`.

Ejemplo de configuración:

```yaml
id: desarrollador
nombre: Agente Desarrollador
descripcion: Asistente especializado en programación y desarrollo de software.

modelo: qwen2.5-coder-7b-instruct-q4_k_m

system_prompt: |
  Eres un asistente especializado en desarrollo de software.
  Responde en español, de forma clara y directa.

parametros:
  temperature: 0.3
  top_p: 0.9
  max_tokens: 2048
```

El campo `modelo` debe coincidir con el nombre de un archivo GGUF disponible en el directorio de modelos, sin su extensión.

## Ejecución

Iniciar la aplicación desde la raíz del proyecto:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

La API quedará disponible en:

```text
http://127.0.0.1:8000
```

La documentación interactiva de FastAPI se encuentra en:

```text
http://127.0.0.1:8000/docs
```

### Iniciar un agente

Por defecto, el motor de inferencia permanece detenido hasta que se solicita iniciar un agente.

Para iniciar el agente `desarrollador`, realizar una petición:

```http
POST http://127.0.0.1:8000/agents/desarrollador/start
```

El servidor localizará el modelo correspondiente y ejecutará llama.cpp.

Una vez iniciado, los clientes podrán enviar solicitudes de generación de texto.

### Conectar un cliente externo

Para utilizar CodeGPT u otro cliente compatible con OpenAI, configurar:

| Parámetro | Valor |
|---|---|
| Proveedor | OpenAI Compatible |
| Base URL | `http://127.0.0.1:8000/v1` |
| Modelo | `desarrollador` |
| API Key | No requerida actualmente |

El cliente utilizará las instrucciones y parámetros definidos en el archivo YAML del agente.

### Detener el motor

Para detener el modelo sin cerrar la aplicación:

```http
POST http://127.0.0.1:8000/engine/stop
```

También es posible consultar su estado:

```http
GET http://127.0.0.1:8000/engine/status
```

Al finalizar el uso de la aplicación, presionar `Ctrl + C` en la consola de Uvicorn. El cierre normal de FastAPI también detendrá el motor de inferencia.

## API disponible

| Método | Endpoint | Función |
|---|---|---|
| GET | `/models` | Listar modelos GGUF |
| GET | `/agents` | Listar agentes |
| GET | `/agents/{agent_id}` | Consultar un agente |
| GET | `/v1/models` | Listar agentes para clientes OpenAI |
| POST | `/agents/{agent_id}/start` | Iniciar un agente |
| GET | `/engine/status` | Consultar el estado del motor |
| POST | `/engine/stop` | Detener el motor |
| POST | `/v1/chat/completions` | Generar respuestas |

## Estado del proyecto

Proyecto en desarrollo. La implementación inicial permite registrar agentes mediante YAML, administrar modelos GGUF con llama.cpp y exponer una API compatible con OpenAI para su utilización desde aplicaciones externas.

Las funcionalidades y la documentación se ampliarán a medida que avance el desarrollo.
