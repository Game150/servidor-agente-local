import asyncio
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.servicios.coordinador import Coordinador
from app.servicios.indice_codigo import IndiceCodigo


def respuesta_herramienta(nombre="buscar_en_web", argumentos='{"query":"novedades"}'):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": nombre, "arguments": argumentos},
                        }
                    ],
                }
            }
        ]
    }


def evento_sse(datos):
    return b"data: " + json.dumps(datos).encode("utf-8") + b"\n\n"


class ToolCallingTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_busca_indice_con_motor_de_embeddings(self):
        requests_embeddings = []

        class EmbeddingsHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests_embeddings.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"data": [{"embedding": [1.0, 0.0]}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        with tempfile.TemporaryDirectory() as temp:
            proyecto = Path(temp) / "proyecto"
            proyecto.mkdir()
            subprocess.run(["git", "init", "-q", str(proyecto)], check=True)
            (proyecto / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
            logs = Path(temp) / "logs"
            indice = IndiceCodigo(proyecto, logs / "indices", "http://localhost", "modelo", "")
            with patch.object(indice, "_embedding", return_value=[1.0, 0.0]):
                indice.indexar()

            servidor = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingsHandler)
            hilo = threading.Thread(target=servidor.serve_forever)
            hilo.start()
            peticiones = []

            def responder(request):
                peticiones.append(json.loads(request.content))
                if len(peticiones) == 1:
                    return httpx.Response(200, json=respuesta_herramienta(
                        nombre="buscar_codigo", argumentos='{"consulta":"login"}'
                    ))
                return httpx.Response(200, json={"choices": [{"message": {"content": "Listo"}}]})

            agente = {
                "id": "desarrollador", "modelo": "modelo-chat", "system_prompt": "Sistema",
                "motor": {"ctx_size": 8192, "parallel": 1}, "parametros": {},
            }
            anterior = getattr(main.app.state, "http", None)
            overrides = main.app.dependency_overrides.copy()
            main.app.dependency_overrides[main.verificar_clave] = lambda: None
            try:
                async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as motor:
                    main.app.state.http = motor
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
                    ) as cliente:
                        with (
                            patch("app.main.CODE_SEARCH_ENABLED", True),
                            patch("app.main.CODE_SEARCH_WORKSPACE", proyecto),
                            patch("app.main.CODE_SEARCH_DOCUMENT_PREFIX", ""),
                            patch("app.main.CODE_SEARCH_QUERY_PREFIX", ""),
                            patch("app.main.LOG_DIR", logs),
                            patch("app.main.EMBEDDINGS_MODEL", "modelo"),
                            patch.object(main.motor_embeddings, "url", f"http://127.0.0.1:{servidor.server_port}"),
                            patch("app.main._asegurar_motor_embeddings", new=AsyncMock()),
                            patch("app.main._agente_o_error", return_value=agente),
                            patch("app.main._preparar_motor", new=AsyncMock()),
                            patch.object(main.coordinador, "liberar", new=AsyncMock()),
                        ):
                            respuesta = await cliente.post("/v1/chat/completions", json={
                                "model": "desarrollador",
                                "messages": [{"role": "user", "content": "Busca login"}],
                            })
                self.assertEqual(respuesta.status_code, 200)
                self.assertEqual(requests_embeddings[0]["model"], "local")
                self.assertEqual(requests_embeddings[0]["input"], "login")
                resultado = json.loads(peticiones[1]["messages"][-1]["content"])
                self.assertEqual(resultado["resultados"][0]["ruta"], "auth.py")
                self.assertIn("datos no confiables", resultado["aviso"])
            finally:
                if anterior is None:
                    del main.app.state.http
                else:
                    main.app.state.http = anterior
                main.app.dependency_overrides.clear()
                main.app.dependency_overrides.update(overrides)
                servidor.shutdown()
                servidor.server_close()
                hilo.join()

    def test_busqueda_codigo_prioriza_la_herramienta_del_cliente(self):
        with patch("app.main.CODE_SEARCH_ENABLED", True):
            self.assertTrue(main._usar_busqueda_codigo({"tools": []}))
            for nombre in ("buscar_codigo", "busqueda-codigo-local__buscar_codigo"):
                with self.subTest(nombre=nombre):
                    payload = {"tools": [{"type": "function", "function": {"name": nombre}}]}
                    self.assertFalse(main._usar_busqueda_codigo(payload))
                    main._agregar_herramienta_busqueda(payload, {}, codigo=False)
                    self.assertEqual(payload["tools"][0]["function"]["name"], nombre)
            self.assertTrue(main._usar_busqueda_codigo({
                "tools": [{"type": "function", "function": {"name": "otra_herramienta"}}]
            }))
        with patch("app.main.CODE_SEARCH_ENABLED", False):
            self.assertFalse(main._usar_busqueda_codigo({"tools": []}))

    async def test_busqueda_codigo_del_cliente_no_se_intercepta(self):
        herramienta = {"type": "function", "function": {"name": "buscar_codigo"}}
        payload = {"tools": [herramienta], "messages": [{"role": "user", "content": "Busca"}]}
        llamada = respuesta_herramienta(nombre="buscar_codigo", argumentos='{"consulta":"login"}')
        with patch("app.main.CODE_SEARCH_ENABLED", True):
            codigo = main._usar_busqueda_codigo(payload)
        main._agregar_herramienta_busqueda(payload, {}, codigo)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=llamada))
        ) as cliente:
            with patch("app.main._buscar_codigo_interno", new=AsyncMock()) as buscar:
                respuesta = await main._completar_sin_stream(cliente, payload, busqueda_codigo=codigo)
        buscar.assert_not_awaited()
        self.assertEqual(json.loads(respuesta.body), llamada)

    async def test_busqueda_codigo_sin_stream_continua_con_resultado(self):
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            if len(peticiones) == 1:
                return httpx.Response(200, json=respuesta_herramienta(
                    nombre="buscar_codigo", argumentos='{"consulta":"autenticacion","limite":2}'
                ))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Listo"}}]})

        payload = {"messages": [{"role": "user", "content": "Busca codigo"}]}
        main._agregar_herramienta_busqueda(payload, {}, codigo=True)
        main._inyectar_herramientas(payload, {}, codigo=True)
        self.assertIn("buscar_codigo", payload["messages"][0]["content"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch("app.main._buscar_codigo_interno", new=AsyncMock(return_value="fragmento")) as buscar:
                respuesta = await main._completar_sin_stream(cliente, payload, busqueda_codigo=True)
        buscar.assert_awaited_once_with("autenticacion", 2)
        self.assertEqual(peticiones[1]["messages"][-1]["content"], "fragmento")
        self.assertEqual(json.loads(respuesta.body)["choices"][0]["message"]["content"], "Listo")

    async def test_indexacion_codigo_interna_se_ofrece_y_ejecuta_solo_con_busqueda_activa(self):
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            if len(peticiones) == 1:
                return httpx.Response(200, json=respuesta_herramienta(
                    nombre="indexar_proyecto", argumentos="{}"
                ))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Índice actualizado"}}]})

        payload = {"messages": [{"role": "user", "content": "Indexa el proyecto"}]}
        main._agregar_herramienta_busqueda(payload, {}, codigo=True)
        self.assertIn("indexar_proyecto", [
            herramienta["function"]["name"] for herramienta in payload["tools"]
        ])
        main._inyectar_herramientas(payload, {}, codigo=True)
        self.assertIn("solo cuando el usuario lo pide", payload["messages"][-1]["content"])
        self.assertIn("pide autorizacion antes de indexar", payload["messages"][-1]["content"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch(
                "app.main._indexar_codigo_interno",
                new=AsyncMock(return_value='{"resultado":{"archivos":2}}'),
            ) as indexar:
                respuesta = await main._completar_sin_stream(
                    cliente, payload, busqueda_codigo=True
                )

        indexar.assert_awaited_once_with(False)
        self.assertEqual(
            peticiones[1]["messages"][-1]["content"],
            '{"resultado":{"archivos":2}}',
        )
        self.assertEqual(
            json.loads(respuesta.body)["choices"][0]["message"]["content"],
            "Índice actualizado",
        )

    async def test_indexacion_codigo_registra_workspace_indice_y_modelo(self):
        proyecto = Path("C:/workspace-prueba")
        indice = Mock(
            proyecto=proyecto,
            ruta=Path("C:/indices/codigo-test.sqlite3"),
            modelo="modelo-prueba",
        )
        resumen = {
            "archivos": 2,
            "actualizados": 1,
            "eliminados": 0,
            "omitidos": 0,
            "fragmentos": 3,
        }

        with (
            patch("app.main._crear_indice_codigo_interno", return_value=indice),
            patch("app.main._asegurar_motor_embeddings", new=AsyncMock()),
            patch("app.main.asyncio.to_thread", new=AsyncMock(return_value=resumen)),
            self.assertLogs("uvicorn.error", level="INFO") as registro,
        ):
            resultado = await main._indexar_codigo_interno()

        self.assertIn(str(proyecto), "\n".join(registro.output))
        self.assertIn(str(indice.ruta), "\n".join(registro.output))
        self.assertIn("modelo_embeddings=modelo-prueba", "\n".join(registro.output))
        self.assertEqual(json.loads(resultado), {"resultado": resumen})

    async def test_indexacion_codigo_rechaza_argumentos_invalidos(self):
        for argumentos in ('{"reconstruir":1}', '{"reconstruir":"sí"}', '{"otro":true}', '[]', '{'):
            llamada = respuesta_herramienta(
                nombre="indexar_proyecto", argumentos=argumentos
            )["choices"][0]["message"]["tool_calls"]
            async with httpx.AsyncClient() as cliente:
                with self.subTest(argumentos=argumentos), patch(
                    "app.main._indexar_codigo_interno", new=AsyncMock()
                ) as indexar, self.assertRaises(HTTPException) as error:
                    await main._ejecutar_herramientas(
                        cliente, llamada, {}, codigo=True
                    )
            self.assertEqual(error.exception.status_code, 502)
            indexar.assert_not_awaited()

    async def test_busqueda_codigo_stream_intercepta_llamada(self):
        primer_stream = (
            evento_sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "buscar_codigo", "arguments": '{"consulta":"login"}'},
            }]}, "finish_reason": None}]})
            + evento_sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            + b"data: [DONE]\n\n"
        )
        segundo_stream = (
            evento_sse({"choices": [{"index": 0, "delta": {"content": "Listo"}, "finish_reason": "stop"}]})
            + b"data: [DONE]\n\n"
        )
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            return httpx.Response(200, stream=httpx.ByteStream(segundo_stream))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch("app.main._buscar_codigo_interno", new=AsyncMock(return_value="fragmento")) as buscar:
                salida = b"".join([parte async for parte in main._completar_con_stream(
                    cliente, {"messages": [], "stream": True},
                    httpx.Response(200, stream=httpx.ByteStream(primer_stream)), busqueda_codigo=True,
                )])
        buscar.assert_awaited_once_with("login", 3)
        self.assertEqual(peticiones[0]["messages"][-1]["content"], "fragmento")
        self.assertIn(b"Listo", salida)
        self.assertNotIn(b"buscar_codigo", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)

    async def test_busqueda_codigo_stream_del_cliente_no_se_intercepta(self):
        herramienta = {"type": "function", "function": {"name": "buscar_codigo"}}
        payload = {"tools": [herramienta], "messages": [{"role": "user", "content": "Busca"}]}
        stream = (
            evento_sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "buscar_codigo", "arguments": '{"consulta":"login"}'},
            }]}, "finish_reason": None}]})
            + evento_sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            + b"data: [DONE]\n\n"
        )
        with patch("app.main.CODE_SEARCH_ENABLED", True):
            codigo = main._usar_busqueda_codigo(payload)
        async with httpx.AsyncClient() as cliente:
            with patch("app.main._buscar_codigo_interno", new=AsyncMock()) as buscar:
                salida = b"".join([parte async for parte in main._completar_con_stream(
                    cliente, payload, httpx.Response(200, stream=httpx.ByteStream(stream)),
                    busqueda_codigo=codigo,
                )])
        buscar.assert_not_awaited()
        self.assertIn(b"buscar_codigo", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)

    async def test_busqueda_codigo_rechaza_argumentos_invalidos(self):
        for argumentos in ('{}', '{"consulta":""}', '{"consulta":"x","limite":true}',
                           '{"consulta":"x","limite":6}', '{"consulta":"x","extra":1}', '{'):
            llamada = respuesta_herramienta(
                nombre="buscar_codigo", argumentos=argumentos
            )["choices"][0]["message"]["tool_calls"]
            async with httpx.AsyncClient() as cliente:
                with self.subTest(argumentos=argumentos), patch(
                    "app.main._buscar_codigo_interno", new=AsyncMock()
                ) as buscar, self.assertRaises(HTTPException) as error:
                    await main._ejecutar_herramientas(cliente, llamada, {}, codigo=True)
            self.assertEqual(error.exception.status_code, 502)
            buscar.assert_not_awaited()

    def test_busqueda_codigo_admite_longitud_maxima_con_unicode_escapado(self):
        for longitud, valido in ((1000, True), (1001, False)):
            argumentos = json.dumps({"consulta": "\U0001f600" * longitud, "limite": 5})
            llamada = respuesta_herramienta(
                nombre="buscar_codigo", argumentos=argumentos
            )["choices"][0]["message"]["tool_calls"][0]
            with self.subTest(longitud=longitud):
                if valido:
                    self.assertEqual(len(main._validar_llamada_codigo(llamada)[1]["consulta"]), 1000)
                else:
                    with self.assertRaises(HTTPException):
                        main._validar_llamada_codigo(llamada)

    async def test_busqueda_codigo_indice_faltante_no_inicia_motor(self):
        indice = Mock()
        indice.ruta.is_file.return_value = False
        with patch("app.main.CODE_SEARCH_WORKSPACE", "proyecto"), patch(
            "app.main.IndiceCodigo", return_value=indice
        ), patch("app.main._asegurar_motor_embeddings", new=AsyncMock()) as motor:
            with self.assertRaises(HTTPException) as error:
                await main._buscar_codigo_interno("login", 3)
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("indexar_proyecto", error.exception.detail)
        motor.assert_not_awaited()

    async def test_registra_busqueda_sin_consulta_ni_resultados(self):
        llamada = respuesta_herramienta(argumentos='{"query":"consulta privada"}')["choices"][0]["message"]["tool_calls"]
        async with httpx.AsyncClient() as cliente:
            with (
                patch("app.main.buscar_en_searxng", new=AsyncMock(return_value="resultado privado")),
                self.assertLogs("uvicorn.error", level="INFO") as registro,
            ):
                await main._ejecutar_herramientas(cliente, llamada, {})
        self.assertIn("Busqueda web iniciada", registro.output[0])
        self.assertIn("Busqueda web completada", registro.output[1])
        self.assertNotIn("privada", " ".join(registro.output))
        self.assertNotIn("privado", " ".join(registro.output))

    async def test_registra_fallo_de_busqueda_sin_datos_privados(self):
        llamada = respuesta_herramienta(argumentos='{"query":"consulta privada"}')["choices"][0]["message"]["tool_calls"]
        async with httpx.AsyncClient() as cliente:
            with (
                patch("app.main.buscar_en_searxng", new=AsyncMock(side_effect=httpx.ConnectError("dato privado"))),
                self.assertLogs("uvicorn.error", level="INFO") as registro,
                self.assertRaises(HTTPException) as error,
            ):
                await main._ejecutar_herramientas(cliente, llamada, {})
        self.assertEqual(error.exception.status_code, 502)
        self.assertIn("Busqueda web fallida", registro.output[1])
        self.assertNotIn("privad", " ".join(registro.output))

    def test_busqueda_desactivada_no_se_ofrece_ni_intercepta(self):
        herramienta_cliente = {"type": "function", "function": {"name": "buscar_en_web"}}
        payload = {"messages": [{"role": "user", "content": "Hola"}], "tools": [herramienta_cliente]}
        llamada = respuesta_herramienta()["choices"][0]["message"]["tool_calls"][0]
        with patch("app.main.WEB_SEARCH_ENABLED", False):
            main._agregar_herramienta_busqueda(payload, {})
            main._inyectar_herramientas(payload, {})
            self.assertFalse(main._es_herramienta_interna(llamada, {}))
        self.assertEqual(payload["tools"], [herramienta_cliente])
        self.assertEqual(payload["messages"][0]["content"], "Hola")

    def test_busqueda_desactivada_conserva_skills(self):
        catalogo = {"ejemplo": {"description": "Instrucciones de ejemplo", "content": "Contenido"}}
        payload = {"messages": [{"role": "user", "content": "Hola"}]}
        with patch("app.main.WEB_SEARCH_ENABLED", False):
            main._agregar_herramienta_busqueda(payload, catalogo)
            main._inyectar_herramientas(payload, catalogo)
        self.assertEqual([tool["function"]["name"] for tool in payload["tools"]], ["cargar_skill"])
        self.assertIn("ejemplo", payload["messages"][0]["content"])
        self.assertNotIn("buscar_en_web", payload["messages"][0]["content"])

    async def test_timeout_sin_stream_es_error_504(self):
        def responder(_):
            raise httpx.ReadTimeout("tiempo agotado")

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with self.assertRaises(HTTPException) as error:
                await main._completar_sin_stream(cliente, {"messages": []})
        self.assertEqual(error.exception.status_code, 504)

    async def test_timeout_stream_se_informa_en_sse(self):
        class TimeoutStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: primer evento\n\n"
                raise httpx.ReadTimeout("tiempo agotado")

        respuesta = httpx.Response(200, stream=TimeoutStream())
        async with httpx.AsyncClient() as cliente:
            salida = b"".join([parte async for parte in main._completar_con_stream(
                cliente, {"messages": []}, respuesta
            )])
        self.assertIn(b'"code":504', salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)

    async def test_busqueda_se_agrega_como_mensaje_tool_y_continua_inferencia(self):
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            if len(peticiones) == 1:
                return httpx.Response(200, json=respuesta_herramienta())

            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Respuesta final"}}]},
            )

        payload = {"model": "local", "messages": [{"role": "user", "content": "Busca novedades"}]}
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch(
                "app.main.buscar_en_searxng",
                new=AsyncMock(return_value="Resultados de prueba"),
            ) as buscar:
                respuesta = await main._completar_sin_stream(cliente, payload)

        mensajes = peticiones[1]["messages"]
        self.assertEqual(buscar.await_args.args[1], "novedades")
        self.assertEqual(mensajes[-2]["role"], "assistant")
        self.assertEqual(mensajes[-1], {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Resultados de prueba",
        })
        self.assertEqual(json.loads(respuesta.body)["choices"][0]["message"]["content"], "Respuesta final")

    async def test_no_ejecuta_argumentos_invalidos(self):
        def responder(_):
            return httpx.Response(
                200,
                json=respuesta_herramienta(argumentos='{"query":"ok","extra":true}'),
            )

        payload = {"messages": [{"role": "user", "content": "Busca"}]}
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch("app.main.buscar_en_searxng", new=AsyncMock()) as buscar:
                with self.assertRaises(HTTPException) as error:
                    await main._completar_sin_stream(cliente, payload)

        self.assertEqual(error.exception.status_code, 502)
        buscar.assert_not_awaited()

    async def test_devuelve_tool_calls_de_terceros_sin_ejecutarlos(self):
        contenido = respuesta_herramienta(nombre="herramienta_del_cliente")

        def responder(_):
            return httpx.Response(200, json=contenido)

        payload = {"messages": [{"role": "user", "content": "Busca"}]}
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch("app.main.buscar_en_searxng", new=AsyncMock()) as buscar:
                respuesta = await main._completar_sin_stream(cliente, payload)

        self.assertEqual(json.loads(respuesta.body), contenido)
        buscar.assert_not_awaited()

    async def test_limita_rondas_y_busquedas(self):
        def responder(_):
            return httpx.Response(200, json=respuesta_herramienta())

        payload = {"messages": [{"role": "user", "content": "Busca"}]}
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch(
                "app.main.buscar_en_searxng",
                new=AsyncMock(return_value="Resultados"),
            ) as buscar:
                with self.assertRaises(HTTPException) as error:
                    await main._completar_sin_stream(cliente, payload)

        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(buscar.await_count, main.MAX_LLAMADAS_HERRAMIENTAS)

    async def test_streaming_ejecuta_tool_call_fragmentada_y_transmite_respuesta_final(self):
        peticiones = []
        primer_stream = b"".join(
            [
                evento_sse({
                    "id": "chatcmpl-1",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }),
                evento_sse({
                    "id": "chatcmpl-1",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "buscar_en_web", "arguments": "{\"que"},
                            }]
                        },
                        "finish_reason": None,
                    }],
                }),
                evento_sse({
                    "id": "chatcmpl-1",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "function": {"arguments": "ry\":\"novedades\"}"},
                            }]
                        },
                        "finish_reason": None,
                    }],
                }),
                evento_sse({
                    "id": "chatcmpl-1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }),
                b"data: [DONE]\n\n",
            ]
        )
        segundo_stream = b"".join(
            [
                evento_sse({
                    "id": "chatcmpl-2",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Respuesta "},
                        "finish_reason": None,
                    }],
                }),
                evento_sse({
                    "id": "chatcmpl-2",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": "final"},
                        "finish_reason": None,
                    }],
                }),
                evento_sse({
                    "id": "chatcmpl-2",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }),
                b"data: [DONE]\n\n",
            ]
        )

        def responder(request):
            peticiones.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(segundo_stream),
            )

        payload = {
            "model": "local",
            "stream": True,
            "messages": [{"role": "user", "content": "Busca novedades"}],
        }
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch(
                "app.main.buscar_en_searxng",
                new=AsyncMock(return_value="Resultados de prueba"),
            ) as buscar:
                respuesta = httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=httpx.ByteStream(primer_stream),
                )
                salida = b"".join(
                    [
                        evento
                        async for evento in main._completar_con_stream(
                            cliente,
                            payload,
                            respuesta,
                        )
                    ]
                )

        self.assertEqual(buscar.await_args.args[1], "novedades")
        self.assertEqual(len(peticiones), 1)
        mensajes = peticiones[0]["messages"]
        self.assertEqual(mensajes[-2]["role"], "assistant")
        self.assertEqual(mensajes[-2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(mensajes[-1], {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Resultados de prueba",
        })
        self.assertIn(b"Respuesta ", salida)
        self.assertIn(b"final", salida)
        self.assertNotIn(b"buscar_en_web", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)

    async def test_streaming_sin_tool_call_retransmite_sse_y_no_busca(self):
        fragmentos = b"".join(
            [
                evento_sse({
                    "id": "chatcmpl-1",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hola"},
                        "finish_reason": None,
                    }],
                }),
                evento_sse({
                    "id": "chatcmpl-1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }),
                b"data: [DONE]\n\n",
            ]
        )

        def responder(_):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(fragmentos),
            )

        payload = {
            "model": "local",
            "stream": True,
            "messages": [{"role": "user", "content": "Hola"}],
        }
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch("app.main.buscar_en_searxng", new=AsyncMock()) as buscar:
                respuesta = await cliente.send(
                    cliente.build_request("POST", "http://llama/v1/chat/completions"),
                    stream=True,
                )
                salida = b"".join(
                    [
                        evento
                        async for evento in main._completar_con_stream(
                            cliente,
                            payload,
                            respuesta,
                        )
                    ]
                )

        self.assertIn(b"Hola", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)
        buscar.assert_not_awaited()

    async def test_streaming_preserva_tool_calls_que_no_pertenecen_al_servidor(self):
        evento_llamada = evento_sse({
            "id": "chatcmpl-1",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-client",
                        "type": "function",
                        "function": {"name": "herramienta_del_cliente", "arguments": "{}"},
                    }]
                },
                "finish_reason": None,
            }],
        })
        final = evento_sse({
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }) + b"data: [DONE]\n\n"

        def responder(_):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(evento_llamada + final),
            )

        payload = {
            "model": "local",
            "stream": True,
            "messages": [{"role": "user", "content": "Usa mi herramienta"}],
        }
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            with patch("app.main.buscar_en_searxng", new=AsyncMock()) as buscar:
                respuesta = await cliente.send(
                    cliente.build_request("POST", "http://llama/v1/chat/completions"),
                    stream=True,
                )
                salida = b"".join(
                    [
                        evento
                        async for evento in main._completar_con_stream(
                            cliente,
                            payload,
                            respuesta,
                        )
                    ]
                )

        self.assertIn(b"herramienta_del_cliente", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)
        buscar.assert_not_awaited()

    async def test_streaming_devuelve_error_sse_si_falla_searxng(self):
        stream_tool = b"".join(
            [
                evento_sse({
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "buscar_en_web",
                                    "arguments": '{"query":"novedades"}',
                                },
                            }]
                        },
                        "finish_reason": None,
                    }],
                }),
                evento_sse({
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }),
                b"data: [DONE]\n\n",
            ]
        )

        payload = {
            "model": "local",
            "stream": True,
            "messages": [{"role": "user", "content": "Busca novedades"}],
        }
        main._agregar_herramienta_busqueda(payload)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        ) as cliente:
            respuesta = httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(stream_tool),
            )
            with patch(
                "app.main.buscar_en_searxng",
                new=AsyncMock(side_effect=httpx.ConnectError("sin conexión")),
            ):
                salida = b"".join(
                    [
                        evento
                        async for evento in main._completar_con_stream(
                            cliente,
                            payload,
                            respuesta,
                        )
                    ]
                )

        self.assertIn(b'"code":502', salida)
        self.assertIn(b"No se pudo completar la busqueda web", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)

    async def test_streaming_agrega_busqueda_y_libera_coordinador_al_final(self):
        peticiones = []
        fragmento = b"data: respuesta\n\n"

        def responder(request):
            peticiones.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(fragmento),
            )

        cliente_anterior = getattr(main.app.state, "http", None)
        cliente = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        main.app.state.http = cliente
        request = Request({"type": "http", "app": main.app, "headers": []})
        solicitud = {
            "model": "desarrollador",
            "messages": [{"role": "user", "content": "Hola"}],
            "stream": True,
        }
        agente = {"system_prompt": "Sistema", "parametros": {}, "motor": {"ctx_size": 8192, "parallel": 1}}

        try:
            with (
                patch("app.main._agente_o_error", return_value=agente),
                patch.object(main.coordinador, "preparar", new=AsyncMock()),
                patch.object(main.coordinador, "liberar", new=AsyncMock()) as liberar,
            ):
                respuesta = await main.completar_chat(request, solicitud)
                cuerpo = b"".join([parte async for parte in respuesta.body_iterator])

            self.assertEqual(peticiones[0]["tools"][-1]["function"]["name"], "buscar_en_web")
            self.assertEqual(cuerpo, fragmento)
            liberar.assert_awaited_once()
        finally:
            await cliente.aclose()
            if cliente_anterior is None:
                del main.app.state.http
            else:
                main.app.state.http = cliente_anterior

    async def test_streaming_libera_coordinador_si_el_cliente_cancela(self):
        class CancelStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: evento\n\n"
                raise asyncio.CancelledError

            async def aclose(self):
                return None

        cliente_anterior = getattr(main.app.state, "http", None)
        cliente = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=CancelStream(),
                )
            )
        )
        main.app.state.http = cliente
        request = Request({"type": "http", "app": main.app, "headers": []})
        solicitud = {
            "model": "desarrollador",
            "messages": [{"role": "user", "content": "Hola"}],
            "stream": True,
        }

        try:
            with (
                patch(
                    "app.main._agente_o_error",
                    return_value={
                        "system_prompt": "Sistema",
                        "parametros": {},
                        "motor": {"ctx_size": 8192, "parallel": 1},
                    },
                ),
                patch("app.main._preparar_motor", new=AsyncMock()),
                patch.object(
                    main.coordinador,
                    "liberar",
                    new=AsyncMock(),
                ) as liberar,
            ):
                respuesta = await main.completar_chat(request, solicitud)
                with self.assertRaises(asyncio.CancelledError):
                    async for _ in respuesta.body_iterator:
                        pass

                liberar.assert_awaited_once()
        finally:
            await cliente.aclose()
            if cliente_anterior is None:
                del main.app.state.http
            else:
                main.app.state.http = cliente_anterior

    async def test_libera_coordinador_si_la_busqueda_falla(self):
        await self._verificar_liberacion_en_error(httpx.ConnectError("sin conexion"))

    async def test_libera_coordinador_si_la_solicitud_se_cancela(self):
        await self._verificar_liberacion_en_error(asyncio.CancelledError())

    async def _verificar_liberacion_en_error(self, error):
        def responder(_):
            return httpx.Response(200, json=respuesta_herramienta())

        cliente_anterior = getattr(main.app.state, "http", None)
        cliente = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        main.app.state.http = cliente
        request = Request({"type": "http", "app": main.app, "headers": []})
        solicitud = {
            "model": "desarrollador",
            "messages": [{"role": "user", "content": "Busca"}],
        }
        agente = {"system_prompt": "Sistema", "parametros": {}, "motor": {"ctx_size": 8192, "parallel": 1}}

        try:
            with (
                patch("app.main._agente_o_error", return_value=agente),
                patch.object(main.coordinador, "preparar", new=AsyncMock()),
                patch.object(main.coordinador, "liberar", new=AsyncMock()) as liberar,
                patch("app.main.buscar_en_searxng", new=AsyncMock(side_effect=error)),
            ):
                with self.assertRaises((HTTPException, asyncio.CancelledError)):
                    await main.completar_chat(request, solicitud)

                liberar.assert_awaited_once()
        finally:
            await cliente.aclose()
            if cliente_anterior is None:
                del main.app.state.http
            else:
                main.app.state.http = cliente_anterior


class CoordinadorParadaTests(unittest.IsolatedAsyncioTestCase):
    async def test_parada_espera_a_peticiones_y_bloquea_nuevas(self):
        motor = Mock()
        motor.es_compatible.return_value = True
        motor.detener.return_value = {"estado": "detenido"}
        coordinador = Coordinador(motor)
        agente = {}
        await coordinador.preparar(agente, reservar=True)
        parada = asyncio.create_task(coordinador.detener())
        await asyncio.sleep(0)
        nueva = asyncio.create_task(coordinador.preparar(agente, reservar=True))
        await asyncio.sleep(0)
        self.assertFalse(parada.done())
        self.assertFalse(nueva.done())
        motor.detener.assert_not_called()
        await coordinador.liberar()
        self.assertEqual(await asyncio.wait_for(parada, 2), {"estado": "detenido"})
        await asyncio.wait_for(nueva, 2)
        motor.detener.assert_called_once()
        await coordinador.liberar()

    async def test_cancelar_parada_en_espera_desbloquea_peticiones(self):
        motor = Mock()
        motor.es_compatible.return_value = True
        coordinador = Coordinador(motor)
        await coordinador.preparar({}, reservar=True)
        parada = asyncio.create_task(coordinador.detener())
        await asyncio.sleep(0)
        parada.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await parada
        await asyncio.wait_for(coordinador.preparar({}, reservar=True), 2)
        motor.detener.assert_not_called()
        await coordinador.liberar()
        await coordinador.liberar()

    async def test_cancelar_parada_en_curso_espera_al_hilo(self):
        iniciado = threading.Event()
        terminar = threading.Event()
        motor = Mock()
        motor.es_compatible.return_value = True

        def detener():
            iniciado.set()
            terminar.wait(timeout=3)
            return {"estado": "detenido"}

        motor.detener.side_effect = detener
        coordinador = Coordinador(motor)
        parada = asyncio.create_task(coordinador.detener())
        try:
            self.assertTrue(await asyncio.to_thread(iniciado.wait, 2))
            parada.cancel()
            nueva = asyncio.create_task(coordinador.preparar({}, reservar=True))
            await asyncio.sleep(0)
            self.assertFalse(nueva.done())
            terminar.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(parada, 2)
            await asyncio.wait_for(nueva, 2)
            await coordinador.liberar()
        finally:
            terminar.set()


if __name__ == "__main__":
    unittest.main()
