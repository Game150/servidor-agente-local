import threading
import time

from app.nucleo.config import MODELS_CACHE_TTL, MODELS_DIR

_candado = threading.Lock()
_cache: list[dict] | None = None
_cache_instante = 0.0


def _escanear() -> list[dict]:
    if not MODELS_DIR.is_dir():
        raise FileNotFoundError(
            f"No se puede acceder al directorio de modelos: {MODELS_DIR}"
        )

    modelos = []

    for archivo in MODELS_DIR.rglob("*.gguf"):
        if archivo.is_file():
            modelos.append({
                "nombre": archivo.stem,
                "ruta": str(archivo),
                "tamaño_bytes": archivo.stat().st_size,
            })

    return sorted(modelos, key=lambda modelo: modelo["nombre"].lower())


def listar_modelos(refrescar: bool = False) -> list[dict]:
    """Lista los GGUF disponibles.

    El resultado se recuerda MODELS_CACHE_TTL segundos: recorrer una carpeta
    de red en cada petición es lento.
    """
    global _cache, _cache_instante

    with _candado:
        vigente = (
            _cache is not None
            and time.monotonic() - _cache_instante < MODELS_CACHE_TTL
        )

        if refrescar or not vigente:
            _cache = _escanear()
            _cache_instante = time.monotonic()

        return list(_cache)


def buscar_modelo(nombre: str) -> dict | None:
    """Busca un modelo por nombre; si no está, reescanea una vez por si se
    copió un archivo nuevo."""
    for refrescar in (False, True):
        for modelo in listar_modelos(refrescar=refrescar):
            if modelo["nombre"] == nombre:
                return modelo

    return None
