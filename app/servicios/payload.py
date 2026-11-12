ROLES_SISTEMA = ("system", "developer")


def _texto(contenido) -> str:
    """Extrae el texto de un mensaje, sea un string o una lista de partes."""
    if isinstance(contenido, str):
        return contenido

    if isinstance(contenido, list):
        return "\n".join(
            parte["text"]
            for parte in contenido
            if isinstance(parte, dict)
            and parte.get("type", "text") == "text"
            and isinstance(parte.get("text"), str)
        )

    return ""


def construir_payload(agente: dict, solicitud: dict) -> dict:
    """Combina la solicitud del cliente con la configuración del agente.

    - Un único mensaje `system`: el del agente seguido de los que mande el
      cliente (muchas plantillas de chat fallan con más de uno).
    - Parámetros de muestreo: los del agente son el valor por defecto; el
      cliente los sobrescribe salvo que el agente tenga `forzar: true`.
    - `max_completion_tokens` se traduce a `max_tokens`.
    """
    sistema_cliente = []
    resto = []

    for mensaje in solicitud["messages"]:
        if isinstance(mensaje, dict) and mensaje.get("role") in ROLES_SISTEMA:
            texto = _texto(mensaje.get("content")).strip()

            if texto:
                sistema_cliente.append(texto)
        else:
            resto.append(mensaje)

    sistema = "\n\n".join([agente["system_prompt"].strip(), *sistema_cliente])

    payload = {
        **solicitud,
        "model": "local",
        "messages": [{"role": "system", "content": sistema}, *resto],
    }

    alias = payload.pop("max_completion_tokens", None)

    if payload.get("max_tokens") is None and alias is not None:
        payload["max_tokens"] = alias

    parametros = dict(agente["parametros"])
    forzar = parametros.pop("forzar", False)

    for clave, valor in parametros.items():
        if forzar or payload.get(clave) is None:
            payload[clave] = valor

    return payload
