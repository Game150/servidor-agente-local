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
from app.servicios.busqueda import buscar_en_searxng
from app.servicios.coordinador import Coordinador
from app.servicios.motor_inferencia import motor
from app.servicios.payload import construir_payload
from app.servicios.registro_agentes import listar_agentes, obtener_agente
from app.servicios.registro_modelos import listar_modelos

coordinador = Coordinador(motor)

MAX_ITERACIONES_HERRAMIENTAS = 3
MAX_LLAMADAS_HERRAMIENTAS = 3
MAX_CARACTERES_ARGUMENTOS = 2_000

HERRAMIENTA_BUSQUEDA_WEB = {
    "type": "function",
    "function": {
        "name": "buscar_en_web",
        "description": (
            "Busca informacion actualizada o externa mediante SearXNG. "
            "Usala cuando la respuesta dependa de datos actuales o de fuentes web."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta breve para buscar en la web.",
                    "maxLength": 500,
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


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


def _agregar_herramienta_busqueda(payload: dict) -> None:
    herramientas = payload.get("tools", [])
    if herramientas is None:
        herramientas = []

    if not isinstance(herramientas, list):
        raise HTTPException(status_code=400, detail="El campo tools debe ser una lista.")

    for herramienta in herramientas:
        if not isinstance(herramienta, dict):
            raise HTTPException(status_code=400, detail="Cada elemento de tools debe ser un objeto.")

        funcion = herramienta.get("function")

        if isinstance(funcion, dict) and funcion.get("name") == "buscar_en_web":
            raise HTTPException(
                status_code=400,
                detail="El nombre de herramienta buscar_en_web esta reservado.",
            )

    payload["tools"] = [*herramientas, HERRAMIENTA_BUSQUEDA_WEB]


def _validar_llamadas_busqueda(tool_calls: list) -> list[tuple[str, str]]:
    if not tool_calls or len(tool_calls) > MAX_LLAMADAS_HERRAMIENTAS:
        raise HTTPException(
            status_code=502,
            detail="El modelo solicito una cantidad invalida de busquedas web.",
        )

    llamadas = []
    identificadores = set()

    for llamada in tool_calls:
        if not isinstance(llamada, dict) or llamada.get("type") != "function":
            raise HTTPException(status_code=502, detail="El modelo devolvio una herramienta invalida.")

        identificador = llamada.get("id")
        funcion = llamada.get("function")

        if (
            not isinstance(identificador, str)
            or not identificador
            or len(identificador) > 128
            or identificador in identificadores
            or not isinstance(funcion, dict)
            or funcion.get("name") != "buscar_en_web"
        ):
            raise HTTPException(status_code=502, detail="El modelo devolvio una llamada de herramienta invalida.")

        argumentos = funcion.get("arguments")

        if not isinstance(argumentos, str) or len(argumentos) > MAX_CARACTERES_ARGUMENTOS:
            raise HTTPException(status_code=502, detail="Los argumentos de buscar_en_web son invalidos.")

        try:
            datos = json.loads(argumentos)
        except json.JSONDecodeError as error:
            raise HTTPException(
                status_code=502,
                detail="Los argumentos de buscar_en_web no contienen JSON valido.",
            ) from error

        if (
            not isinstance(datos, dict)
            or set(datos) != {"query"}
            or not isinstance(datos["query"], str)
            or not datos["query"].strip()
            or len(datos["query"].strip()) > 500
        ):
            raise HTTPException(status_code=502, detail="La consulta de buscar_en_web es invalida.")

        identificadores.add(identificador)
        llamadas.append((identificador, datos["query"].strip()))

    return llamadas


async def _completar_sin_stream(
    cliente: httpx.AsyncClient,
    payload: dict,
) -> Response:
    total_llamadas = 0

    for iteracion in range(MAX_ITERACIONES_HERRAMIENTAS + 1):
        respuesta = None

        try:
            peticion = cliente.build_request(
                "POST",
                f"{LLAMA_URL}/v1/chat/completions",
                json=payload,
            )
            respuesta = await cliente.send(peticion, stream=True)
            cuerpo = await respuesta.aread()
            codigo = respuesta.status_code
        except httpx.RequestError as error:
            raise HTTPException(
                status_code=503,
                detail=f"No se pudo conectar con llama.cpp: {error}",
            ) from error
        finally:
            if respuesta is not None:
                await respuesta.aclose()

        if codigo >= 400:
            return _error_motor(codigo, cuerpo)

        try:
            datos = json.loads(cuerpo)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(
                content=cuerpo,
                status_code=codigo,
                media_type="application/json; charset=utf-8",
            )

        elecciones = datos.get("choices") if isinstance(datos, dict) else None
        if not isinstance(elecciones, list) or not elecciones or not isinstance(elecciones[0], dict):
            return Response(
                content=cuerpo,
                status_code=codigo,
                media_type="application/json; charset=utf-8",
            )

        mensaje = elecciones[0].get("message")
        if not isinstance(mensaje, dict):
            return Response(
                content=cuerpo,
                status_code=codigo,
                media_type="application/json; charset=utf-8",
            )

        tool_calls = mensaje.get("tool_calls")
        if not tool_calls:
            return Response(
                content=cuerpo,
                status_code=codigo,
                media_type="application/json; charset=utf-8",
            )

        if not isinstance(tool_calls, list):
            raise HTTPException(status_code=502, detail="El modelo devolvio tool_calls invalidos.")

        if any(
            not isinstance(llamada, dict)
            or llamada.get("type") != "function"
            or not isinstance(llamada.get("function"), dict)
            or llamada["function"].get("name") != "buscar_en_web"
            for llamada in tool_calls
        ):
            return Response(
                content=cuerpo,
                status_code=codigo,
                media_type="application/json; charset=utf-8",
            )

        if iteracion == MAX_ITERACIONES_HERRAMIENTAS:
            raise HTTPException(
                status_code=502,
                detail="El modelo supero el limite de iteraciones de herramientas.",
            )

        llamadas = _validar_llamadas_busqueda(tool_calls)
        if total_llamadas + len(llamadas) > MAX_LLAMADAS_HERRAMIENTAS:
            raise HTTPException(
                status_code=502,
                detail="El modelo supero el limite de busquedas por solicitud.",
            )

        mensajes_herramienta = []
        for identificador, query in llamadas:
            try:
                resultado = await buscar_en_searxng(cliente, query)
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(
                    status_code=502,
                    detail="No se pudo completar la busqueda web en SearXNG.",
                ) from error

            mensajes_herramienta.append(
                {
                    "role": "tool",
                    "tool_call_id": identificador,
                    "content": resultado,
                }
            )

        payload["messages"].append(mensaje)
        payload["messages"].extend(mensajes_herramienta)
        total_llamadas += len(llamadas)

    raise HTTPException(status_code=502, detail="El modelo no completo la solicitud.")


def _codificar_evento_sse(datos: dict | str) -> bytes:
    if isinstance(datos, str):
        contenido = datos
    else:
        contenido = json.dumps(datos, ensure_ascii=False, separators=(",", ":"))

    return f"data: {contenido}\n\n".encode("utf-8")


def _acumular_llamadas_stream(datos: dict, estado: dict) -> bytes | None:
    """Acumula deltas de tool calls y devuelve el evento sin esos deltas."""
    elecciones = datos.get("choices")

    if not isinstance(elecciones, list):
        return None

    evento_modificado = False
    elecciones_limpias = []

    for eleccion in elecciones:
        if not isinstance(eleccion, dict):
            elecciones_limpias.append(eleccion)
            continue

        limpia = dict(eleccion)
        delta = eleccion.get("delta")

        if isinstance(delta, dict):
            delta_limpio = dict(delta)
            llamadas = delta_limpio.pop("tool_calls", None)
            finish_reason = limpia.get("finish_reason")

            if isinstance(llamadas, list) and llamadas:
                estado["vio_tool_calls"] = True
                evento_modificado = True

                for llamada in llamadas:
                    if not isinstance(llamada, dict):
                        estado["llamada_invalida"] = True
                        continue

                    indice = llamada.get("index")
                    if not isinstance(indice, int) or isinstance(indice, bool) or indice < 0:
                        estado["llamada_invalida"] = True
                        continue

                    acumulada = estado["llamadas"].setdefault(
                        indice,
                        {
                            "id": "",
                            "type": None,
                            "function": {"name": "", "arguments": ""},
                        },
                    )

                    identificador = llamada.get("id")
                    tipo = llamada.get("type")
                    funcion = llamada.get("function")

                    if isinstance(identificador, str):
                        acumulada["id"] += identificador
                    elif identificador is not None:
                        estado["llamada_invalida"] = True

                    if isinstance(tipo, str):
                        acumulada["type"] = tipo
                    elif tipo is not None:
                        estado["llamada_invalida"] = True

                    if funcion is not None and not isinstance(funcion, dict):
                        estado["llamada_invalida"] = True
                    elif isinstance(funcion, dict):
                        nombre = funcion.get("name")
                        argumentos = funcion.get("arguments")

                        if isinstance(nombre, str):
                            acumulada["function"]["name"] += nombre
                        elif nombre is not None:
                            estado["llamada_invalida"] = True

                        if isinstance(argumentos, str):
                            acumulada["function"]["arguments"] += argumentos
                        elif argumentos is not None:
                            estado["llamada_invalida"] = True

                if finish_reason == "tool_calls":
                    limpia["finish_reason"] = None

            if delta_limpio:
                limpia["delta"] = delta_limpio
            else:
                limpia.pop("delta", None)

        elecciones_limpias.append(limpia)

    if not evento_modificado:
        return None

    resultado = dict(datos)
    resultado["choices"] = elecciones_limpias

    if not any(
        eleccion.get("delta") or eleccion.get("finish_reason")
        for eleccion in elecciones_limpias
        if isinstance(eleccion, dict)
    ):
        return b""

    return _codificar_evento_sse(resultado)


def _evento_llamadas_sse(datos: dict) -> bytes | None:
    elecciones = datos.get("choices")

    if not isinstance(elecciones, list):
        return None

    elecciones_llamadas = []

    for eleccion in elecciones:
        if not isinstance(eleccion, dict):
            continue

        delta = eleccion.get("delta")
        llamadas = delta.get("tool_calls") if isinstance(delta, dict) else None
        if not isinstance(llamadas, list) or not llamadas:
            continue

        elecciones_llamadas.append(
            {
                "index": eleccion.get("index", 0),
                "delta": {"tool_calls": llamadas},
                "finish_reason": None,
            }
        )

    if not elecciones_llamadas:
        return None

    resultado = {
        clave: valor
        for clave, valor in datos.items()
        if clave != "choices"
    }
    resultado["choices"] = elecciones_llamadas

    return _codificar_evento_sse(resultado)


async def _iterar_eventos_stream(respuesta: httpx.Response, estado: dict):
    lineas = []

    async for linea in respuesta.aiter_lines():
        if linea:
            lineas.append(linea)
            continue

        if not lineas:
            continue

        async for evento in _procesar_evento_stream(lineas, estado):
            yield evento

        lineas = []

    if lineas:
        async for evento in _procesar_evento_stream(lineas, estado):
            yield evento

    llamadas = [
        estado["llamadas"][indice]
        for indice in sorted(estado["llamadas"])
    ]

    if estado["llamada_invalida"]:
        return

    if llamadas and all(
        isinstance(llamada.get("function"), dict)
        and llamada["function"].get("name") == "buscar_en_web"
        for llamada in llamadas
    ):
        estado["llamadas_internas"] = llamadas
        return

    for evento in estado["eventos_retenidos"]:
        yield evento


async def _procesar_evento_stream(lineas: list[str], estado: dict):
    lineas_datos = [
        linea[5:].lstrip(" ")
        for linea in lineas
        if linea.startswith("data:")
    ]

    if not lineas_datos:
        yield ("\n".join(lineas) + "\n\n").encode("utf-8")
        return

    contenido = "\n".join(lineas_datos)

    if contenido == "[DONE]":
        estado["eventos_retenidos"].append(_codificar_evento_sse("[DONE]"))
        return

    try:
        datos = json.loads(contenido)
    except (json.JSONDecodeError, UnicodeDecodeError):
        yield ("\n".join(lineas) + "\n\n").encode("utf-8")
        return

    if not isinstance(datos, dict):
        yield ("\n".join(lineas) + "\n\n").encode("utf-8")
        return

    elecciones = datos.get("choices")
    termina_con_llamada = isinstance(elecciones, list) and any(
        isinstance(eleccion, dict)
        and eleccion.get("finish_reason") == "tool_calls"
        for eleccion in elecciones
    )

    evento_sin_llamadas = _acumular_llamadas_stream(datos, estado)

    if evento_sin_llamadas is not None:
        evento_llamadas = _evento_llamadas_sse(datos)
        if evento_llamadas is not None:
            estado["eventos_retenidos"].append(evento_llamadas)

        if evento_sin_llamadas:
            yield evento_sin_llamadas
        if not termina_con_llamada:
            return

    if termina_con_llamada:
        estado["eventos_retenidos"].append(_codificar_evento_sse(datos))
        return

    yield ("\n".join(lineas) + "\n\n").encode("utf-8")


def _error_sse(codigo: int, mensaje: str) -> bytes:
    error = {
        "error": {
            "message": mensaje,
            "type": _tipo_error(codigo),
            "code": codigo,
        }
    }

    return _codificar_evento_sse(error) + _codificar_evento_sse("[DONE]")


async def _completar_con_stream(
    cliente: httpx.AsyncClient,
    payload: dict,
    respuesta: httpx.Response,
):
    total_llamadas = 0

    for iteracion in range(MAX_ITERACIONES_HERRAMIENTAS + 1):
        estado = {
            "llamadas": {},
            "eventos_retenidos": [],
            "vio_tool_calls": False,
            "llamada_invalida": False,
        }

        try:
            async for evento in _iterar_eventos_stream(respuesta, estado):
                yield evento
        finally:
            await respuesta.aclose()

        if estado["llamada_invalida"]:
            yield _error_sse(502, "El modelo devolvio una llamada de herramienta invalida.")
            return

        llamadas = estado.get("llamadas_internas")
        if not llamadas:
            return

        if iteracion == MAX_ITERACIONES_HERRAMIENTAS:
            yield _error_sse(
                502,
                "El modelo supero el limite de iteraciones de herramientas.",
            )
            return

        try:
            llamadas_validadas = _validar_llamadas_busqueda(llamadas)
        except HTTPException as error:
            yield _error_sse(error.status_code, str(error.detail))
            return

        if total_llamadas + len(llamadas_validadas) > MAX_LLAMADAS_HERRAMIENTAS:
            yield _error_sse(502, "El modelo supero el limite de busquedas por solicitud.")
            return

        mensajes_herramienta = []
        for identificador, query in llamadas_validadas:
            try:
                resultado = await buscar_en_searxng(cliente, query)
            except (httpx.HTTPError, ValueError) as error:
                yield _error_sse(
                    502,
                    "No se pudo completar la busqueda web en SearXNG.",
                )
                return

            mensajes_herramienta.append(
                {
                    "role": "tool",
                    "tool_call_id": identificador,
                    "content": resultado,
                }
            )

        payload["messages"].append(
            {
                "role": "assistant",
                "tool_calls": llamadas,
            }
        )
        payload["messages"].extend(mensajes_herramienta)
        total_llamadas += len(llamadas_validadas)

        try:
            peticion = cliente.build_request(
                "POST",
                f"{LLAMA_URL}/v1/chat/completions",
                json=payload,
            )
            respuesta = await cliente.send(peticion, stream=True)
        except httpx.RequestError as error:
            yield _error_sse(
                503,
                f"No se pudo conectar con llama.cpp: {error}",
            )
            return

        if respuesta.status_code >= 400:
            try:
                cuerpo = await respuesta.aread()
            finally:
                await respuesta.aclose()

            error_motor = _error_motor(respuesta.status_code, cuerpo)
            try:
                datos_error = json.loads(error_motor.body)
                mensaje = datos_error.get("error", {}).get(
                    "message",
                    "llama.cpp devolvio un error.",
                )
            except (json.JSONDecodeError, AttributeError):
                mensaje = "llama.cpp devolvio un error."

            yield _error_sse(respuesta.status_code, mensaje)
            return


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
    payload = construir_payload(agente, solicitud)
    _agregar_herramienta_busqueda(payload)

    # Arranca o cambia de modelo si hace falta y reserva un lugar. Desde acá
    # hay que llamar siempre a coordinador.liberar().
    await _preparar_motor(agente, reservar=True)

    cliente: httpx.AsyncClient = request.app.state.http

    if not solicitud.get("stream", False):
        try:
            return await _completar_sin_stream(cliente, payload)
        finally:
            await coordinador.liberar()

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
                async for fragmento in _completar_con_stream(
                    cliente,
                    payload,
                    respuesta,
                ):
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
