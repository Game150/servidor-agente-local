import re
import unicodedata
from pathlib import Path

import yaml

from app.nucleo.config import SKILLS_DIR

MAX_SKILLS = 50
MAX_BYTES_SKILL = 32_000
NOMBRE_SKILL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _normalizar(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn").strip()


def elegir_skill(texto: str, skills: dict[str, dict]) -> str | None:
    """Elige la skill cuyos `triggers` (prefijos de palabra) más aparecen en el texto.

    Devuelve None si no hay coincidencias o si hay empate, para que decida el modelo.
    """
    texto = _normalizar(texto)
    puntajes = {
        nombre: sum(1 for t in skill.get("triggers", []) if re.search(r"\b" + re.escape(t), texto))
        for nombre, skill in skills.items()
    }
    mejor = max(puntajes.values(), default=0)
    ganadoras = [nombre for nombre, puntaje in puntajes.items() if puntaje == mejor]
    return ganadoras[0] if mejor and len(ganadoras) == 1 else None


def skills_del_agente(skills: dict[str, dict], agente_id: str | None) -> dict[str, dict]:
    """Skills visibles para un agente: las sin `agentes` y las que lo listan."""
    if agente_id is None:
        return skills
    return {
        nombre: skill for nombre, skill in skills.items()
        if not skill.get("agentes") or agente_id in skill["agentes"]
    }


def listar_skills() -> dict[str, dict[str, str]]:
    if not SKILLS_DIR.exists():
        return {}
    if not SKILLS_DIR.is_dir():
        raise ValueError(f"El directorio de skills no es valido: {SKILLS_DIR}")

    skills = {}
    raiz = SKILLS_DIR.resolve()
    for carpeta in sorted(SKILLS_DIR.iterdir()):
        if not carpeta.is_dir() or not (carpeta / "SKILL.md").exists():
            continue
        ruta = carpeta / "SKILL.md"
        if not ruta.resolve().is_relative_to(raiz):
            raise ValueError(f"La skill {carpeta.name} esta fuera del directorio de skills.")
        if len(skills) >= MAX_SKILLS:
            raise ValueError(f"Se supera el limite de {MAX_SKILLS} skills.")
        try:
            with ruta.open("rb") as archivo:
                contenido = archivo.read(MAX_BYTES_SKILL + 1)
            if len(contenido) > MAX_BYTES_SKILL:
                raise ValueError("El archivo supera el limite de tamaño.")
            texto = contenido.decode("utf-8")
            partes = texto.splitlines()
            if not partes or partes[0].strip() != "---":
                raise ValueError("Falta el frontmatter YAML.")
            fin = next((i for i in range(1, len(partes)) if partes[i].strip() == "---"), None)
            if fin is None:
                raise ValueError("El frontmatter YAML no esta cerrado.")
            meta = yaml.safe_load("\n".join(partes[1:fin]))
            if not isinstance(meta, dict):
                raise ValueError("El frontmatter debe ser un objeto.")
            nombre = meta.get("name")
            descripcion = meta.get("description")
            instrucciones = "\n".join(partes[fin + 1:]).strip()
            if (
                not isinstance(nombre, str)
                or len(nombre) > 64
                or not NOMBRE_SKILL.fullmatch(nombre)
                or nombre != carpeta.name
            ):
                raise ValueError("El nombre es invalido o no coincide con la carpeta.")
            if not isinstance(descripcion, str) or not descripcion.strip() or len(descripcion) > 1024:
                raise ValueError("La descripcion debe tener entre 1 y 1024 caracteres.")
            if not instrucciones:
                raise ValueError("La skill no contiene instrucciones.")
            triggers = meta.get("triggers", [])
            if not isinstance(triggers, list) or any(not isinstance(t, str) or not t.strip() for t in triggers):
                raise ValueError("triggers debe ser una lista de textos.")
            agentes = meta.get("agentes", [])
            if not isinstance(agentes, list) or any(not isinstance(a, str) or not a.strip() for a in agentes):
                raise ValueError("agentes debe ser una lista de identificadores.")
            skills[nombre] = {
                "description": descripcion.strip(),
                "content": instrucciones,
                "triggers": [_normalizar(t) for t in triggers],
                "agentes": [a.strip() for a in agentes],
            }
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as error:
            raise ValueError(f"Skill invalida '{carpeta.name}': {error}") from error
    return skills
