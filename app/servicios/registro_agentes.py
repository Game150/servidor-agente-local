
import yaml

from app.nucleo.config import AGENTS_DIR
from app.servicios.registro_modelos import listar_modelos


def listar_agentes() -> list[dict]:
    if not AGENTS_DIR.is_dir():
        raise FileNotFoundError(
            f"No se encuentra el catálogo: {AGENTS_DIR}"
        )

    modelos_disponibles = {
        modelo["nombre"]
        for modelo in listar_modelos()
    }

    agentes = []

    for archivo in sorted(AGENTS_DIR.glob("*.yaml")):
        with archivo.open("r", encoding="utf-8") as f:
            agente = yaml.safe_load(f)

        if not isinstance(agente, dict):
            raise ValueError(
                f"Configuración inválida: {archivo.name}"
            )

        campos_obligatorios = (
            "id",
            "nombre",
            "descripcion",
            "modelo",
            "system_prompt",
            "parametros",
        )

        if not all(campo in agente for campo in campos_obligatorios):
            raise ValueError(
                f"Faltan campos obligatorios en: {archivo.name}"
            )

        modelo = agente["modelo"]

        if modelo not in modelos_disponibles:
            raise ValueError(
                f"El agente '{agente['id']}' referencia un modelo "
                f"que no existe: '{modelo}'"
            )

        agentes.append(agente)

    return agentes

def obtener_agente(identificador: str) -> dict | None:
    agentes = listar_agentes()

    for agente in agentes:
        if agente["id"] == identificador:
            return agente

    return None
