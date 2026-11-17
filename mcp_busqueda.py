"""Servidor MCP stdio para buscar código en un proyecto configurado."""

import os
from functools import lru_cache
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.nucleo.config import API_KEY, EMBEDDINGS_MODEL, LOG_DIR
from app.servicios.indice_codigo import IndiceCodigo

mcp = FastMCP("busqueda-codigo-local")


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
    """Actualiza el índice local del proyecto; reconstruir=true recalcula todos los vectores."""
    return _indice().indexar(reconstruir)


@mcp.tool()
def buscar_codigo(consulta: str, limite: int = 3) -> list[dict]:
    """Busca por significado y devuelve hasta cinco fragmentos del proyecto indexado."""
    return _indice().buscar(consulta, limite)


if __name__ == "__main__":
    mcp.run(transport="stdio")
