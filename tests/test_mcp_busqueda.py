import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


class EmbeddingsHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        vector = [1.0, 0.0] if "login" in body["input"] else [0.0, 1.0]
        respuesta = json.dumps({"data": [{"embedding": vector}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(respuesta)))
        self.end_headers()
        self.wfile.write(respuesta)

    def log_message(self, format, *args):
        pass


@unittest.skipIf(ClientSession is None, "Instala requirements-mcp.txt para probar MCP.")
class McpBusquedaTests(unittest.IsolatedAsyncioTestCase):
    async def test_cliente_stdio_indexa_y_busca(self):
        with tempfile.TemporaryDirectory() as temporal:
            proyecto = Path(temporal) / "proyecto"
            proyecto.mkdir()
            subprocess.run(["git", "init", "-q", str(proyecto)], check=True)
            (proyecto / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
            servidor = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingsHandler)
            hilo = threading.Thread(target=servidor.serve_forever)
            hilo.start()
            try:
                script = Path(__file__).resolve().parents[1] / "mcp_busqueda.py"
                parametros = StdioServerParameters(
                    command=sys.executable,
                    args=[str(script)],
                    env={
                        **os.environ,
                        "MCP_WORKSPACE": str(proyecto),
                        "MCP_API_URL": f"http://127.0.0.1:{servidor.server_port}",
                        "EMBEDDINGS_MODEL": "modelo",
                        "LOG_DIR": str(Path(temporal) / "logs"),
                        "API_KEY": "",
                    },
                )
                async with stdio_client(parametros) as (lectura, escritura):
                    async with ClientSession(lectura, escritura) as sesion:
                        await sesion.initialize()
                        herramientas = await sesion.list_tools()
                        self.assertEqual(
                            {herramienta.name for herramienta in herramientas.tools},
                            {"indexar_proyecto", "buscar_codigo", "leer_lineas"},
                        )
                        esquemas = {
                            herramienta.name: herramienta.inputSchema
                            for herramienta in herramientas.tools
                        }
                        self.assertEqual(
                            set(esquemas["buscar_codigo"]["properties"]),
                            {"consulta", "limite"},
                        )
                        self.assertEqual(
                            set(esquemas["buscar_codigo"]["required"]),
                            {"consulta"},
                        )
                        self.assertEqual(
                            set(esquemas["leer_lineas"]["properties"]),
                            {"ruta", "linea", "margen"},
                        )
                        self.assertEqual(
                            set(esquemas["leer_lineas"]["required"]),
                            {"ruta", "linea"},
                        )
                        antes = await asyncio.wait_for(
                            sesion.call_tool(
                                "buscar_codigo",
                                {"consulta": "consulta-privada-7f38"},
                            ),
                            timeout=30,
                        )
                        self.assertTrue(antes.isError)
                        self.assertIn("indexar_proyecto", antes.content[0].text)
                        indexado = await asyncio.wait_for(
                            sesion.call_tool("indexar_proyecto", {}), timeout=30
                        )
                        self.assertFalse(indexado.isError)
                        resultado = await asyncio.wait_for(
                            sesion.call_tool(
                                "buscar_codigo",
                                {"consulta": "consulta-privada-7f38"},
                            ),
                            timeout=30,
                        )
                        self.assertFalse(resultado.isError)
                        self.assertIn("auth.py", resultado.content[0].text)
                        self.assertIn("def login", resultado.content[0].text)
                        lectura = await asyncio.wait_for(
                            sesion.call_tool(
                                "leer_lineas",
                                {"ruta": "auth.py", "linea": 1, "margen": 0},
                            ),
                            timeout=30,
                        )
                        self.assertFalse(lectura.isError)
                        self.assertIn('"linea_inicio": 1', lectura.content[0].text)
                        self.assertIn('"linea_fin": 1', lectura.content[0].text)
                        self.assertIn("def login(): pass", lectura.content[0].text)
                log = (Path(temporal) / "logs" / "mcp-busqueda.log").read_text(encoding="utf-8")
                self.assertIn("indexar_proyecto: listo", log)
                self.assertIn("buscar_codigo: 1 resultados", log)
                self.assertIn("buscar_codigo: fallo (RuntimeError)", log)
                self.assertIn("leer_lineas: listo", log)
                self.assertNotIn("Primero ejecuta la herramienta indexar_proyecto", log)
                self.assertNotIn("consulta-privada-7f38", log)
                self.assertNotIn("def login(): pass", log)
                self.assertNotIn("auth.py", log)
                self.assertNotIn(str(proyecto), log)
            finally:
                servidor.shutdown()
                servidor.server_close()
                hilo.join()


if __name__ == "__main__":
    unittest.main()
