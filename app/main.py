import httpx

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from app.servicios.registro_modelos import listar_modelos
from app.servicios.registro_agentes import (
    listar_agentes,
    obtener_agente,
)
from app.servicios.motor_inferencia import (
    estado_motor,
    iniciar_motor,
    detener_motor,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        detener_motor()

app = FastAPI(
    title="Servidor Agente Local",
    description="API para administrar modelos y agentes de IA locales.",
    version="0.1.0",
    lifespan=lifespan,
)

LLAMA_API = "http://127.0.0.1:8080/v1/chat/completions"


@app.get("/")
def inicio():
    return {
        "estado": "online",
        "aplicacion": "Servidor Agente Local",
    }


@app.get("/models")
def obtener_modelos():
    try:
        modelos = listar_modelos()

        return {
            "total": len(modelos),
            "modelos": modelos,
        }

    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))


@app.get("/agents")
def obtener_agentes():
    try:
        agentes = listar_agentes()

        return {
            "total": len(agentes),
            "agentes": agentes,
        }

    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/agents/{agent_id}")
def obtener_agente_por_id(agent_id: str):
    try:
        agente = obtener_agente(agent_id)

        if agente is None:
            raise HTTPException(
                status_code=404,
                detail=f"No existe el agente: {agent_id}",
            )

        return agente

    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.get("/v1/models")
def listar_modelos_api():
    try:
        agentes = listar_agentes()

        return {
            "object": "list",
            "data": [
                {
                    "id": agente["id"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "servidor-agente-local",
                }
                for agente in agentes
            ],
        }

    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/v1/chat/completions")
async def completar_chat(solicitud: dict):
    identificador = solicitud.get("model")

    if not identificador:
        raise HTTPException(
            status_code=400,
            detail="Debes especificar el agente en el campo model.",
        )

    try:
        agente = obtener_agente(identificador)

    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error))

    if agente is None:
        raise HTTPException(
            status_code=404,
            detail=f"No existe el agente: {identificador}",
        )

    motor = estado_motor()

    if not motor["ejecutandose"]:
        raise HTTPException(
            status_code=503,
            detail="El motor de inferencia está detenido.",
        )

    if motor["modelo_activo"] != agente["modelo"]:
        raise HTTPException(
            status_code=409,
            detail="El modelo activo no corresponde al agente solicitado.",
        )

    mensajes = solicitud.get("messages")

    if not isinstance(mensajes, list) or not mensajes:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar una lista de mensajes.",
        )

    parametros = agente["parametros"]

    payload = {
        **solicitud,
        "model": "local",
        "messages": [
            {
                "role": "system",
                "content": agente["system_prompt"],
            },
            *mensajes,
        ],
    }

    payload["temperature"] = solicitud.get(
        "temperature",
        parametros.get("temperature", 0.7),
    )

    payload["top_p"] = solicitud.get(
        "top_p",
        parametros.get("top_p", 0.9),
    )

    max_tokens = solicitud.get(
        "max_tokens",
        solicitud.get("max_completion_tokens"),
    )

    if max_tokens is None:
        max_tokens = parametros.get("max_tokens", 2048)

    payload["max_tokens"] = max_tokens
    payload.pop("max_completion_tokens", None)

    try:
        cliente = httpx.AsyncClient(timeout=None)

        solicitud_motor = cliente.build_request(
            "POST",
            LLAMA_API,
            json=payload,
        )

        respuesta = await cliente.send(
            solicitud_motor,
            stream=True,
        )

    except httpx.RequestError as error:
        if "cliente" in locals():
            await cliente.aclose()

        raise HTTPException(
            status_code=503,
            detail=f"No se pudo conectar con llama.cpp: {error}",
        )

    if respuesta.status_code >= 400:
        detalle = (await respuesta.aread()).decode(
            "utf-8",
            errors="replace",
        )

        codigo = respuesta.status_code

        await respuesta.aclose()
        await cliente.aclose()

        raise HTTPException(
            status_code=codigo,
            detail=detalle,
        )

    if solicitud.get("stream", False):

        async def transmitir():
            try:
                async for fragmento in respuesta.aiter_raw():
                    yield fragmento
            finally:
                await respuesta.aclose()
                await cliente.aclose()

        return StreamingResponse(
            transmitir(),
            media_type=respuesta.headers.get(
                "content-type",
                "text/event-stream",
            ),
        )

    try:
        contenido = await respuesta.aread()
        return httpx.Response(
            status_code=respuesta.status_code,
            content=contenido,
            headers=respuesta.headers,
        ).json()

    finally:
        await respuesta.aclose()
        await cliente.aclose()


@app.get("/engine/status")
def consultar_estado_motor():
    return estado_motor()


@app.post("/agents/{agent_id}/start")
def iniciar_agente(agent_id: str):
    try:
        agente = obtener_agente(agent_id)

        if agente is None:
            raise HTTPException(
                status_code=404,
                detail=f"No existe el agente: {agent_id}",
            )

        return iniciar_motor(agente["modelo"])

    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/engine/stop")
def detener_agente():
    return detener_motor()
