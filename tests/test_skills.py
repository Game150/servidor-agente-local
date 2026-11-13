import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app import main
from app.servicios import skills


def evento(datos):
    return b"data: " + json.dumps(datos).encode("utf-8") + b"\n\n"


class SkillsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.raiz = Path(self.temporal.name)
        self.parche = patch.object(skills, "SKILLS_DIR", self.raiz)
        self.parche.start()
        self.addCleanup(self.parche.stop)
        carpeta = self.raiz / "ejemplo"
        carpeta.mkdir()
        (carpeta / "SKILL.md").write_text(
            "---\nname: ejemplo\ndescription: Explica ejemplos.\n---\n"
            "Responde usando la instruccion de ejemplo.\n",
            encoding="utf-8",
        )
        anterior = getattr(main.app.state, "catalogo_skills", None)
        main.app.state.catalogo_skills = ("ejemplo",)
        self.addCleanup(setattr, main.app.state, "catalogo_skills", anterior)

    async def test_registra_catalogo_al_iniciar(self):
        with patch.object(main.motor, "detener"):
            with self.assertLogs("uvicorn.error", level="INFO") as registro:
                async with main.lifespan(main.app):
                    pass
        self.assertIn("INFO:uvicorn.error:Skills disponibles: ejemplo", registro.output)
        self.assertNotIn("Responde usando", "\n".join(registro.output))

    async def test_catalogo_invalido_no_impide_arrancar(self):
        (self.raiz / "ejemplo" / "SKILL.md").write_text("invalida", encoding="utf-8")
        with patch.object(main.motor, "detener"):
            with self.assertLogs("uvicorn.error", level="WARNING") as registro:
                async with main.lifespan(main.app):
                    pass
        self.assertIn("No se pudo listar las skills locales", "\n".join(registro.output))

    def test_registra_cambios_del_catalogo_sin_repetirlo(self):
        carpeta = self.raiz / "otra"
        carpeta.mkdir()
        (carpeta / "SKILL.md").write_text(
            "---\nname: otra\ndescription: Otra skill.\n---\nInstrucciones.\n",
            encoding="utf-8",
        )
        payload = {"messages": [{"role": "system", "content": ""}]}
        with patch.object(main.logger, "info") as registro:
            main._preparar_skills(payload)
            main._preparar_skills({"messages": [{"role": "system", "content": ""}]})
            (carpeta / "SKILL.md").unlink()
            main._preparar_skills({"messages": [{"role": "system", "content": ""}]})
        self.assertEqual(registro.call_count, 2)
        self.assertEqual(registro.call_args_list[0].args, ("Skills disponibles: %s", "ejemplo, otra"))
        self.assertEqual(registro.call_args_list[1].args, ("Skills disponibles: %s", "ejemplo"))

    def test_registra_catalogo_vacio(self):
        (self.raiz / "ejemplo" / "SKILL.md").unlink()
        with self.assertLogs("uvicorn.error", level="INFO") as registro:
            main._preparar_skills({"messages": [{"role": "system", "content": ""}]})
        self.assertIn("INFO:uvicorn.error:Skills disponibles: (ninguna)", registro.output)

    def test_descubre_y_carga_instrucciones_solo_al_seleccionar(self):
        payload = {"messages": [{"role": "system", "content": "Sistema"}]}
        with patch.object(main.logger, "info") as registro:
            catalogo = main._preparar_skills(payload)
        registro.assert_not_called()
        self.assertIn("ejemplo: Explica ejemplos.", payload["messages"][0]["content"])
        self.assertNotIn("Responde usando", payload["messages"][0]["content"])
        self.assertIn("ejemplo", catalogo)

        payload = {"messages": [{"role": "system", "content": "Sistema"}], "skills": ["ejemplo"]}
        with self.assertLogs("uvicorn.error", level="INFO") as registro:
            main._preparar_skills(payload)
        self.assertEqual(registro.output, [
            "INFO:uvicorn.error:Skill cargada: ejemplo (seleccion explicita)"
        ])
        self.assertNotIn("skills", payload)
        self.assertIn("Responde usando", payload["messages"][0]["content"])

    def test_rechaza_seleccion_invalida_y_skill_inexistente(self):
        for seleccion in (["inexistente"], ["ejemplo", "ejemplo"], [123], "ejemplo"):
            with self.subTest(seleccion=seleccion):
                with self.assertRaises(HTTPException) as error:
                    main._preparar_skills({"messages": [{"role": "system", "content": ""}], "skills": seleccion})
                self.assertEqual(error.exception.status_code, 400)

    def test_rechaza_frontmatter_y_nombre_invalidos(self):
        ruta = self.raiz / "ejemplo" / "SKILL.md"
        for contenido in (
            "sin frontmatter",
            "---\nname: otro\ndescription: prueba\n---\ntexto",
            "---\nname: ejemplo\ndescription: ''\n---\ntexto",
            "---\nname: ejemplo\ndescription: prueba\n---\n",
        ):
            ruta.write_text(contenido, encoding="utf-8")
            with self.subTest(contenido=contenido):
                with self.assertRaises(ValueError):
                    skills.listar_skills()

    def test_rechaza_archivo_demasiado_grande(self):
        ruta = self.raiz / "ejemplo" / "SKILL.md"
        ruta.write_bytes(b"x" * (skills.MAX_BYTES_SKILL + 1))
        with self.assertRaisesRegex(ValueError, "limite"):
            skills.listar_skills()

    async def test_carga_automatica_sin_stream(self):
        catalogo = skills.listar_skills()
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            if len(peticiones) == 1:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "tool_calls": [{
                        "id": "call-1", "type": "function",
                        "function": {"name": "cargar_skill", "arguments": '{"name":"ejemplo"}'},
                    }],
                }}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Listo"}}]})

        payload = {"messages": [{"role": "system", "content": ""}]}
        main._agregar_herramienta_busqueda(payload, catalogo)
        with self.assertLogs("uvicorn.error", level="INFO") as registro:
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
                respuesta = await main._completar_sin_stream(cliente, payload, catalogo)
        self.assertIn(
            "INFO:uvicorn.error:Skill cargada: ejemplo (solicitada por el modelo)",
            registro.output,
        )
        self.assertEqual(json.loads(respuesta.body)["choices"][0]["message"]["content"], "Listo")
        self.assertIn("Responde usando", peticiones[1]["messages"][-1]["content"])
        self.assertEqual(peticiones[1]["messages"][-1]["tool_call_id"], "call-1")

    async def test_skill_inexistente_permite_recuperarse_sin_stream(self):
        catalogo = skills.listar_skills()
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            if len(peticiones) == 1:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "tool_calls": [{
                        "id": "call-1", "type": "function",
                        "function": {"name": "cargar_skill", "arguments": '{"name":"desconocida"}'},
                    }],
                }}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Skill no disponible"}}]})

        payload = {"messages": [{"role": "system", "content": ""}]}
        main._agregar_herramienta_busqueda(payload, catalogo)
        with self.assertLogs("uvicorn.error", level="WARNING") as registro:
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
                respuesta = await main._completar_sin_stream(cliente, payload, catalogo)
        self.assertIn("Skill solicitada no disponible", "\n".join(registro.output))
        self.assertEqual(json.loads(respuesta.body)["choices"][0]["message"]["content"], "Skill no disponible")
        resultado = peticiones[1]["messages"][-1]
        self.assertEqual(resultado["tool_call_id"], "call-1")
        self.assertIn("Error", resultado["content"])
        self.assertIn("ejemplo", resultado["content"])

    async def test_endpoint_seleccion_explicita_no_reenvia_campo_a_llama(self):
        peticiones = []

        def responder(request):
            peticiones.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Listo"}}]})

        overrides = main.app.dependency_overrides.copy()
        main.app.dependency_overrides[main.verificar_clave] = lambda: None
        anterior = getattr(main.app.state, "http", None)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as motor:
                main.app.state.http = motor
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
                ) as cliente:
                    with patch("app.main._preparar_motor", new=AsyncMock()), patch.object(
                        main.coordinador, "liberar", new=AsyncMock()
                    ):
                        respuesta = await cliente.post("/v1/chat/completions", json={
                            "model": "desarrollador",
                            "messages": [{"role": "user", "content": "Explica"}],
                            "skills": ["ejemplo"],
                        })
            self.assertEqual(respuesta.status_code, 200)
            self.assertNotIn("skills", peticiones[0])
            self.assertIn("Responde usando", peticiones[0]["messages"][0]["content"])
        finally:
            if anterior is None:
                del main.app.state.http
            else:
                main.app.state.http = anterior
            main.app.dependency_overrides.clear()
            main.app.dependency_overrides.update(overrides)

    async def test_carga_automatica_con_stream_fragmentado(self):
        catalogo = skills.listar_skills()
        peticiones = []
        primer_stream = b"".join([
            evento({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "cargar_", "arguments": '{"name":"'},
            }]}, "finish_reason": None}]}),
            evento({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "function": {"name": "skill", "arguments": 'ejemplo"}'},
            }]}, "finish_reason": None}]}),
            evento({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ])
        final = evento({"choices": [{"index": 0, "delta": {"content": "Listo"}, "finish_reason": "stop"}]}) + b"data: [DONE]\n\n"

        def responder(request):
            peticiones.append(json.loads(request.content))
            return httpx.Response(200, stream=httpx.ByteStream(final))

        payload = {"messages": [{"role": "system", "content": ""}], "stream": True}
        respuesta = httpx.Response(200, stream=httpx.ByteStream(primer_stream))
        with self.assertLogs("uvicorn.error", level="INFO") as registro:
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
                salida = b"".join([fragmento async for fragmento in main._completar_con_stream(
                    cliente, payload, respuesta, catalogo
                )])
        self.assertIn(
            "INFO:uvicorn.error:Skill cargada: ejemplo (solicitada por el modelo)",
            registro.output,
        )
        self.assertIn(b"Listo", salida)
        self.assertNotIn(b"cargar_skill", salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)
        self.assertIn("Responde usando", peticiones[0]["messages"][-1]["content"])

    async def test_rechaza_argumentos_invalidos(self):
        catalogo = skills.listar_skills()
        llamada = {"id": "1", "type": "function", "function": {
            "name": "cargar_skill", "arguments": '{"name":"../ejemplo"}',
        }}
        async with httpx.AsyncClient() as cliente:
            with self.assertRaises(HTTPException) as error:
                await main._ejecutar_herramientas(cliente, [llamada], catalogo)
        self.assertEqual(error.exception.status_code, 502)

    async def test_valida_todas_las_llamadas_antes_de_buscar(self):
        catalogo = skills.listar_skills()
        llamadas = [
            {"id": "duplicado", "type": "function", "function": {
                "name": "buscar_en_web", "arguments": '{"query":"prueba"}',
            }},
            {"id": "duplicado", "type": "function", "function": {
                "name": "cargar_skill", "arguments": '{"name":"ejemplo"}',
            }},
        ]
        async with httpx.AsyncClient() as cliente:
            with patch("app.main.buscar_en_searxng", new=AsyncMock()) as buscar:
                with self.assertRaises(HTTPException):
                    await main._ejecutar_herramientas(cliente, llamadas, catalogo)
                buscar.assert_not_awaited()

    async def test_skill_inexistente_permite_recuperarse_con_stream(self):
        catalogo = skills.listar_skills()
        primer_stream = b"".join([
            evento({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "cargar_skill", "arguments": '{"name":"desconocida"}'},
            }]}, "finish_reason": None}]}),
            evento({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ])
        respuesta = httpx.Response(200, stream=httpx.ByteStream(primer_stream))
        peticiones = []
        final = evento({"choices": [{"index": 0, "delta": {"content": "Skill no disponible"}, "finish_reason": "stop"}]}) + b"data: [DONE]\n\n"

        def responder(request):
            peticiones.append(json.loads(request.content))
            return httpx.Response(200, stream=httpx.ByteStream(final))

        with self.assertLogs("uvicorn.error", level="WARNING"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
                salida = b"".join([fragmento async for fragmento in main._completar_con_stream(
                    cliente, {"messages": []}, respuesta, catalogo
                )])
        self.assertIn(b"Skill no disponible", salida)
        self.assertNotIn(b'"error"', salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)
        resultado = peticiones[0]["messages"][-1]
        self.assertEqual(resultado["tool_call_id"], "call-1")
        self.assertIn("Error", resultado["content"])
        self.assertIn("ejemplo", resultado["content"])

    async def test_skill_con_nombre_invalido_en_stream_termina_con_error(self):
        catalogo = skills.listar_skills()
        primer_stream = b"".join([
            evento({"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "cargar_skill", "arguments": '{"name":"../ejemplo"}'},
            }]}, "finish_reason": None}]}),
            evento({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ])
        respuesta = httpx.Response(200, stream=httpx.ByteStream(primer_stream))
        async with httpx.AsyncClient() as cliente:
            salida = b"".join([fragmento async for fragmento in main._completar_con_stream(
                cliente, {"messages": []}, respuesta, catalogo
            )])
        self.assertIn(b'"error"', salida)
        self.assertEqual(salida.count(b"data: [DONE]"), 1)
