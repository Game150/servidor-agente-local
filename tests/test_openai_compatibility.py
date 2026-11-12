import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import main


class OpenAICompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.overrides = main.app.dependency_overrides.copy()
        main.app.dependency_overrides[main.verificar_clave] = lambda: None

    async def asyncTearDown(self):
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(self.overrides)

    async def test_models_lists_configured_agent_ids(self):
        agentes = [{"id": "desarrollador"}, {"id": "revisor"}]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        ) as cliente:
            with patch("app.main._agentes_o_error", return_value=agentes):
                respuesta = await cliente.get("/v1/models")

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["object"], "list")
        self.assertEqual(
            [modelo["id"] for modelo in respuesta.json()["data"]],
            ["desarrollador", "revisor"],
        )

    async def test_chat_completion_routes_agent_and_preserves_openai_response(self):
        agente = {
            "id": "desarrollador",
            "system_prompt": "Responde de forma concisa.",
            "motor": {"ctx_size": 8192, "parallel": 1},
            "parametros": {
                "temperature": 0.3,
                "top_p": 0.9,
                "max_tokens": 256,
                "forzar": False,
            },
        }
        peticiones_motor = []
        respuesta_modelo = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hola."},
                    "finish_reason": "stop",
                }
            ],
        }

        def responder_motor(peticion):
            peticiones_motor.append(json.loads(peticion.content))
            return httpx.Response(200, json=respuesta_modelo)

        cliente_anterior = getattr(main.app.state, "http", None)
        cliente_motor = httpx.AsyncClient(
            transport=httpx.MockTransport(responder_motor),
        )
        main.app.state.http = cliente_motor

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://testserver",
            ) as cliente:
                with (
                    patch("app.main._agente_o_error", return_value=agente),
                    patch("app.main._preparar_motor", new=AsyncMock()),
                    patch.object(
                        main.coordinador,
                        "liberar",
                        new=AsyncMock(),
                    ),
                ):
                    respuesta = await cliente.post(
                        "/v1/chat/completions",
                        json={
                            "model": "desarrollador",
                            "messages": [{"role": "user", "content": "Hola"}],
                            "max_completion_tokens": 128,
                        },
                    )

            self.assertEqual(respuesta.status_code, 200)
            self.assertEqual(respuesta.json(), respuesta_modelo)
            payload_motor = peticiones_motor[0]
            self.assertEqual(payload_motor["model"], "local")
            self.assertEqual(payload_motor["max_tokens"], 128)
            self.assertEqual(payload_motor["messages"][0]["role"], "system")
            self.assertIn("Responde de forma concisa.", payload_motor["messages"][0]["content"])
        finally:
            await cliente_motor.aclose()
            if cliente_anterior is None:
                del main.app.state.http
            else:
                main.app.state.http = cliente_anterior

    async def test_api_key_uses_bearer_authentication(self):
        main.app.dependency_overrides.pop(main.verificar_clave, None)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        ) as cliente:
            with (
                patch("app.main.API_KEY", "local-test-key"),
                patch("app.main._agentes_o_error", return_value=[]),
            ):
                sin_clave = await cliente.get("/v1/models")
                con_clave = await cliente.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer local-test-key"},
                )

        self.assertEqual(sin_clave.status_code, 401)
        self.assertEqual(sin_clave.json()["error"]["type"], "authentication_error")
        self.assertEqual(con_clave.status_code, 200)

    async def test_unknown_agent_returns_openai_error_shape(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        ) as cliente:
            with patch("app.main.obtener_agente", return_value=None):
                respuesta = await cliente.post(
                    "/v1/chat/completions",
                    json={
                        "model": "inexistente",
                        "messages": [{"role": "user", "content": "Hola"}],
                    },
                )

        self.assertEqual(respuesta.status_code, 404)
        self.assertEqual(respuesta.json()["error"]["type"], "not_found_error")

    async def test_streaming_returns_server_sent_events(self):
        fragmento = b"data: [DONE]\n\n"

        def responder_motor(_):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(fragmento),
            )

        cliente_anterior = getattr(main.app.state, "http", None)
        cliente_motor = httpx.AsyncClient(
            transport=httpx.MockTransport(responder_motor),
        )
        main.app.state.http = cliente_motor

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://testserver",
            ) as cliente:
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
                    respuesta = await cliente.post(
                        "/v1/chat/completions",
                        json={
                            "model": "desarrollador",
                            "messages": [{"role": "user", "content": "Hola"}],
                            "stream": True,
                        },
                    )

            self.assertEqual(respuesta.status_code, 200)
            self.assertIn("text/event-stream", respuesta.headers["content-type"])
            self.assertEqual(respuesta.content, fragmento)
            liberar.assert_awaited_once()
        finally:
            await cliente_motor.aclose()
            if cliente_anterior is None:
                del main.app.state.http
            else:
                main.app.state.http = cliente_anterior


if __name__ == "__main__":
    unittest.main()
