import asyncio
import json
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.nucleo.config import API_KEY, LLAMA_URL
from app.servicios.coordinador import Coordinador
from app.servicios.motor_inferencia import motor
from app.servicios.payload import construir_payload
from app.servicios.registro_agentes import listar_agentes, obtener_agente
from app.servicios.registro_modelos import listar_modelos

coordinador = Coordinador(motor)


# --- Ciclo de vida -----------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Un único cliente HTTP compartido. trust_env=False evita que un proxy del
    # sistema intercepte las llamadas a localhost.
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=5.0),
        trust_env=False,
    )

    try:
        yield
    finally:
        await app.state.http.aclose()
        await asyncio.to_thread(motor.detener)


# --- Seguridad -----------------------------------------------------------------

async def verificar_clave(authorization: str | None = Header(default=None)):
    """Si API_KEY está definida, exige `Authorization: Bearer <clave>`."""
    if not API_KEY:
        return

    esperado = f"Bearer {API_KEY}".encode()
    recibido = (authorization or "").encode()

    if not secrets.compare_digest(recibido, esperado):
        raise HTTPException(
            status_code=401,
            detail="API key inválida o ausente.",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = FastAPI(
    title="Servidor Agente Local",
    description="API para administrar modelos y agentes de IA locales.",
    version="0.2.0",
    lifespan=lifespan,
)

# Todas las rutas salvo "/" van en este router y requieren clave si hay API_KEY.
protegido = APIRouter(dependencies=[Depends(verificar_clave)])


# --- Errores en formato OpenAI -------------------------------------------------

def _tipo_error(codigo: int) -> str:
    if codigo == 401:
        return "authentication_error"
    if codigo == 404:
        return "not_found_error"
    if codigo < 500:
        return "invalid_request_error"
    return "server_error"


def _respuesta_error(codigo: int, mensaje: str, headers: dict | None = None):
    return JSONResponse(
        status_code=codigo,
        headers=headers,
        content={
            "error": {
                "message": mensaje,
                "type": _tipo_error(codigo),
                "code": codigo,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def manejar_http(request: Request, exc: StarletteHTTPException):
    # Los clientes OpenAI esperan {"error": {...}} en las rutas /v1.
    if request.url.path.startswith("/v1"):
        return _respuesta_error(exc.status_code, str(exc.detail), exc.headers)

    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def manejar_validacion(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1"):
        return _respuesta_error(400, "El cuerpo de la petición no es un JSON válido.")

    return await request_validation_exception_handler(request, exc)


# --- Utilidades ------------------------------------------------------------------

def _agentes_o_error() -> list[dict]:
    try:
        return listar_agentes()
    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error))


def _agente_o_error(identificador: str) -> dict:
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

    return agente


async def _preparar_motor(agente: dict, reservar: bool = False) -> None:
    try:
        await coordinador.preparar(agente, reservar=reservar)
    except (FileNotFoundError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error))


# --- Rutas informativas -----------------------------------------------------------

@app.get("/")
def inicio():
    return {
        "estado": "online",
        "aplicacion": "Servidor Agente Local",
    }


@protegido.get("/models")
def obtener_modelos():
    try:
        modelos = listar_modelos()
    except FileNotFoundError as error:
        raise HTTPException(status_code=503, detail=str(error))

    return {"total": len(modelos), "modelos": modelos}


@protegido.get("/agents")
def obtener_agentes():
    agentes = _agentes_o_error()

    return {"total": len(agentes), "agentes": agentes}


@protegido.get("/agents/{agent_id}")
def obtener_agente_por_id(agent_id: str):
    return _agente_o_error(agent_id)


@protegido.get("/v1/models")
def listar_modelos_api():
    return {
        "object": "list",
        "data": [
            {
                "id": agente["id"],
                "object": "model",
                "created": 0,
                "owned_by": "servidor-agente-local",
            }
            for agente in _agentes_o_error()
        ],
    }


# --- Chat (compatible con OpenAI) -------------------------------------------------

def _error_motor(codigo: int, cuerpo: bytes) -> JSONResponse:
    """Reenvía un error de llama-server; si ya viene en formato OpenAI se
    deja tal cual."""
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        datos = None

    if isinstance(datos, dict) and "error" in datos:
        return JSONResponse(status_code=codigo, content=datos)

    return _respuesta_error(codigo, cuerpo.decode("utf-8", errors="replace"))


@protegido.post("/v1/chat/completions")
async def completar_chat(request: Request, solicitud: dict):
    identificador = solicitud.get("model")

    if not identificador:
        raise HTTPException(
            status_code=400,
            detail="Debes especificar el agente en el campo model.",
        )

    agente = _agente_o_error(identificador)

    mensajes = solicitud.get("messages")

    if not isinstance(mensajes, list) or not mensajes:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar una lista de mensajes.",
        )

    payload = construir_payload(agente, solicitud)

    # Arranca o cambia de modelo si hace falta y reserva un lugar. Desde acá
    # hay que llamar siempre a coordinador.liberar().
    await _preparar_motor(agente, reservar=True)

    cliente: httpx.AsyncClient = request.app.state.http

    try:
        peticion = cliente.build_request(
            "POST",
            f"{LLAMA_URL}/v1/chat/completions",
            json=payload,
        )
        respuesta = await cliente.send(peticion, stream=True)
    except httpx.RequestError as error:
        await coordinador.liberar()
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo conectar con llama.cpp: {error}",
        )
    except BaseException:
        await coordinador.liberar()
        raise

    if respuesta.status_code >= 400:
        try:
            cuerpo = await respuesta.aread()
        finally:
            await respuesta.aclose()
            await coordinador.liberar()

        return _error_motor(respuesta.status_code, cuerpo)

    if solicitud.get("stream", False):

        async def transmitir():
            try:
                async for fragmento in respuesta.aiter_raw():
                    yield fragmento
            finally:
                await respuesta.aclose()
                await coordinador.liberar()

        return StreamingResponse(
            transmitir(),
            media_type=respuesta.headers.get("content-type", "text/event-stream"),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        cuerpo = await respuesta.aread()
    finally:
        await respuesta.aclose()
        await coordinador.liberar()

    return Response(
        content=cuerpo,
        status_code=respuesta.status_code,
        media_type="application/json; charset=utf-8",
    )


# --- Control del motor ----------------------------------------------------------------

@protegido.get("/engine/status")
def consultar_estado_motor():
    return motor.estado()


@protegido.post("/agents/{agent_id}/start")
async def iniciar_agente(agent_id: str):
    """Precarga el modelo de un agente (opcional: el primer chat también lo hace)."""
    agente = _agente_o_error(agent_id)

    await _preparar_motor(agente)

    return {"mensaje": f"Agente '{agent_id}' listo.", **motor.estado()}


@protegido.post("/engine/stop")
def detener_agente():
    return motor.detener()


app.include_router(protegido)
