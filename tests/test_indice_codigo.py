import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.servicios.indice_codigo import IndiceCodigo


class IndiceCodigoTests(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.raiz = Path(self.temporal.name)
        self.proyecto = self.raiz / "proyecto"
        self.proyecto.mkdir()
        subprocess.run(["git", "init", "-q", str(self.proyecto)], check=True)
        (self.proyecto / ".gitignore").write_text("ignorado.py\n", encoding="utf-8")
        (self.proyecto / ".env").write_text("API_KEY=secreto", encoding="utf-8")
        (self.proyecto / "ignorado.py").write_text("ignorado", encoding="utf-8")
        (self.proyecto / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
        (self.proyecto / "db.py").write_text("def database():\n    return True\n", encoding="utf-8")
        self.indice = IndiceCodigo(
            self.proyecto, self.raiz / "indices", "http://localhost:8000", "modelo", ""
        )

    @staticmethod
    def vector(_cliente, texto):
        return [1.0, 0.0] if "login" in texto else [0.0, 1.0]

    def test_indexacion_incremental_busqueda_y_eliminacion(self):
        with patch.object(self.indice, "_embedding", side_effect=self.vector) as embedding:
            resumen = self.indice.indexar()
            self.assertEqual(resumen["archivos"], 2)
            self.assertEqual(resumen["fragmentos"], 2)
            self.assertEqual(embedding.call_count, 2)
            resultados = self.indice.buscar("login", limite=1)
            self.assertEqual(resultados[0]["ruta"], "auth.py")
            self.assertEqual(resultados[0]["linea_inicio"], 1)
            self.assertEqual(resultados[0]["linea_fin"], 2)
            self.assertEqual(self.indice.indexar()["actualizados"], 0)
            self.assertEqual(embedding.call_count, 3)
            (self.proyecto / "db.py").unlink()
            (self.proyecto / "auth.py").write_text("login nuevo", encoding="utf-8")
            resumen = self.indice.indexar()
            self.assertEqual(resumen["actualizados"], 1)
            self.assertEqual(resumen["eliminados"], 1)
            self.assertEqual(self.indice.indexar(reconstruir=True)["actualizados"], 1)

    def test_fallo_de_embeddings_conserva_indice_anterior(self):
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
        (self.proyecto / "auth.py").write_text("login cambiado", encoding="utf-8")
        with patch.object(self.indice, "_embedding", side_effect=RuntimeError("fallo")):
            with self.assertRaisesRegex(RuntimeError, "fallo"):
                self.indice.indexar()
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.assertIn("return True", self.indice.buscar("login", 1)[0]["texto"])

    def test_requiere_indice_y_valida_consulta(self):
        with self.assertRaisesRegex(RuntimeError, "indexar_proyecto"):
            self.indice.buscar("login")
        for consulta, limite in [("", 1), ("x", 6), ("x" * 1001, 1)]:
            with self.subTest(consulta=consulta, limite=limite):
                with self.assertRaises(ValueError):
                    self.indice.buscar(consulta, limite)

    def test_prefijos_se_usan_y_su_cambio_exige_reindexar(self):
        self.indice.prefijo_documento = "search_document: "
        self.indice.prefijo_consulta = "search_query: "
        textos = []

        def generar(_cliente, texto):
            textos.append(texto)
            return [1.0, 0.0]

        with patch.object(self.indice, "_embedding", side_effect=generar):
            self.indice.indexar()
            self.indice.buscar("login")
            self.assertTrue(all(t.startswith("search_document: ") for t in textos[:-1]))
            self.assertEqual(textos[-1], "search_query: login")
            self.indice.prefijo_documento = "otro: "
            with self.assertRaisesRegex(RuntimeError, "indexar_proyecto"):
                self.indice.buscar("login")
            self.assertEqual(self.indice.indexar()["actualizados"], 2)

    def test_peticion_embeddings_usa_api_y_autorizacion(self):
        peticiones = []

        def responder(peticion):
            peticiones.append(peticion)
            return httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0]}]})

        self.indice.clave = "clave-local"
        with httpx.Client(transport=httpx.MockTransport(responder)) as cliente:
            self.assertEqual(self.indice._embedding(cliente, "hola"), [0.6, 0.8])
        self.assertEqual(str(peticiones[0].url), "http://localhost:8000/v1/embeddings")
        self.assertEqual(peticiones[0].headers["authorization"], "Bearer clave-local")
        self.assertEqual(json.loads(peticiones[0].content)["model"], "modelo")

    def test_modelo_directo_no_cambia_identidad_del_indice(self):
        modelos = []

        def responder(peticion):
            modelos.append(json.loads(peticion.content)["model"])
            return httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0]}]})

        self.indice.modelo_peticion = "local"
        with httpx.Client(transport=httpx.MockTransport(responder)) as cliente:
            self.assertEqual(self.indice._embedding(cliente, "hola"), [0.6, 0.8])
        self.assertEqual(modelos, ["local"])
        self.assertEqual(self.indice.modelo, "modelo")

    def test_error_de_embeddings_se_comunica(self):
        def responder(_peticion):
            return httpx.Response(503, json={"error": {"message": "sin modelo"}})

        with httpx.Client(transport=httpx.MockTransport(responder)) as cliente:
            with self.assertRaisesRegex(RuntimeError, "503.*sin modelo"):
                self.indice._embedding(cliente, "hola")

    def test_fragmentos_largos_conservan_numeros_de_linea(self):
        fragmentos = list(self.indice._fragmentos("a" * 1001 + "\nfinal\n"))
        self.assertEqual([(a, b) for a, b, _ in fragmentos], [(1, 1), (1, 2)])
        self.assertEqual("".join(texto for _, _, texto in fragmentos), "a" * 1001 + "\nfinal\n")


if __name__ == "__main__":
    unittest.main()
