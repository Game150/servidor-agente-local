import json
import unittest

import httpx

from app import main


def sse(datos):
    return b"data: " + json.dumps(datos).encode("utf-8") + b"\n\n"


class ContextoTests(unittest.IsolatedAsyncioTestCase):
    def test_log_indica_campos_no_disponibles(self):
        with self.assertLogs("uvicorn.error", level="INFO") as logs:
            main._registrar_consumo({}, None, None)
        self.assertIn("entrada=no disponible", logs.output[0])
        self.assertIn("ocupacion=no disponible", logs.output[0])
        self.assertIn("motivo=no disponible", logs.output[0])

    def test_umbrales_y_metricas(self):
        self.assertEqual(main._aviso_contexto(799, 1000), "")
        self.assertIn("cerca", main._aviso_contexto(800, 1000))
        self.assertNotIn("muy cerca", main._aviso_contexto(949, 1000))
        self.assertIn("muy cerca", main._aviso_contexto(950, 1000))
        self.assertEqual(main._aviso_contexto(1000, None), "")
        self.assertEqual(main._tokens_uso({"timings": {
            "cache_n": 400, "prompt_n": 390, "predicted_n": 10,
        }}), 800)
        self.assertIsNone(main._tokens_uso({"timings": {"prompt_n": 800}}))

    async def test_respuesta_no_stream(self):
        async def verificar(total, formato=None):
            def responder(_):
                return httpx.Response(200, json={
                    "choices": [{"finish_reason": "stop", "message": {"content": "Respuesta"}}],
                    "usage": {"total_tokens": total},
                })
            payload = {"messages": [], "response_format": formato}
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
                resultado = await main._completar_sin_stream(cliente, payload, contexto=1000)
            return json.loads(resultado.body)["choices"][0]["message"]["content"]
        self.assertEqual(await verificar(799), "Respuesta")
        self.assertIn("80 %", await verificar(800))
        self.assertIn("muy cerca", await verificar(950))
        self.assertEqual(await verificar(999, {"type": "json_object"}), "Respuesta")

    async def test_stream_aviso_antes_de_finish(self):
        async def verificar(total, formato=None):
            datos = (
                sse({"id": "test", "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": {"content": "Respuesta"}, "finish_reason": None}]})
                + sse({"id": "test", "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "timings": {"cache_n": 200, "prompt_n": total - 210, "predicted_n": 10}})
                + b"data: [DONE]\n\n"
            )
            respuesta = httpx.Response(200, stream=httpx.ByteStream(datos))
            async with httpx.AsyncClient() as cliente:
                salida = b"".join([fragmento async for fragmento in main._completar_con_stream(
                    cliente, {"messages": [], "response_format": formato}, respuesta, contexto=1000
                )])
            return salida.decode("utf-8")
        self.assertNotIn("Contexto", await verificar(799))
        resultado = await verificar(950)
        self.assertIn("muy cerca", resultado)
        eventos = [json.loads(linea[6:]) for linea in resultado.splitlines() if linea.startswith("data: {")]
        self.assertIn("muy cerca", eventos[-2]["choices"][0]["delta"]["content"])
        self.assertEqual(eventos[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(resultado.count("data: [DONE]"), 1)
        self.assertNotIn("Contexto", await verificar(950, {"type": "json_object"}))

    async def test_stream_con_texto_en_ultimo_evento(self):
        datos = sse({"choices": [{"index": 0, "delta": {"content": "Ultimo texto"},
            "finish_reason": "stop"}], "timings": {
                "cache_n": 0, "prompt_n": 799, "predicted_n": 1,
            }}) + b"data: [DONE]\n\n"
        respuesta = httpx.Response(200, stream=httpx.ByteStream(datos))
        async with httpx.AsyncClient() as cliente:
            salida = b"".join([fragmento async for fragmento in main._completar_con_stream(
                cliente, {"messages": []}, respuesta, contexto=1000
            )])
        datos_evento = json.loads(salida.splitlines()[0][6:])
        contenido = datos_evento["choices"][0]["delta"]["content"]
        self.assertTrue(contenido.startswith("Ultimo texto"))
        self.assertIn("80 %", contenido)

    async def test_log_sin_stream_muestra_motivo_y_consumo(self):
        respuesta = {"choices": [{"finish_reason": "length",
            "message": {"content": "contenido privado"}}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 100, "total_tokens": 800}}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=respuesta))
        ) as cliente:
            with self.assertLogs("uvicorn.error", level="INFO") as logs:
                await main._completar_sin_stream(cliente, {"messages": []}, contexto=1000)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("entrada=700 salida=100 total=800", logs.output[0])
        self.assertIn("contexto_por_slot=1000 ocupacion=80.0% motivo=length", logs.output[0])
        self.assertNotIn("contenido privado", logs.output[0])

    async def test_log_stream_usa_cache_y_uso_posterior_al_finish(self):
        datos = (
            sse({"choices": [{"index": 0, "delta": {"content": "privado"}, "finish_reason": None}]})
            + sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                   "timings": {"cache_n": 200, "prompt_n": 500, "predicted_n": 100}})
            + sse({"choices": [], "usage": {"prompt_tokens": 700,
                   "completion_tokens": 100, "total_tokens": 800}})
            + b"data: [DONE]\n\n"
        )
        respuesta = httpx.Response(200, stream=httpx.ByteStream(datos))
        async with httpx.AsyncClient() as cliente:
            with self.assertLogs("uvicorn.error", level="INFO") as logs:
                salida = b"".join([fragmento async for fragmento in main._completar_con_stream(
                    cliente, {"messages": []}, respuesta, contexto=1000)])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("entrada=700 salida=100 total=800", logs.output[0])
        self.assertIn("ocupacion=80.0% motivo=length", logs.output[0])
        self.assertNotIn("privado", logs.output[0])
        self.assertEqual(salida.count(b"data: [DONE]"), 1)
