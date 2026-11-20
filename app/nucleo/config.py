"""Configuración central del servidor.

Todo lo que depende del entorno (rutas, puertos, claves) se lee de variables
de entorno o de un archivo `.env` en la raíz del proyecto. Las variables ya
definidas en el sistema tienen prioridad sobre las del `.env`.
"""

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def _cargar_env(ruta: Path) -> None:
    """Carga un .env sencillo (CLAVE=valor) sin dependencias externas."""
    if not ruta.is_file():
        return

    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()

        if not linea or linea.startswith("#") or "=" not in linea:
            continue

        clave, valor = linea.split("=", 1)
        valor = valor.strip().strip('"').strip("'")
        os.environ.setdefault(clave.strip(), valor)


_cargar_env(BASE_DIR / ".env")


def _entero(nombre: str, defecto: int) -> int:
    valor = os.getenv(nombre, "").strip()

    if not valor:
        return defecto

    try:
        return int(valor)
    except ValueError:
        raise ValueError(
            f"La variable {nombre} debe ser un número entero: {valor!r}"
        ) from None


def _booleano(nombre: str, defecto: bool) -> bool:
    valor = os.getenv(nombre, "").strip().lower()
    if not valor:
        return defecto
    if valor in ("true", "1"):
        return True
    if valor in ("false", "0"):
        return False
    raise ValueError(f"La variable {nombre} debe ser true, false, 1 o 0.")


def _ruta(nombre: str, defecto: Path) -> Path:
    valor = os.getenv(nombre, "").strip()
    ruta = Path(valor).expanduser() if valor else defecto

    if not ruta.is_absolute():
        ruta = BASE_DIR / ruta

    return ruta


# --- Rutas -----------------------------------------------------------------

MODELS_DIR = _ruta("MODELS_DIR", BASE_DIR / "models")
AGENTS_DIR = _ruta("AGENTS_DIR", BASE_DIR / "config" / "agentes")
SKILLS_DIR = _ruta("SKILLS_DIR", BASE_DIR / "skills")
LOG_DIR = _ruta("LOG_DIR", BASE_DIR / "logs")
LOG_MAX_BYTES = _entero("LOG_MAX_BYTES", 5 * 1024 * 1024)

# --- Motor de inferencia (llama.cpp) --------------------------------------

LLAMA_HOST = os.getenv("LLAMA_HOST", "127.0.0.1").strip() or "127.0.0.1"
LLAMA_PORT = _entero("LLAMA_PORT", 8080)
LLAMA_URL = f"http://{LLAMA_HOST}:{LLAMA_PORT}"
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "").strip()
EMBEDDINGS_HOST = (
    os.getenv("EMBEDDINGS_HOST", LLAMA_HOST).strip() or LLAMA_HOST
)
EMBEDDINGS_PORT = _entero("EMBEDDINGS_PORT", 8081)
EMBEDDINGS_CTX_SIZE = _entero("EMBEDDINGS_CTX_SIZE", 2048)
EMBEDDINGS_GPU_LAYERS = _entero("EMBEDDINGS_GPU_LAYERS", 0)
if not 1 <= EMBEDDINGS_PORT <= 65535:
    raise ValueError("EMBEDDINGS_PORT debe estar entre 1 y 65535.")
if EMBEDDINGS_CTX_SIZE <= 0:
    raise ValueError("EMBEDDINGS_CTX_SIZE debe ser mayor que 0.")
if EMBEDDINGS_GPU_LAYERS < 0:
    raise ValueError("EMBEDDINGS_GPU_LAYERS debe ser mayor o igual a 0.")

# Búsqueda semántica interna, sobre un único proyecto configurado explícitamente.
CODE_SEARCH_ENABLED = _booleano("CODE_SEARCH_ENABLED", False)
_code_search_workspace = os.getenv("CODE_SEARCH_WORKSPACE", "").strip()
CODE_SEARCH_WORKSPACE = Path(_code_search_workspace).expanduser() if _code_search_workspace else None
CODE_SEARCH_DOCUMENT_PREFIX = os.getenv("CODE_SEARCH_DOCUMENT_PREFIX", "").strip()
CODE_SEARCH_QUERY_PREFIX = os.getenv("CODE_SEARCH_QUERY_PREFIX", "").strip()
if CODE_SEARCH_ENABLED:
    if CODE_SEARCH_WORKSPACE is None or not CODE_SEARCH_WORKSPACE.is_absolute():
        raise ValueError("CODE_SEARCH_WORKSPACE debe ser una ruta absoluta cuando CODE_SEARCH_ENABLED esta activo.")
    if not EMBEDDINGS_MODEL:
        raise ValueError("Configura EMBEDDINGS_MODEL para activar CODE_SEARCH_ENABLED.")
INFERENCE_TIMEOUT_MINUTES = _entero("INFERENCE_TIMEOUT_MINUTES", 0)
if INFERENCE_TIMEOUT_MINUTES < 0:
    raise ValueError("INFERENCE_TIMEOUT_MINUTES debe ser un entero mayor o igual a 0.")

# --- Busqueda web ---------------------------------------------------------

WEB_SEARCH_ENABLED = _booleano("WEB_SEARCH_ENABLED", True)
SEARXNG_URL = (
    os.getenv("SEARXNG_URL", "http://192.168.10.254:8888").strip().rstrip("/")
    or "http://192.168.10.254:8888"
)

# Segundos que se espera a que llama-server termine de cargar el modelo.
STARTUP_TIMEOUT = _entero("STARTUP_TIMEOUT", 180)

# Segundos que se recuerda el listado de modelos (evita recorrer la red
# en cada petición).
MODELS_CACHE_TTL = _entero("MODELS_CACHE_TTL", 30)

# --- Seguridad --------------------------------------------------------------

# Si se define, todas las rutas (salvo "/") exigen `Authorization: Bearer <clave>`.
API_KEY = os.getenv("API_KEY", "").strip()


def nombre_binario() -> str:
    return "llama-server.exe" if os.name == "nt" else "llama-server"


def resolver_llama_server() -> Path | None:
    """Localiza el ejecutable de llama-server.

    Orden de búsqueda:
      1. Variable LLAMA_SERVER_PATH (archivo o carpeta que lo contiene).
      2. Carpetas `llama*` en la raíz del proyecto, filtradas por LLAMA_BACKEND.
      3. El PATH del sistema, solo cuando LLAMA_BACKEND es `auto`.
    """
    nombre = nombre_binario()
    backend = os.getenv("LLAMA_BACKEND", "auto").strip().lower() or "auto"

    if backend not in ("auto", "vulkan", "rocm"):
        raise ValueError("LLAMA_BACKEND debe ser auto, vulkan o rocm.")

    explicito = os.getenv("LLAMA_SERVER_PATH", "").strip()

    if explicito:
        ruta = Path(explicito).expanduser()

        if not ruta.is_absolute():
            ruta = BASE_DIR / ruta

        if ruta.is_dir():
            ruta = ruta / nombre

        return ruta if ruta.is_file() else None

    candidatos = [
        ruta
        for ruta in BASE_DIR.glob(f"llama*/**/{nombre}")
        if ruta.is_file()
    ]

    if backend != "auto":
        candidatos = [
            ruta
            for ruta in candidatos
            if any(
                backend in parte.lower()
                for parte in ruta.relative_to(BASE_DIR).parts
            )
        ]

        if not candidatos:
            raise FileNotFoundError(
                f"LLAMA_BACKEND={backend}, pero no se encontró llama-server "
                f"en una carpeta 'llama*' cuyo nombre contenga '{backend}'. "
                "Define LLAMA_SERVER_PATH para indicar otra ubicación."
            )

    if candidatos:
        return max(candidatos, key=lambda ruta: ruta.stat().st_mtime)

    en_path = shutil.which(nombre)

    return Path(en_path) if en_path else None
