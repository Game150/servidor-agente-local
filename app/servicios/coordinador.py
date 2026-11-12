import asyncio


class Coordinador:
    """Decide cuándo se puede cambiar de agente.

    Varias peticiones del mismo agente conviven sin problema (llama-server las
    encola). Pero si llega una petición de un agente que necesita otro modelo,
    primero se espera a que terminen las que están en curso, luego se cambia
    el modelo y mientras tanto las nuevas peticiones esperan. Así un cambio
    de agente nunca corta una respuesta que se está transmitiendo.
    """

    def __init__(self, motor) -> None:
        self._motor = motor
        self._cond = asyncio.Condition()
        self._activas = 0
        self._cambiando = False

    async def preparar(self, agente: dict, reservar: bool = False) -> None:
        """Deja el motor listo para `agente`, cambiando de modelo si hace falta.

        Con `reservar=True` además cuenta la petición como activa; en ese caso
        quien llama debe invocar `liberar()` al terminar (incluso si falla).
        Las excepciones del motor (FileNotFoundError, RuntimeError) se propagan
        sin dejar el coordinador en un estado inconsistente.
        """
        async with self._cond:
            while True:
                if self._cambiando:
                    await self._cond.wait()
                    continue

                if self._motor.es_compatible(agente):
                    if reservar:
                        self._activas += 1
                    return

                self._cambiando = True

                try:
                    await self._cond.wait_for(lambda: self._activas == 0)
                    await asyncio.to_thread(self._motor.asegurar, agente)
                finally:
                    self._cambiando = False
                    self._cond.notify_all()

    async def liberar(self) -> None:
        async with self._cond:
            self._activas = max(0, self._activas - 1)
            self._cond.notify_all()
