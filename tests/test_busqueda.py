import unittest

import httpx

from app.servicios.busqueda import buscar_en_searxng


class BuscarEnSearxngTests(unittest.IsolatedAsyncioTestCase):
    async def test_limita_resultados_y_marca_contenido_no_confiable(self):
        def responder(_):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": f"Resultado {indice}",
                            "url": f"https://example.test/{indice}",
                            "content": "Extracto",
                        }
                        for indice in range(5)
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as cliente:
            resultado = await buscar_en_searxng(cliente, "consulta")

        self.assertIn("contenido externo no confiable", resultado)
        self.assertEqual(resultado.count("- Titulo:"), 3)
        self.assertLessEqual(len(resultado), 5_000)

    async def test_rechaza_consulta_mayor_al_limite(self):
        async with httpx.AsyncClient() as cliente:
            with self.assertRaises(ValueError):
                await buscar_en_searxng(cliente, "x" * 501)

    async def test_rechaza_respuesta_mayor_al_limite(self):
        contenido = b'{"results":[]}' + b" " * 1_000_001

        def responder(_):
            return httpx.Response(200, content=contenido)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(responder)
        ) as cliente:
            with self.assertRaises(ValueError):
                await buscar_en_searxng(cliente, "consulta")


if __name__ == "__main__":
    unittest.main()
