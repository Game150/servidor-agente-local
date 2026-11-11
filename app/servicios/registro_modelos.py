from app.nucleo.config import MODELS_DIR


def listar_modelos() -> list[dict]:
    if not MODELS_DIR.is_dir():
        raise FileNotFoundError(
            f"No se puede acceder al directorio: {MODELS_DIR}"
        )

    modelos = []

    for archivo in MODELS_DIR.rglob("*.gguf"):
        if archivo.is_file():
            modelos.append({
                "nombre": archivo.stem,
                "ruta": str(archivo),
                "tamaño_bytes": archivo.stat().st_size,
            })

    return sorted(
        modelos,
        key=lambda modelo: modelo["nombre"].lower()
    )

