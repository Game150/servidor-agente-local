import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from app.nucleo.config import (
    LLAMA_HOST,
    LLAMA_PORT,
    LLAMA_URL,
    LOG_DIR,
    LOG_MAX_BYTES,
    STARTUP_TIMEOUT,
    resolver_llama_server,
)
from app.servicios.registro_modelos import buscar_modelo

logger = logging.getLogger("uvicorn.error")

# Opener sin proxy: nunca hay que pasar por un proxy para hablar con localhost.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def huella_de(agente: dict) -> str:
    """Identifica con qué modelo y qué configuración corre llama-server.

    Si dos agentes comparten huella pueden compartir el mismo proceso; si
    cambia (otro modelo o, por ejemplo, otro ctx_size) hay que reiniciarlo.
    """
    return json.dumps([agente["modelo"], agente["motor"]], sort_keys=True)


def construir_argumentos(binario: Path, ruta_modelo: Path, motor: dict) -> list[str]:
    argumentos = [
        str(binario),
        "-m", str(ruta_modelo),
        "-ngl", str(motor["gpu_layers"]),
        # Ojo: llama-server reparte el contexto entre los slots paralelos
        # (contexto por slot = ctx_size / parallel).
        "-c", str(motor["ctx_size"]),
        "-np", str(motor["parallel"]),
        "-ctk", motor["cache_type_k"],
        "-ctv", motor["cache_type_v"],
        "--host", LLAMA_HOST,
        "--port", str(LLAMA_PORT),
    ]

    if motor.get("flash_attn"):
        argumentos += ["-fa", motor["flash_attn"]]

    if motor.get("threads"):
        argumentos += ["-t", str(motor["threads"])]

    argumentos += list(motor.get("extra_args", []))

    return argumentos


def _opciones_proceso() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}

    return {"start_new_session": True}


def _puerto_ocupado() -> bool:
    try:
        with socket.create_connection((LLAMA_HOST, LLAMA_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _health_ok() -> bool:
    try:
        with _OPENER.open(f"{LLAMA_URL}/health", timeout=2) as respuesta:
            return respuesta.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        # Incluye el 503 que llama-server devuelve mientras carga el modelo.
        return False


class MotorInferencia:
    """Controla un único proceso llama-server.

    Hay dos candados con funciones distintas:
      - `_op` serializa arranques (que pueden tardar minutos).
      - `_mu` protege el estado y solo se toma por instantes, así que
        consultar el estado nunca se bloquea mientras carga un modelo.
    """

    def __init__(self) -> None:
        self._op = threading.Lock()
        self._mu = threading.Lock()
        self._proceso: subprocess.Popen | None = None
        self._modelo: str | None = None
        self._huella: str | None = None
        self._listo = False

    # -- Estado -------------------------------------------------------------

    def _refrescar(self) -> None:
        """Descarta el proceso si murió. Requiere tener `_mu`."""
        if self._proceso is not None and self._proceso.poll() is not None:
            self._proceso = None
            self._modelo = None
            self._huella = None
            self._listo = False

    def estado(self) -> dict:
        with self._mu:
            self._refrescar()

            if self._proceso is None:
                nombre = "detenido"
            else:
                nombre = "listo" if self._listo else "iniciando"

            return {
                "ejecutandose": self._proceso is not None,
                "estado": nombre,
                "modelo_activo": self._modelo,
                "puerto": LLAMA_PORT,
            }

    def es_compatible(self, agente: dict) -> bool:
        """True si ya hay un llama-server listo con la configuración del agente."""
        huella = huella_de(agente)

        with self._mu:
            self._refrescar()

            return (
                self._proceso is not None
                and self._listo
                and self._huella == huella
            )

    # -- Arranque -----------------------------------------------------------

    def asegurar(self, agente: dict) -> dict:
        """Deja corriendo llama-server con la configuración del agente.

        Si ya está así no hace nada; si hay otro modelo (o la misma pero con
        otra configuración) lo reinicia. Bloquea hasta que el modelo termina
        de cargar, por lo que debe llamarse fuera del bucle de eventos.
        """
        with self._op:
            if self.es_compatible(agente):
                return {
                    "mensaje": "El modelo ya está ejecutándose.",
                    **self.estado(),
                }

            self._detener_proceso()
            inicio = time.monotonic()
            logger.info(
                "Cargando modelo %s (agente=%s, ctx=%s)",
                agente["modelo"],
                agente["id"],
                agente["motor"].get("ctx_size"),
            )
            self._lanzar(agente)
            self._esperar_listo()
            logger.info(
                "Modelo cargado: %s (agente=%s, %.1f s)",
                agente["modelo"],
                agente["id"],
                time.monotonic() - inicio,
            )

            return {
                "mensaje": "Motor de inferencia iniciado.",
                **self.estado(),
            }

    def _lanzar(self, agente: dict) -> None:
        binario = resolver_llama_server()

        if binario is None:
            raise FileNotFoundError(
                "No se encuentra llama-server. Definí LLAMA_SERVER_PATH en "
                "el .env o extraé llama.cpp en una carpeta 'llama*' dentro "
                "del proyecto."
            )

        modelo = buscar_modelo(agente["modelo"])

        if modelo is None:
            raise FileNotFoundError(
                f"No se encuentra el modelo '{agente['modelo']}' en el "
                "directorio de modelos."
            )

        if _puerto_ocupado():
            raise RuntimeError(
                f"El puerto {LLAMA_PORT} ya está en uso. ¿Quedó un "
                "llama-server anterior abierto? Cerralo o cambiá LLAMA_PORT."
            )

        argumentos = construir_argumentos(
            binario, Path(modelo["ruta"]), agente["motor"]
        )

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ruta_log = LOG_DIR / "llama-server.log"

        if ruta_log.exists() and ruta_log.stat().st_size > LOG_MAX_BYTES:
            ruta_log.replace(ruta_log.with_name(ruta_log.name + ".1"))

        with ruta_log.open("a", encoding="utf-8") as registro:
            registro.write(
                f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                f"agente={agente['id']} ===\n{' '.join(argumentos)}\n"
            )
            registro.flush()

            proceso = subprocess.Popen(
                argumentos,
                cwd=str(binario.parent),
                stdin=subprocess.DEVNULL,
                stdout=registro,
                stderr=subprocess.STDOUT,
                **_opciones_proceso(),
            )

        with self._mu:
            self._proceso = proceso
            self._modelo = agente["modelo"]
            self._huella = huella_de(agente)
            self._listo = False

    def _esperar_listo(self) -> None:
        limite = time.monotonic() + STARTUP_TIMEOUT

        while time.monotonic() < limite:
            with self._mu:
                proceso = self._proceso

            if proceso is None:
                raise RuntimeError(
                    "El motor se detuvo mientras se estaba iniciando."
                )

            codigo = proceso.poll()

            if codigo is not None:
                self._detener_proceso()
                raise RuntimeError(
                    f"llama-server terminó al arrancar (código {codigo}). "
                    f"Últimas líneas del log:\n{self._cola_log()}"
                )

            if _health_ok():
                with self._mu:
                    self._listo = True
                return

            time.sleep(0.5)

        self._detener_proceso()
        raise RuntimeError(
            f"Pasaron {STARTUP_TIMEOUT} s y el modelo no terminó de cargar. "
            f"Últimas líneas del log:\n{self._cola_log()}"
        )

    @staticmethod
    def _cola_log(lineas: int = 15) -> str:
        ruta = LOG_DIR / "llama-server.log"

        try:
            with ruta.open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 8192))
                texto = f.read().decode("utf-8", errors="replace")
        except OSError:
            return "(no hay log)"

        return "\n".join(texto.splitlines()[-lineas:])

    # -- Parada -------------------------------------------------------------

    def _detener_proceso(self) -> bool:
        """Mata el proceso actual si lo hay. Devuelve True si había uno vivo."""
        with self._mu:
            proceso = self._proceso
            self._proceso = None
            self._modelo = None
            self._huella = None
            self._listo = False

        if proceso is None or proceso.poll() is not None:
            return False

        proceso.terminate()

        try:
            proceso.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proceso.kill()
            proceso.wait()

        return True

    def detener(self) -> dict:
        """Detiene el motor. No espera a `_op`: puede cancelar un arranque."""
        habia = self._detener_proceso()

        if habia:
            logger.info("Motor detenido por solicitud.")

        return {
            "mensaje": (
                "Motor de inferencia detenido."
                if habia
                else "El motor ya está detenido."
            ),
            **self.estado(),
        }


motor = MotorInferencia()
