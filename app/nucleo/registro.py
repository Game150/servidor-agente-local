"""Contexto de logging por solicitud: id corto y hora de inicio."""

import logging
from contextvars import ContextVar

solicitud_id: ContextVar[str] = ContextVar("solicitud_id", default="")
solicitud_inicio: ContextVar[float | None] = ContextVar("solicitud_inicio", default=None)


class FiltroSolicitud(logging.Filter):
    """Agrega `solicitud` a cada registro: '[id] ' dentro de una solicitud, vacío fuera."""

    def filter(self, record: logging.LogRecord) -> bool:
        identificador = solicitud_id.get()
        record.solicitud = f"[{identificador}] " if identificador else ""
        return True
