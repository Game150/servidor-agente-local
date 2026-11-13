import asyncio
import json
import logging
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
from app.servicios.skills import NOMBRE_SKILL, listar_skills

coordinador = Coordinador(motor)
logger = logging.getLogger("uvicorn.error")

MAX_ITERACIONES_HERRAMIENTAS = 3
MAX_LLAMADAS_HERRAMIENTAS = 3
MAX_CARACTERES_ARGUMENTOS = 2_000


def _aviso_contexto(tokens: int, contexto: int | None) -> str:
    if not isinstance(contexto, int) or contexto <= 0 or tokens * 100 < contexto * 80:
        return ""
    if tokens * 100 >= contexto * 95:
        nivel = "muy cerca"
    else:
        nivel = "cerca"
    porcentaje = min(tokens * 100 // contexto, 100)
    return (
        f"\n\n⚠️ Contexto {nivel} del límite (aproximadamente {porcentaje} %). "
        "Considera resumir la conversación o iniciar un chat nuevo."
    )


def _tokens_uso(datos: dict) -> int | None:
    uso = datos.get("usage")
    if isinstance(uso, dict):
        total = uso.get("total_tokens")
        if type(total) is int and total >= 0:
            return total
    tiempos = datos.get("timings")
    if isinstance(tiempos, dict):
        valores = [tiempos.get(clave) for clave in ("cache_n", "prompt_n", "predicted_n")]
        if all(type(valor) is int and valor >= 0 for valor in valores):
            return sum(valores)
    return None


def _registrar_consumo(datos: dict, contexto: int | None, motivo: object) -> None:
    uso = datos.get("usage")
    tiempos = datos.get("timings")

    def entero(valor):
        return valor if type(valor) is int and valor >= 0 else None

    entrada = salida = None
    if isinstance(uso, dict):
        entrada = entero(uso.get("prompt_tokens"))
        salida = entero(uso.get("completion_tokens"))
    if isinstance(tiempos, dict):
        cache = entero(tiempos.get("cache_n"))
        prompt = entero(tiempos.get("prompt_n"))
        if entrada is None and cache is not None and prompt is not None:
            entrada = cache + prompt
        if salida is None:
            salida = entero(tiempos.get("predicted_n"))

    total = _tokens_uso(datos)
    if total is None and entrada is not None and salida is not None:
        total = entrada + salida
    valor = lambda numero: str(numero) if numero is not None else "no disponible"
    if total is not None and type(contexto) is int and contexto > 0:
        ocupacion = f"{total * 100 / contexto:.1f}%"
    else:
        ocupacion = "no disponible"
    motivo = motivo if motivo in ("stop", "length", "tool_calls", "content_filter") else "no disponible"
    logger.info(
        "Consumo de inferencia final: entrada=%s salida=%s total=%s contexto_por_slot=%s ocupacion=%s motivo=%s",
        valor(entrada), valor(salida), valor(total), valor(contexto), ocupacion, motivo,
    )


def _admite_aviso(payload: dict) -> bool:
    formato = payload.get("response_format")
    return (
        payload.get("n", 1) == 1
        and (formato is None or formato == {"type": "text"})
        and not payload.get("grammar")
        and not payload.get("json_schema")
    )

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

HERRAMIENTA_SKILL = {
    "type": "function",
    "function": {
        "name": "cargar_skill",
        "description": "Carga las instrucciones de una skill local por su nombre cuando sea relevante.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Nombre de la skill."}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}


# --- Ciclo de vida -----------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.catalogo_skills = None
    try:
        _registrar_catalogo_skills(app, listar_skills())
    except (OSError, ValueError) as error:
        logger.warning("No se pudo listar las skills locales: %s", error)

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


def _contexto_por_slot(agente: dict) -> int:
    return agente["motor"]["ctx_size"] // agente["motor"]["parallel"]


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
                "context_length": _contexto_por_slot(agente),
                "max_model_len": _contexto_por_slot(agente),
                "max_tokens": agente["parametros"]["max_tokens"],
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


def _agregar_herramienta_busqueda(payload: dict, skills: dict | None = None) -> None:
    herramientas = payload.get("tools", [])
    if herramientas is None:
        herramientas = []

    if not isinstance(herramientas, list):
        raise HTTPException(status_code=400, detail="El campo tools debe ser una lista.")

    for herramienta in herramientas:
        if not isinstance(herramienta, dict):
            raise HTTPException(status_code=400, detail="Cada elemento de tools debe ser un objeto.")

        funcion = herramienta.get("function")

        if isinstance(funcion, dict) and (
            funcion.get("name") == "buscar_en_web"
            or (skills and funcion.get("name") == "cargar_skill")
        ):
            raise HTTPException(
                status_code=400,
                detail=f"El nombre de herramienta {funcion['name']} esta reservado.",
            )

    internas = [HERRAMIENTA_SKILL] if skills else []
    payload["tools"] = [*herramientas, *internas, HERRAMIENTA_BUSQUEDA_WEB]


def _registrar_catalogo_skills(aplicacion: FastAPI, skills: dict) -> None:
    nombres = tuple(sorted(skills))
    if getattr(aplicacion.state, "catalogo_skills", None) != nombres:
        logger.info("Skills disponibles: %s", ", ".join(nombres) if nombres else "(ninguna)")
        aplicacion.state.catalogo_skills = nombres


def _preparar_skills(payload: dict) -> dict:
    seleccion = payload.pop("skills", [])
    if (
        not isinstance(seleccion, list)
        or len(seleccion) > 3
        or any(not isinstance(nombre, str) or not nombre for nombre in seleccion)
        or len(set(seleccion)) != len(seleccion)
    ):
        raise HTTPException(status_code=400, detail="skills debe ser una lista de hasta 3 nombres distintos.")
    try:
        skills = listar_skills()
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    _registrar_catalogo_skills(app, skills)
    for nombre in seleccion:
        if nombre not in skills:
            raise HTTPException(status_code=400, detail=f"No existe la skill: {nombre}")
    if skills:
        catalogo = "\n".join(
            f"- {nombre}: {skill['description']}" for nombre, skill in skills.items()
        )
        payload["messages"][0]["content"] += (
            "\n\nSkills locales disponibles. Este es el catalogo completo; usa cargar_skill "
            "solo con un nombre exacto de la lista cuando sea relevante. No inventes "
            "skills ni busques en la web para descubrirlas:\n" + catalogo
        )
    for nombre in seleccion:
        payload["messages"][0]["content"] += (
            f"\n\nInstrucciones de la skill {nombre}:\n{skills[nombre]['content']}"
        )
        logger.info("Skill cargada: %s (seleccion explicita)", nombre)
    return skills


def _es_herramienta_interna(llamada: dict, skills: dict) -> bool:
    funcion = llamada.get("function")
    return isinstance(funcion, dict) and (
        funcion.get("name") == "buscar_en_web"
        or (bool(skills) and funcion.get("name") == "cargar_skill")
    )


async def _ejecutar_herramientas(cliente: httpx.AsyncClient, llamadas: list, skills: dict) -> list:
    validadas = []
    identificadores = set()
    for llamada in llamadas:
        if not isinstance(llamada, dict) or not _es_herramienta_interna(llamada, skills):
            raise HTTPException(status_code=502, detail="El modelo devolvio una herramienta invalida.")
        funcion = llamada["function"]
        if funcion["name"] == "buscar_en_web":
            identificador, argumento = _validar_llamadas_busqueda([llamada])[0]
        else:
            identificador = llamada.get("id")
            argumentos = funcion.get("arguments")
            if (
                llamada.get("type") != "function"
                or not isinstance(identificador, str)
                or not identificador
                or len(identificador) > 128
                or not isinstance(argumentos, str)
                or len(argumentos) > MAX_CARACTERES_ARGUMENTOS
            ):
                raise HTTPException(status_code=502, detail="La llamada a cargar_skill es invalida.")
            try:
                datos = json.loads(argumentos)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=502, detail="Los argumentos de cargar_skill no contienen JSON valido.") from error
            if (
                not isinstance(datos, dict)
                or set(datos) != {"name"}
                or not isinstance(datos["name"], str)
                or len(datos["name"]) > 64
                or not NOMBRE_SKILL.fullmatch(datos["name"])
            ):
                raise HTTPException(status_code=502, detail="La skill solicitada no existe o es invalida.")
            argumento = datos["name"]
        if identificador in identificadores:
            raise HTTPException(status_code=502, detail="Identificador de herramienta duplicado.")
        identificadores.add(identificador)
        validadas.append((identificador, funcion["name"], argumento))

    mensajes = []
    for identificador, nombre, argumento in validadas:
        if nombre == "buscar_en_web":
            try:
                resultado = await buscar_en_searxng(cliente, argumento)
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(status_code=502, detail="No se pudo completar la busqueda web en SearXNG.") from error
        elif argumento not in skills:
            logger.warning("Skill solicitada no disponible en el catalogo local.")
            resultado = (
                "Error: la skill solicitada no esta disponible. "
                "Nombres disponibles: " + ", ".join(skills) +
                ". Usa solo nombres exactos del catalogo."
            )
        else:
            resultado = skills[argumento]["content"]
            logger.info("Skill cargada: %s (solicitada por el modelo)", argumento)
        mensajes.append({"role": "tool", "tool_call_id": identificador, "content": resultado})
    return mensajes


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
    skills: dict | None = None,
    contexto: int | None = None,
) -> Response:
    skills = skills or {}
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
            _registrar_consumo(datos, contexto, elecciones[0].get("finish_reason"))
            tokens = _tokens_uso(datos)
            if (
                len(elecciones) == 1
                and elecciones[0].get("finish_reason") in ("stop", "length")
                and isinstance(mensaje.get("content"), str)
                and mensaje["content"]
                and tokens is not None
                and _admite_aviso(payload)
            ):
                aviso = _aviso_contexto(tokens, contexto)
                if aviso:
                    mensaje["content"] += aviso
                    cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
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
            or not _es_herramienta_interna(llamada, skills)
            for llamada in tool_calls
        ):
            _registrar_consumo(datos, contexto, elecciones[0].get("finish_reason"))
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

        if total_llamadas + len(tool_calls) > MAX_LLAMADAS_HERRAMIENTAS:
            raise HTTPException(
                status_code=502,
                detail="El modelo supero el limite de llamadas de herramientas por solicitud.",
            )

        mensajes_herramienta = await _ejecutar_herramientas(cliente, tool_calls, skills)

        payload["messages"].append(mensaje)
        payload["messages"].extend(mensajes_herramienta)
        total_llamadas += len(tool_calls)

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
        _es_herramienta_interna(llamada, estado.get("skills", {}))
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

    for clave in ("usage", "timings"):
        if isinstance(datos.get(clave), dict):
            estado.setdefault("metricas", {})[clave] = datos[clave]
    elecciones = datos.get("choices")
    if isinstance(elecciones, list):
        for eleccion in elecciones:
            if isinstance(eleccion, dict) and eleccion.get("finish_reason") is not None:
                estado["motivo"] = eleccion["finish_reason"]
    if isinstance(elecciones, list) and len(elecciones) == 1:
        eleccion = elecciones[0]
        if isinstance(eleccion, dict):
            delta = eleccion.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str) and delta["content"]:
                estado["contenido_emitido"] = True
            tokens = _tokens_uso(datos)
            if (
                estado.get("contenido_emitido")
                and not estado.get("vio_tool_calls")
                and not (isinstance(delta, dict) and delta.get("tool_calls"))
                and eleccion.get("finish_reason") in ("stop", "length")
                and tokens is not None
                and estado.get("admite_aviso")
            ):
                aviso = _aviso_contexto(tokens, estado.get("contexto"))
                if aviso:
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str) and delta["content"]:
                        datos["choices"][0]["delta"]["content"] += aviso
                        yield _codificar_evento_sse(datos)
                        return
                    evento = {clave: datos[clave] for clave in (
                        "id", "object", "created", "model", "system_fingerprint"
                    ) if clave in datos}
                    evento["choices"] = [{
                        "index": eleccion.get("index", 0),
                        "delta": {"content": aviso},
                        "finish_reason": None,
                    }]
                    yield _codificar_evento_sse(evento)
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
    skills: dict | None = None,
    contexto: int | None = None,
):
    skills = skills or {}
    total_llamadas = 0

    for iteracion in range(MAX_ITERACIONES_HERRAMIENTAS + 1):
        estado = {
            "llamadas": {},
            "eventos_retenidos": [],
            "vio_tool_calls": False,
            "llamada_invalida": False,
            "skills": skills,
            "contexto": contexto,
            "admite_aviso": _admite_aviso(payload),
            "contenido_emitido": False,
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
            _registrar_consumo(estado.get("metricas", {}), contexto, estado.get("motivo"))
            return

        if iteracion == MAX_ITERACIONES_HERRAMIENTAS:
            yield _error_sse(
                502,
                "El modelo supero el limite de iteraciones de herramientas.",
            )
            return

        if total_llamadas + len(llamadas) > MAX_LLAMADAS_HERRAMIENTAS:
            yield _error_sse(502, "El modelo supero el limite de llamadas de herramientas por solicitud.")
            return

        try:
            mensajes_herramienta = await _ejecutar_herramientas(cliente, llamadas, skills)
        except HTTPException as error:
            yield _error_sse(error.status_code, str(error.detail))
            return

        payload["messages"].append(
            {
                "role": "assistant",
                "tool_calls": llamadas,
            }
        )
        payload["messages"].extend(mensajes_herramienta)
        total_llamadas += len(llamadas)

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
    skills = _preparar_skills(payload)
    _agregar_herramienta_busqueda(payload, skills)
    contexto = _contexto_por_slot(agente)

    # Arranca o cambia de modelo si hace falta y reserva un lugar. Desde acá
    # hay que llamar siempre a coordinador.liberar().
    await _preparar_motor(agente, reservar=True)

    cliente: httpx.AsyncClient = request.app.state.http

    if not solicitud.get("stream", False):
        try:
            return await _completar_sin_stream(cliente, payload, skills, contexto)
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
                    skills,
                    contexto,
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
