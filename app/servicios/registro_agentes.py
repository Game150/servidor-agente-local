import yaml

from app.nucleo.config import AGENTS_DIR

CAMPOS_TEXTO = ("id", "nombre", "descripcion", "modelo", "system_prompt")
CAMPOS_PERMITIDOS = {*CAMPOS_TEXTO, "parametros", "motor"}

# Parámetros de muestreo por defecto. Cualquier otro parámetro que acepte
# llama-server (top_k, min_p, repeat_penalty...) también puede ponerse en el
# YAML y se reenvía tal cual.
PARAMETROS_DEFECTO = {
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 2048,
    # False: el cliente puede sobrescribir los parámetros del agente.
    # True: siempre mandan los del agente.
    "forzar": False,
}

# Configuración de llama-server para cada agente.
MOTOR_DEFECTO = {
    "ctx_size": 8192,
    "gpu_layers": 99,
    "parallel": 1,
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "flash_attn": None,
    "threads": None,
    "extra_args": [],
}


def _fallo(archivo: str, mensaje: str) -> ValueError:
    return ValueError(f"{archivo}: {mensaje}")


def _es_entero(valor) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _es_numero(valor) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _validar_parametros(bruto, archivo: str) -> dict:
    if bruto is None:
        bruto = {}

    if not isinstance(bruto, dict):
        raise _fallo(archivo, "'parametros' debe ser un mapa")

    parametros = {**PARAMETROS_DEFECTO, **bruto}

    for campo in ("temperature", "top_p"):
        if not _es_numero(parametros[campo]):
            raise _fallo(archivo, f"parametros.{campo} debe ser un número")

    if not 0 <= parametros["top_p"] <= 1:
        raise _fallo(archivo, "parametros.top_p debe estar entre 0 y 1")

    if parametros["temperature"] < 0:
        raise _fallo(archivo, "parametros.temperature no puede ser negativa")

    if not _es_entero(parametros["max_tokens"]) or parametros["max_tokens"] < 1:
        raise _fallo(archivo, "parametros.max_tokens debe ser un entero >= 1")

    if not isinstance(parametros["forzar"], bool):
        raise _fallo(archivo, "parametros.forzar debe ser true o false")

    return parametros


def _validar_motor(bruto, archivo: str) -> dict:
    if bruto is None:
        bruto = {}

    if not isinstance(bruto, dict):
        raise _fallo(archivo, "'motor' debe ser un mapa")

    desconocidos = set(bruto) - set(MOTOR_DEFECTO)

    if desconocidos:
        raise _fallo(
            archivo,
            "campos desconocidos en 'motor': "
            + ", ".join(sorted(desconocidos)),
        )

    motor = {**MOTOR_DEFECTO, **bruto}

    if not _es_entero(motor["ctx_size"]) or motor["ctx_size"] < 256:
        raise _fallo(archivo, "motor.ctx_size debe ser un entero >= 256")

    if not _es_entero(motor["gpu_layers"]) or motor["gpu_layers"] < -1:
        raise _fallo(archivo, "motor.gpu_layers debe ser un entero >= -1")

    if not _es_entero(motor["parallel"]) or motor["parallel"] < 1:
        raise _fallo(archivo, "motor.parallel debe ser un entero >= 1")

    for campo in ("cache_type_k", "cache_type_v"):
        if not isinstance(motor[campo], str) or not motor[campo].strip():
            raise _fallo(archivo, f"motor.{campo} debe ser texto")

    # En YAML, `on` y `off` sin comillas se leen como booleanos.
    flash = motor["flash_attn"]

    if isinstance(flash, bool):
        flash = "on" if flash else "off"

    if flash is not None and flash not in ("on", "off", "auto"):
        raise _fallo(archivo, "motor.flash_attn debe ser on, off o auto")

    motor["flash_attn"] = flash

    if motor["threads"] is not None and (
        not _es_entero(motor["threads"]) or motor["threads"] < 1
    ):
        raise _fallo(archivo, "motor.threads debe ser un entero >= 1")

    extra = motor["extra_args"]

    if extra is None:
        extra = []

    if not isinstance(extra, list) or not all(
        isinstance(item, (str, int, float)) and not isinstance(item, bool)
        for item in extra
    ):
        raise _fallo(archivo, "motor.extra_args debe ser una lista de textos")

    motor["extra_args"] = [str(item) for item in extra]

    return motor


def _validar_agente(bruto, archivo: str) -> dict:
    if not isinstance(bruto, dict):
        raise _fallo(archivo, "configuración inválida")

    faltantes = [campo for campo in CAMPOS_TEXTO if campo not in bruto]

    if faltantes:
        raise _fallo(
            archivo,
            "faltan campos obligatorios: " + ", ".join(faltantes),
        )

    desconocidos = set(bruto) - CAMPOS_PERMITIDOS

    if desconocidos:
        raise _fallo(
            archivo,
            "campos desconocidos: " + ", ".join(sorted(desconocidos)),
        )

    for campo in CAMPOS_TEXTO:
        if not isinstance(bruto[campo], str) or not bruto[campo].strip():
            raise _fallo(archivo, f"'{campo}' debe ser un texto no vacío")

    return {
        **{campo: bruto[campo] for campo in CAMPOS_TEXTO},
        "parametros": _validar_parametros(bruto.get("parametros"), archivo),
        "motor": _validar_motor(bruto.get("motor"), archivo),
    }


def listar_agentes() -> list[dict]:
    """Lee y valida los YAML de config/agentes/.

    No consulta la carpeta de modelos: así listar agentes es rápido y sigue
    funcionando aunque el recurso de red no esté disponible. Que el modelo
    exista se comprueba al iniciar el agente.
    """
    if not AGENTS_DIR.is_dir():
        raise FileNotFoundError(
            f"No se encuentra el catálogo de agentes: {AGENTS_DIR}"
        )

    agentes = []
    vistos: dict[str, str] = {}

    for archivo in sorted(AGENTS_DIR.glob("*.y*ml")):
        if archivo.suffix not in (".yaml", ".yml"):
            continue

        try:
            with archivo.open("r", encoding="utf-8") as f:
                bruto = yaml.safe_load(f)
        except yaml.YAMLError as error:
            raise _fallo(archivo.name, f"YAML inválido ({error})") from error

        agente = _validar_agente(bruto, archivo.name)

        if agente["id"] in vistos:
            raise _fallo(
                archivo.name,
                f"el id '{agente['id']}' ya lo usa {vistos[agente['id']]}",
            )

        vistos[agente["id"]] = archivo.name
        agentes.append(agente)

    return agentes


def obtener_agente(identificador: str) -> dict | None:
    for agente in listar_agentes():
        if agente["id"] == identificador:
            return agente

    return None
