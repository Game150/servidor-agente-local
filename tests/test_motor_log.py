import unittest
from pathlib import Path
from unittest.mock import patch

from app.nucleo.config import BASE_DIR
from app.servicios.motor_inferencia import MotorInferencia, construir_argumentos, huella_de
from app.servicios.registro_agentes import _validar_motor


class LogDeCargaTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
