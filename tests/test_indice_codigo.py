import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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
            with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
                filas = conexion.execute(
                    """
                    SELECT f.ruta
                    FROM fragmentos_fts
                    JOIN fragmentos f ON f.rowid = fragmentos_fts.rowid
                    WHERE fragmentos_fts MATCH ?
                    """,
                    ('"login"',),
                ).fetchall()
            self.assertEqual({fila[0] for fila in filas}, {"auth.py"})
            self.assertEqual(self.indice.indexar()["actualizados"], 0)
            self.assertEqual(embedding.call_count, 3)
            (self.proyecto / "db.py").unlink()
            (self.proyecto / "auth.py").write_text("logout nuevo", encoding="utf-8")
            resumen = self.indice.indexar()
            self.assertEqual(resumen["actualizados"], 1)
            self.assertEqual(resumen["eliminados"], 1)
            with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
                antiguos = conexion.execute(
                    "SELECT rowid FROM fragmentos_fts WHERE fragmentos_fts MATCH ?",
                    ('"login"',),
                ).fetchall()
                nuevos = conexion.execute(
                    "SELECT rowid FROM fragmentos_fts WHERE fragmentos_fts MATCH ?",
                    ('"logout"',),
                ).fetchall()
            self.assertEqual(antiguos, [])
            self.assertEqual(len(nuevos), 1)
            with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
                conexion.execute(
                    "INSERT INTO fragmentos_fts(fragmentos_fts, rank) "
                    "VALUES ('integrity-check', 1)"
                )
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

    def test_busqueda_hibrida_fusiona_rangos_sin_duplicar(self):
        (self.proyecto / "symbols.py").write_text(
            "def exact_customer_lookup(): pass\n", encoding="utf-8"
        )
        (self.proyecto / "symbols-copy.py").write_text(
            "def exact_customer_lookup(): pass\n", encoding="utf-8"
        )
        (self.proyecto / "semantic.py").write_text(
            "def fetch_account(): pass\n", encoding="utf-8"
        )

        def generar(_cliente, texto):
            if texto == "exact_customer_lookup" or "fetch_account" in texto:
                return [1.0, 0.0]
            if "exact_customer_lookup" in texto:
                return [0.0, 1.0]
            return self.vector(None, texto)

        with patch.object(self.indice, "_embedding", side_effect=generar):
            self.indice.indexar()
            resultados = self.indice.buscar("exact_customer_lookup", limite=5)
            repetidos = self.indice.buscar("exact_customer_lookup", limite=5)

        self.assertEqual(resultados[0]["ruta"], "symbols.py")
        self.assertEqual(resultados[0]["similitud"], 0.0)
        self.assertEqual(
            set(resultados[0]),
            {"ruta", "linea_inicio", "linea_fin", "similitud", "texto"},
        )
        self.assertIn("semantic.py", {resultado["ruta"] for resultado in resultados})
        self.assertIn("symbols-copy.py", {resultado["ruta"] for resultado in resultados})
        self.assertEqual(resultados, repetidos)
        claves = [
            (resultado["ruta"], resultado["linea_inicio"], resultado["linea_fin"])
            for resultado in resultados
        ]
        self.assertEqual(len(claves), len(set(claves)))

    def test_busqueda_textual_encuentra_clases_variables_y_errores(self):
        (self.proyecto / "symbols.py").write_text(
            "class CustomerCache:\n"
            "    CACHE_MISS_ERROR = 'refresh token expired'\n",
            encoding="utf-8",
        )
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
            for consulta in ("CustomerCache", "CACHE_MISS_ERROR", "refresh token expired"):
                with self.subTest(consulta=consulta):
                    self.assertEqual(self.indice.buscar(consulta, 1)[0]["ruta"], "symbols.py")

    def test_consulta_sin_terminos_textuales_conserva_busqueda_vectorial(self):
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
            resultados = self.indice.buscar("!!!", limite=2)
        self.assertEqual(len(resultados), 2)

    def test_busqueda_textual_incluye_nombres_de_archivo(self):
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
            resultado = self.indice.buscar("auth", limite=1)
        self.assertEqual(resultado[0]["ruta"], "auth.py")

    def test_indice_existente_se_migra_sin_perder_vectores(self):
        self.indice.ruta.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
            conexion.executescript("""
                CREATE TABLE metadata (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
                CREATE TABLE archivos (ruta TEXT PRIMARY KEY, hash TEXT NOT NULL);
                CREATE TABLE fragmentos (
                    ruta TEXT NOT NULL, inicio INTEGER NOT NULL, fin INTEGER NOT NULL,
                    texto TEXT NOT NULL, vector TEXT NOT NULL
                );
            """)
            conexion.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    ("modelo", "modelo"),
                    ("prefijo_documento", ""),
                    ("prefijo_consulta", ""),
                ],
            )
            conexion.execute("INSERT INTO archivos VALUES (?, ?)", ("auth.py", "hash"))
            conexion.execute(
                "INSERT INTO fragmentos VALUES (?, ?, ?, ?, ?)",
                ("auth.py", 1, 1, "legacy_identifier()", json.dumps([1.0, 0.0])),
            )

        with patch.object(self.indice, "_embedding", return_value=[1.0, 0.0]) as embedding:
            resultado = self.indice.buscar("legacy_identifier", limite=1)
        self.assertEqual(resultado[0]["texto"], "legacy_identifier()")
        embedding.assert_called_once()
        for _ in range(3):
            with closing(self.indice._conexion()) as conexion:
                coincidencias = conexion.execute(
                    "SELECT COUNT(*) FROM fragmentos_fts WHERE fragmentos_fts MATCH ?",
                    ('"legacy_identifier"',),
                ).fetchone()[0]
            self.assertEqual(coincidencias, 1)

        reabierto = IndiceCodigo(
            self.proyecto, self.raiz / "indices", "http://localhost:8000", "modelo", ""
        )
        with patch.object(reabierto, "_embedding", return_value=[1.0, 0.0]):
            self.assertEqual(
                reabierto.buscar("legacy_identifier", 1)[0]["texto"],
                "legacy_identifier()",
            )
        with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
            vector = conexion.execute("SELECT vector FROM fragmentos").fetchone()[0]
        self.assertEqual(json.loads(vector), [1.0, 0.0])

    def test_migracion_interrumpida_revierte_solo_cambios_fts(self):
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
        with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
            conexion.execute("DROP TRIGGER fragmentos_fts_insert")
            conexion.execute("DROP TRIGGER fragmentos_fts_delete")
            conexion.execute("DROP TRIGGER fragmentos_fts_update")
            conexion.execute("DROP TABLE fragmentos_fts")
            originales = conexion.execute(
                "SELECT ruta, inicio, fin, texto, vector FROM fragmentos ORDER BY rowid"
            ).fetchall()

        class FalloDuranteRebuild:
            def __init__(self, conexion):
                self.conexion = conexion

            def execute(self, sentencia, parametros=()):
                if "VALUES ('rebuild')" in sentencia:
                    raise sqlite3.OperationalError("interrupción simulada")
                return self.conexion.execute(sentencia, parametros)

            def __getattr__(self, nombre):
                return getattr(self.conexion, nombre)

        conexion = sqlite3.connect(self.indice.ruta)
        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupción simulada"):
            IndiceCodigo._asegurar_fts5(FalloDuranteRebuild(conexion))
        conexion.close()

        with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
            self.assertEqual(
                conexion.execute(
                    "SELECT ruta, inicio, fin, texto, vector FROM fragmentos ORDER BY rowid"
                ).fetchall(),
                originales,
            )
            existe_fts = conexion.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'fragmentos_fts'"
            ).fetchone()
            cantidad_triggers = conexion.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE 'fragmentos_fts_%'"
            ).fetchone()[0]
        self.assertIsNone(existe_fts)
        self.assertEqual(cantidad_triggers, 0)

    def test_busqueda_y_reindexado_concurrentes(self):
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
            barrera = threading.Barrier(2)

            def reindexar():
                barrera.wait()
                for _ in range(4):
                    self.indice.indexar(reconstruir=True)

            def buscar():
                barrera.wait()
                for _ in range(8):
                    resultados = self.indice.buscar("login", limite=1)
                    if not resultados or resultados[0]["ruta"] != "auth.py":
                        raise AssertionError("La búsqueda perdió el resultado durante el reindexado.")

            with ThreadPoolExecutor(max_workers=2) as ejecutor:
                escritura = ejecutor.submit(reindexar)
                lectura = ejecutor.submit(buscar)
                escritura.result(timeout=30)
                lectura.result(timeout=30)
        with closing(sqlite3.connect(self.indice.ruta)) as conexion, conexion:
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conexion.execute(
                "INSERT INTO fragmentos_fts(fragmentos_fts, rank) "
                "VALUES ('integrity-check', 1)"
            )

    def test_lee_lineas_acotadas_de_archivo_indexado(self):
        texto = "\n".join(f"linea {numero}" for numero in range(1, 102)) + "\n"
        (self.proyecto / "lineas.py").write_text(texto, encoding="utf-8")
        (self.proyecto / "vacio.py").write_text("", encoding="utf-8")
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()

        inicial = self.indice.leer_lineas("lineas.py", 1, margen=2)
        final = self.indice.leer_lineas("lineas.py", 101, margen=2)
        self.assertEqual((inicial["linea_inicio"], inicial["linea_fin"]), (1, 3))
        self.assertEqual((final["linea_inicio"], final["linea_fin"]), (99, 101))
        resultado = self.indice.leer_lineas("lineas.py", 25, margen=2)
        self.assertEqual(
            (resultado["ruta"], resultado["linea_inicio"], resultado["linea_fin"]),
            ("lineas.py", 23, 27),
        )
        self.assertEqual(resultado["contenido"], "\n".join(f"linea {n}" for n in range(23, 28)))
        self.assertFalse(resultado["truncado"])
        maximo = self.indice.leer_lineas("lineas.py", 50, margen=30)
        self.assertEqual(
            (maximo["linea_inicio"], maximo["linea_fin"]),
            (20, 80),
        )
        self.assertEqual(maximo["linea_fin"] - maximo["linea_inicio"] + 1, 61)
        with self.assertRaises(ValueError):
            self.indice.leer_lineas("vacio.py", 1)

    def test_lectura_rechaza_rutas_y_limites_invalidos(self):
        (self.proyecto / "visible.py").write_text("contenido\n", encoding="utf-8")
        fuera = self.raiz / "fuera.py"
        fuera.write_text("secreto\n", encoding="utf-8")
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()

        for ruta in ("../fuera.py", str(fuera), ".env"):
            with self.subTest(ruta=ruta), self.assertRaises(ValueError):
                self.indice.leer_lineas(ruta, 1)
        with self.assertRaises(FileNotFoundError):
            self.indice.leer_lineas("inexistente.py", 1)
        for linea, margen in ((0, 0), (True, 0), (1, -1), (1, 31), (1, True)):
            with self.subTest(linea=linea, margen=margen), self.assertRaises(ValueError):
                self.indice.leer_lineas("visible.py", linea, margen)
        with self.assertRaises(ValueError):
            self.indice.leer_lineas("visible.py", 2)

        for nombre, destino in (("escape.py", fuera), ("enlace-interno.py", self.proyecto / "visible.py")):
            enlace = self.proyecto / nombre
            try:
                enlace.symlink_to(destino)
            except (OSError, NotImplementedError):
                continue
            with self.subTest(enlace=nombre), self.assertRaises(ValueError):
                self.indice.leer_lineas(nombre, 1)

    def test_lectura_trunca_por_limite_de_caracteres(self):
        (self.proyecto / "grande.py").write_text("x" * 9000, encoding="utf-8")
        with patch.object(self.indice, "_embedding", side_effect=self.vector):
            self.indice.indexar()
        resultado = self.indice.leer_lineas("grande.py", 1, margen=0)
        self.assertEqual(len(resultado["contenido"]), 8000)
        self.assertTrue(resultado["truncado"])

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
