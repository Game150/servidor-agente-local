import unittest

from app.servicios.payload import construir_payload
from app.servicios.prompts import INSTRUCCIONES_GLOBALES, PERFILES_PROMPT
from app.servicios.registro_agentes import _validar_agente


class PromptTests(unittest.TestCase):
    def test_payload_combines_global_profile_agent_and_client_instructions(self):
        agente = {
            "perfil_prompt": "trabajo",
            "system_prompt": "Instrucción propia.",
            "parametros": {},
        }
        solicitud = {
            "messages": [
                {"role": "system", "content": "Regla del cliente."},
                {"role": "developer", "content": "Otra regla del cliente."},
                {"role": "user", "content": "Hola"},
            ]
        }

        payload = construir_payload(agente, solicitud)
        sistema = payload["messages"][0]["content"]

        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(
            [mensaje["role"] for mensaje in payload["messages"]],
            ["system", "user"],
        )
        self.assertLess(
            sistema.index(INSTRUCCIONES_GLOBALES.strip()),
            sistema.index(PERFILES_PROMPT["trabajo"].strip()),
        )
        self.assertLess(
            sistema.index(PERFILES_PROMPT["trabajo"].strip()),
            sistema.index("Instrucción propia."),
        )
        self.assertLess(sistema.index("Instrucción propia."), sistema.index("Regla del cliente."))
        self.assertIn("Otra regla del cliente.", sistema)

    def test_unknown_profile_is_rejected(self):
        agente = {
            "id": "agente",
            "nombre": "Agente",
            "descripcion": "Prueba",
            "modelo": "modelo",
            "perfil_prompt": "desconocido",
        }

        with self.assertRaisesRegex(ValueError, "perfil_prompt"):
            _validar_agente(agente, "agente.yaml")

    def test_agent_without_profile_remains_compatible(self):
        agente = {
            "id": "agente",
            "nombre": "Agente",
            "descripcion": "Prueba",
            "modelo": "modelo",
            "system_prompt": "Prompt anterior.",
        }

        validado = _validar_agente(agente, "agente.yaml")

        self.assertIsNone(validado["perfil_prompt"])
        self.assertEqual(validado["system_prompt"], "Prompt anterior.")


if __name__ == "__main__":
    unittest.main()
