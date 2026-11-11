
import subprocess
from pathlib import Path

from app.nucleo.config import BASE_DIR
from app.servicios.registro_modelos import listar_modelos


LLAMA_SERVER = (
    BASE_DIR
    / "llama-b12292-bin-win-vulkan-x64"
    / "llama-server.exe"
)

LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8080
LOG_DIR = BASE_DIR / "logs"

proceso = None
modelo_activo = None


def estado_motor() -> dict:
    global proceso, modelo_activo

    if proceso is not None and proceso.poll() is not None:
        proceso = None
        modelo_activo = None

    return {
        "ejecutandose": proceso is not None,
        "modelo_activo": modelo_activo,
        "puerto": LLAMA_PORT,
    }


def iniciar_motor(nombre_modelo: str) -> dict:
    global proceso, modelo_activo

    estado = estado_motor()

    if estado["ejecutandose"]:
        if modelo_activo == nombre_modelo:
            return {
                "mensaje": "El modelo ya está ejecutándose.",
                **estado,
            }

        raise RuntimeError(
            "Ya hay otro modelo activo. Detenelo antes de iniciar otro."
        )

    if not LLAMA_SERVER.is_file():
        raise FileNotFoundError(
            f"No se encuentra llama-server.exe: {LLAMA_SERVER}"
        )

    modelos = listar_modelos()

    modelo = next(
        (
            Path(item["ruta"])
            for item in modelos
            if item["nombre"] == nombre_modelo
        ),
        None,
    )

    if modelo is None:
        raise FileNotFoundError(
            f"No se encuentra el modelo en el catálogo: {nombre_modelo}"
        )

    argumentos = [
        str(LLAMA_SERVER),
        "-m", str(modelo),
        "-ngl", "99",
        "-c", "8192",
        "-np", "1",
        "-ctk", "q8_0",
        "-ctv", "q8_0",
        "--host", LLAMA_HOST,
        "--port", str(LLAMA_PORT),
    ]

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    ruta_log = LOG_DIR / "llama-server.log"

    with ruta_log.open("a", encoding="utf-8") as registro:
        proceso = subprocess.Popen(
            argumentos,
            cwd=str(LLAMA_SERVER.parent),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=registro,
            stderr=subprocess.STDOUT,
        )

    modelo_activo = nombre_modelo

    return {
        "mensaje": "Motor de inferencia iniciado.",
        **estado_motor(),
    }


def detener_motor() -> dict:
    global proceso, modelo_activo

    if proceso is None:
        return {
            "mensaje": "El motor ya está detenido.",
            **estado_motor(),
        }

    proceso.terminate()

    try:
        proceso.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proceso.kill()
        proceso.wait()

    proceso = None
    modelo_activo = None

    return {
        "mensaje": "Motor de inferencia detenido.",
        **estado_motor(),
    }

