import unittest
from unittest.mock import patch

from app.servicios.motor_inferencia import MotorInferencia


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


if __name__ == "__main__":
    unittest.main()
