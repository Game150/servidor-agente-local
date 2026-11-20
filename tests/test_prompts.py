import unittest
from pathlib import Path

import yaml
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

    def test_forzar_parametros_solo_impide_override_de_campos_seleccionados(self):
        agente = {
            "id": "agente",
            "nombre": "Agente",
            "descripcion": "Prueba",
            "modelo": "modelo",
            "parametros": {
                "max_tokens": 8192,
                "reasoning_budget_tokens": 1024,
            },
            "forzar_parametros": ["reasoning_budget_tokens"],
        }
        validado = _validar_agente(agente, "agente.yaml")
        payload = construir_payload(validado, {
            "messages": [{"role": "user", "content": "Hola"}],
            "max_tokens": 2048,
            "reasoning_budget_tokens": 512,
        })

        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["reasoning_budget_tokens"], 1024)

    def test_forzar_parametros_rechaza_nombres_desconocidos_y_duplicados(self):
        agente = {
            "id": "agente",
            "nombre": "Agente",
            "descripcion": "Prueba",
            "modelo": "modelo",
            "parametros": {"reasoning_budget_tokens": 1024},
        }
        for valores in (["inexistente"], ["reasoning_budget_tokens"] * 2, "reasoning_budget_tokens"):
            with self.subTest(valores=valores), self.assertRaisesRegex(ValueError, "forzar_parametros"):
                _validar_agente({**agente, "forzar_parametros": valores}, "agente.yaml")

    def test_reasoning_budget_tokens_rechaza_valores_invalidos(self):
        agente = {
            "id": "agente",
            "nombre": "Agente",
            "descripcion": "Prueba",
            "modelo": "modelo",
        }
        for valor in (-2, 1.5, True, "1024"):
            with self.subTest(valor=valor), self.assertRaisesRegex(ValueError, "reasoning_budget_tokens"):
                _validar_agente({
                    **agente, "parametros": {"reasoning_budget_tokens": valor},
                }, "agente.yaml")

    def test_plantilla_completa_de_agente_es_configuracion_valida(self):
        ruta = Path(__file__).resolve().parents[1] / "config" / "agentes" / "ejemplo.yaml.example"
        with ruta.open(encoding="utf-8") as archivo:
            configuracion = yaml.safe_load(archivo)

        agente = _validar_agente(configuracion, ruta.name)

        self.assertEqual(agente["perfil_prompt"], "desarrollo")
        self.assertEqual(agente["parametros"]["reasoning_budget_tokens"], 1024)
        self.assertEqual(agente["forzar_parametros"], ["reasoning_budget_tokens"])
        self.assertEqual(agente["motor"]["ngram_mod"], False)


if __name__ == "__main__":
    unittest.main()
