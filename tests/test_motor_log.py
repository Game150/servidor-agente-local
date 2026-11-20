import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.nucleo.config import BASE_DIR, resolver_llama_server
from app.servicios.motor_inferencia import (
    MotorInferencia,
    construir_argumentos,
    huella_de,
    verificar_soporte_ngram_mod,
)
from app.servicios.registro_agentes import _validar_motor


class LogDeCargaTests(unittest.TestCase):
    def test_llama_backend_selecciona_vulkan_o_rocm_por_nombre_de_carpeta(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            vulkan = raiz / "llama-b1-win-vulkan" / "llama-server.exe"
            rocm = raiz / "llama-b2-win-rocm-gfx103x" / "llama-server.exe"
            vulkan.parent.mkdir()
            rocm.parent.mkdir()
            vulkan.touch()
            rocm.touch()

            with patch("app.nucleo.config.BASE_DIR", raiz), patch.dict(
                os.environ,
                {"LLAMA_SERVER_PATH": "", "LLAMA_BACKEND": "vulkan"},
            ), patch("app.nucleo.config.shutil.which", return_value=None):
                self.assertEqual(resolver_llama_server(), vulkan)

            with patch("app.nucleo.config.BASE_DIR", raiz), patch.dict(
                os.environ,
                {"LLAMA_SERVER_PATH": "", "LLAMA_BACKEND": "rocm"},
            ), patch("app.nucleo.config.shutil.which", return_value=None):
                self.assertEqual(resolver_llama_server(), rocm)

    def test_llama_backend_auto_conserva_seleccion_mas_reciente(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            vulkan = raiz / "llama-vulkan" / "llama-server.exe"
            rocm = raiz / "llama-rocm" / "llama-server.exe"
            vulkan.parent.mkdir()
            rocm.parent.mkdir()
            vulkan.touch()
            rocm.touch()
            os.utime(vulkan, (1, 1))

            with patch("app.nucleo.config.BASE_DIR", raiz), patch.dict(
                os.environ,
                {"LLAMA_SERVER_PATH": "", "LLAMA_BACKEND": "auto"},
            ), patch("app.nucleo.config.shutil.which", return_value=None):
                self.assertEqual(resolver_llama_server(), rocm)

    def test_llama_backend_invalido_o_inexistente_falla_explicito(self):
        with tempfile.TemporaryDirectory() as temporal:
            with patch("app.nucleo.config.BASE_DIR", Path(temporal)), patch.dict(
                os.environ,
                {"LLAMA_SERVER_PATH": "", "LLAMA_BACKEND": "directml"},
            ):
                with self.assertRaisesRegex(ValueError, "LLAMA_BACKEND"):
                    resolver_llama_server()

            with patch("app.nucleo.config.BASE_DIR", Path(temporal)), patch.dict(
                os.environ,
                {"LLAMA_SERVER_PATH": "", "LLAMA_BACKEND": "rocm"},
            ):
                with self.assertRaisesRegex(FileNotFoundError, "LLAMA_BACKEND=rocm"):
                    resolver_llama_server()

    def test_llama_server_path_explicito_tiene_prioridad_sobre_backend(self):
        with tempfile.TemporaryDirectory() as temporal:
            raiz = Path(temporal)
            binario = raiz / "custom" / "llama-server.exe"
            binario.parent.mkdir()
            binario.touch()

            with patch("app.nucleo.config.BASE_DIR", raiz), patch.dict(
                os.environ,
                {
                    "LLAMA_SERVER_PATH": str(binario),
                    "LLAMA_BACKEND": "vulkan",
                },
            ):
                self.assertEqual(resolver_llama_server(), binario)

    def test_asegurar_registra_modelo_cargado(self):
        agente = {
            "id": "demo",
            "modelo": "modelo-demo",
            "motor": {"ctx_size": 4096},
        }
        motor = MotorInferencia()

        with patch.object(motor, "_lanzar"), patch.object(
            motor, "_esperar_listo"
        ), patch.object(motor, "estado", return_value={}):
            with self.assertLogs("uvicorn.error", level="INFO") as registro:
                motor.asegurar(agente)

        salida = "\n".join(registro.output)
        self.assertIn("Cargando modelo modelo-demo (agente=demo, ctx=4096)", salida)
        self.assertIn("Modelo cargado: modelo-demo (agente=demo,", salida)

    def test_batch_size_configurado_llega_al_motor_y_cambia_huella(self):
        anterior = _validar_motor({}, "agente.yaml")
        configurado = _validar_motor({"batch_size": 1024}, "agente.yaml")
        argumentos = construir_argumentos(Path("llama-server"), Path("modelo.gguf"), configurado)
        indice = argumentos.index("--batch-size")
        self.assertEqual(argumentos[indice + 1], "1024")
        self.assertNotIn("--batch-size", construir_argumentos(Path("llama-server"), Path("modelo.gguf"), anterior))
        self.assertNotEqual(
            huella_de({"modelo": "modelo", "motor": anterior}),
            huella_de({"modelo": "modelo", "motor": configurado}),
        )

    def test_batch_size_invalido_se_rechaza(self):
        for valor in (0, -1, True, 1.5, "512"):
            with self.subTest(valor=valor), self.assertRaisesRegex(ValueError, "motor.batch_size"):
                _validar_motor({"batch_size": valor}, "agente.yaml")

    def test_gpu_layers_auto_se_valida_y_se_envia_al_motor(self):
        motor = _validar_motor({"gpu_layers": "auto"}, "agente.yaml")
        argumentos = construir_argumentos(Path("llama-server"), Path("modelo.gguf"), motor)
        self.assertEqual(argumentos[argumentos.index("-ngl") + 1], "auto")

        for valor in ("all", "automático"):
            with self.subTest(valor=valor), self.assertRaisesRegex(ValueError, "gpu_layers"):
                _validar_motor({"gpu_layers": valor}, "agente.yaml")

    def test_embeddings_usa_puerto_y_modo_independientes(self):
        argumentos = construir_argumentos(
            Path("llama-server"),
            Path("embeddings.gguf"),
            {
                "gpu_layers": 0,
                "ctx_size": 2048,
                "parallel": 1,
                "cache_type_k": "q8_0",
                "cache_type_v": "f16",
            },
            host="127.0.0.1",
            port=8081,
            embedding=True,
        )

        self.assertIn("--embedding", argumentos)
        self.assertEqual(argumentos[argumentos.index("--port") + 1], "8081")
        self.assertEqual(argumentos[argumentos.index("-m") + 1], "embeddings.gguf")

    def test_ubatch_size_se_envia_y_cambia_huella(self):
        anterior = _validar_motor({"batch_size": 1024}, "agente.yaml")
        configurado = _validar_motor({"batch_size": 1024, "ubatch_size": 256}, "agente.yaml")
        argumentos = construir_argumentos(Path("llama-server"), Path("modelo.gguf"), configurado)
        indice = argumentos.index("--ubatch-size")
        self.assertEqual(argumentos[indice + 1], "256")
        self.assertNotIn("--ubatch-size", construir_argumentos(Path("llama-server"), Path("modelo.gguf"), anterior))
        self.assertNotEqual(
            huella_de({"modelo": "modelo", "motor": anterior}),
            huella_de({"modelo": "modelo", "motor": configurado}),
        )

    def test_ruta_relativa_mmproj_se_resuelve_desde_la_raiz_del_proyecto(self):
        motor = _validar_motor(
            {"extra_args": ["--mmproj", "proyectores/mmproj-F16.gguf"]},
            "agente.yaml",
        )
        argumentos = construir_argumentos(
            Path("llama-server"),
            Path("modelo.gguf"),
            motor,
        )
        indice = argumentos.index("--mmproj")
        self.assertEqual(
            argumentos[indice + 1],
            str(BASE_DIR / "proyectores/mmproj-F16.gguf"),
        )

    def test_ubatch_size_invalido_se_rechaza(self):
        for valor in (0, -1, True, 1.5, "512"):
            with self.subTest(valor=valor), self.assertRaisesRegex(ValueError, "motor.ubatch_size"):
                _validar_motor({"ubatch_size": valor}, "agente.yaml")

    def test_ngram_mod_desactivado_no_cambia_argumentos(self):
        motor = _validar_motor({}, "agente.yaml")
        self.assertFalse(motor["ngram_mod"])
        self.assertEqual(
            construir_argumentos(Path("llama-server"), Path("modelo.gguf"), motor),
            [
                "llama-server", "-m", "modelo.gguf", "-ngl", "99", "-c", "8192",
                "-np", "1", "-ctk", "q8_0", "-ctv", "q8_0",
                "--host", "127.0.0.1", "--port", "8080",
            ],
        )

    def test_ngram_mod_usa_opciones_y_parametros_configurados(self):
        motor = _validar_motor(
            {
                "ngram_mod": True,
                "ngram_mod_n_match": 18,
                "ngram_mod_n_min": 12,
                "ngram_mod_n_max": 36,
            },
            "agente.yaml",
        )
        argumentos = construir_argumentos(
            Path("llama-server"), Path("modelo.gguf"), motor
        )

        self.assertEqual(argumentos[argumentos.index("--spec-type") + 1], "ngram-mod")
        self.assertEqual(
            argumentos[argumentos.index("--spec-ngram-mod-n-match") + 1], "18"
        )
        self.assertEqual(
            argumentos[argumentos.index("--spec-ngram-mod-n-min") + 1], "12"
        )
        self.assertEqual(
            argumentos[argumentos.index("--spec-ngram-mod-n-max") + 1], "36"
        )

    def test_ngram_mod_respeta_opciones_existentes_en_extra_args(self):
        motor = _validar_motor(
            {
                "ngram_mod": True,
                "extra_args": [
                    "--spec-type", "ngram-mod",
                    "--spec-ngram-mod-n-match=20",
                    "--spec-ngram-mod-n-min", "10",
                    "--spec-ngram-mod-n-max", "30",
                ],
            },
            "agente.yaml",
        )
        argumentos = construir_argumentos(
            Path("llama-server"), Path("modelo.gguf"), motor
        )

        for opcion in (
            "--spec-type",
            "--spec-ngram-mod-n-match",
            "--spec-ngram-mod-n-min",
            "--spec-ngram-mod-n-max",
        ):
            self.assertEqual(
                sum(
                    argumento == opcion or argumento.startswith(f"{opcion}=")
                    for argumento in argumentos
                ),
                1,
            )

    def test_ngram_mod_rechaza_parametros_invalidos(self):
        casos = (
            ({"ngram_mod": "true"}, "motor.ngram_mod"),
            ({"ngram_mod_n_match": 0}, "motor.ngram_mod_n_match"),
            ({"ngram_mod_n_min": True}, "motor.ngram_mod_n_min"),
            (
                {"ngram_mod_n_min": 65, "ngram_mod_n_max": 64},
                "ngram_mod_n_min",
            ),
        )
        for configuracion, mensaje in casos:
            with self.subTest(configuracion=configuracion):
                with self.assertRaisesRegex(ValueError, mensaje):
                    _validar_motor(configuracion, "agente.yaml")

    def test_ngram_mod_rechaza_tipo_incompatible_en_extra_args(self):
        motor = _validar_motor(
            {"ngram_mod": True, "extra_args": ["--spec-type", "ngram-simple"]},
            "agente.yaml",
        )
        with self.assertRaisesRegex(ValueError, "ngram-mod"):
            construir_argumentos(Path("llama-server"), Path("modelo.gguf"), motor)

    def test_ngram_mod_verifica_soporte_del_ejecutable(self):
        salida_ayuda = SimpleNamespace(
            stdout=(
                "--spec-type none,ngram-mod\n"
                "--spec-ngram-mod-n-match N\n"
                "--spec-ngram-mod-n-min N\n"
                "--spec-ngram-mod-n-max N\n"
            ),
            stderr="",
            returncode=0,
        )
        with patch(
            "app.servicios.motor_inferencia.subprocess.run",
            return_value=salida_ayuda,
        ) as ejecutar:
            verificar_soporte_ngram_mod(Path("llama-server"))
        ejecutar.assert_called_once()

        salida_antigua = SimpleNamespace(
            stdout="--spec-type none\n",
            stderr="",
            returncode=0,
        )
        with patch(
            "app.servicios.motor_inferencia.subprocess.run",
            return_value=salida_antigua,
        ):
            with self.assertRaisesRegex(RuntimeError, "Actualiza llama.cpp"):
                verificar_soporte_ngram_mod(Path("llama-server"))

    def test_arranque_ngram_mod_falla_antes_de_lanzar_si_no_hay_soporte(self):
        binario = Path("llama-server")
        motor = MotorInferencia()
        agente = {
            "id": "ngram",
            "modelo": "modelo",
            "motor": {"ngram_mod": True},
        }
        with patch(
            "app.servicios.motor_inferencia.resolver_llama_server",
            return_value=binario,
        ), patch(
            "app.servicios.motor_inferencia.buscar_modelo",
            return_value={"ruta": "modelo.gguf"},
        ), patch(
            "app.servicios.motor_inferencia.verificar_soporte_ngram_mod",
            side_effect=RuntimeError("llama.cpp sin soporte"),
        ) as verificar:
            with self.assertRaisesRegex(RuntimeError, "sin soporte"):
                motor._lanzar(agente)

        verificar.assert_called_once_with(binario)

    def test_cambiar_ngram_mod_cambia_huella_y_reinicia_motor(self):
        agente_base = {
            "id": "base",
            "modelo": "modelo",
            "motor": _validar_motor({}, "agente.yaml"),
        }
        agente_ngram = {
            "id": "ngram",
            "modelo": "modelo",
            "motor": _validar_motor({"ngram_mod": True}, "agente.yaml"),
        }
        self.assertNotEqual(huella_de(agente_base), huella_de(agente_ngram))

        class ProcesoFalso:
            codigo = None

            def poll(self):
                return self.codigo

            def terminate(self):
                self.codigo = 0

            def wait(self, timeout=None):
                self.codigo = 0

            def kill(self):
                self.codigo = -1

        motor = MotorInferencia()
        lanzamientos = []

        def lanzar(agente):
            lanzamientos.append(agente["id"])
            with motor._mu:
                motor._proceso = ProcesoFalso()
                motor._modelo = agente["modelo"]
                motor._huella = huella_de(agente)
                motor._listo = False

        with patch.object(motor, "_lanzar", side_effect=lanzar), patch.object(
            motor, "_esperar_listo"
        ):
            motor.asegurar(agente_base)
            motor.asegurar(agente_ngram)

        self.assertEqual(lanzamientos, ["base", "ngram"])


if __name__ == "__main__":
    unittest.main()
