"""Servidor MCP stdio para buscar código en un proyecto configurado."""

import logging
import os
import time
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.nucleo.config import API_KEY, EMBEDDINGS_MODEL, LOG_DIR
from app.servicios.indice_codigo import IndiceCodigo

mcp = FastMCP("busqueda-codigo-local")

# Solo a archivo: stdout es el canal del protocolo MCP y no puede usarse para logs.
logger = logging.getLogger("mcp_busqueda")
logger.setLevel(logging.INFO)
logger.propagate = False
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _manejador = RotatingFileHandler(
        LOG_DIR / "mcp-busqueda.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    _manejador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_manejador)
except OSError:
    logger.addHandler(logging.NullHandler())
logger.info("MCP iniciado")


@lru_cache(maxsize=1)
def _indice() -> IndiceCodigo:
    workspace = os.getenv("MCP_WORKSPACE", "").strip()
    if not workspace:
        raise ValueError("Configura MCP_WORKSPACE con la ruta absoluta del proyecto.")
    ruta = Path(workspace)
    if not ruta.is_absolute():
        raise ValueError("MCP_WORKSPACE debe ser una ruta absoluta.")
    return IndiceCodigo(
        ruta,
        LOG_DIR / "indices",
        os.getenv("MCP_API_URL", "http://127.0.0.1:8000"),
        EMBEDDINGS_MODEL,
        API_KEY,
        (os.getenv("MCP_DOCUMENT_PREFIX", "").strip() + " ")
        if os.getenv("MCP_DOCUMENT_PREFIX", "").strip() else "",
        (os.getenv("MCP_QUERY_PREFIX", "").strip() + " ")
        if os.getenv("MCP_QUERY_PREFIX", "").strip() else "",
    )


@mcp.tool()
def indexar_proyecto(reconstruir: bool = False) -> dict:
    """Crea o actualiza incrementalmente el índice si el usuario lo solicita; si falta o está desactualizado, explica y pide autorización. reconstruir=true recalcula todos los vectores."""
    inicio = time.monotonic()
    logger.info("indexar_proyecto: inicio reconstruir=%s", reconstruir)
    try:
        resultado = _indice().indexar(reconstruir)
    except Exception as error:
        logger.error(
            "indexar_proyecto: fallo (%s) (%.1f s)",
            type(error).__name__,
            time.monotonic() - inicio,
        )
        raise
    logger.info("indexar_proyecto: listo %s (%.1f s)", resultado, time.monotonic() - inicio)
    return resultado


@mcp.tool()
def buscar_codigo(consulta: str, limite: int = 3) -> list[dict]:
    """Busca por significado y texto en el índice; devuelve hasta limite resultados (predeterminado 3, máximo 5). Confirma los fragmentos en la fuente actual."""
    inicio = time.monotonic()
    try:
        resultados = _indice().buscar(consulta, limite)
    except Exception as error:
        logger.error(
            "buscar_codigo: fallo (%s) (%.1f s)",
            type(error).__name__,
            time.monotonic() - inicio,
        )
        raise
    logger.info(
        "buscar_codigo: %d resultados (%.1f s)",
        len(resultados),
        time.monotonic() - inicio,
    )
    return resultados


@mcp.tool()
def leer_lineas(ruta: str, linea: int, margen: int = 5) -> dict:
    """Lee un archivo indexado: ruta relativa, línea desde 1 y margen 0-30 (predeterminado 5); devuelve hasta 61 líneas y 8000 caracteres."""
    inicio = time.monotonic()
    try:
        resultado = _indice().leer_lineas(ruta, linea, margen)
    except Exception as error:
        logger.error(
            "leer_lineas: fallo (%s) (%.1f s)",
            type(error).__name__,
            time.monotonic() - inicio,
        )
        raise
    logger.info("leer_lineas: listo (%.1f s)", time.monotonic() - inicio)
    return resultado


if __name__ == "__main__":
    mcp.run(transport="stdio")
