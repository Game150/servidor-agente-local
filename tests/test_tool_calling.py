import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from app import main


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
        agente = {"system_prompt": "Sistema", "parametros": {}}

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
        agente = {"system_prompt": "Sistema", "parametros": {}}

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


if __name__ == "__main__":
    unittest.main()
