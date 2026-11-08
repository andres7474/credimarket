"""Caché cache-aside del catálogo en Redis, tolerante a fallos y con métricas.

Clave:      productos:v1:{producto_id}   (v1 = versión del formato del valor guardado)
Valor:      JSON del producto (incluye su version y el flag activo)
TTL:        CACHE_TTL_SEGUNDOS (60 s por defecto)
Escritura:  la BD se actualiza primero; tras el COMMIT se borra la clave (invalidación).
Fallo:      timeouts de 100 ms; tras un error se omite Redis durante 5 s (cortocircuito) y
            se responde desde la BD.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque

import redis

log = logging.getLogger("cache")
PREFIJO = "productos:v1:"


def clave(producto_id: int) -> str:
    return f"{PREFIJO}{producto_id}"


class Metricas:
    def __init__(self):
        self.lock = threading.Lock()
        self.reiniciar()

    def reiniciar(self):
        with getattr(self, "lock", threading.Lock()):
            self.hits = self.misses = self.errores = self.omitidas = 0
            self.invalidaciones = self.invalidaciones_fallidas = 0
            self.latencias = {"hit": deque(maxlen=5000), "miss": deque(maxlen=5000), "sin_cache": deque(maxlen=5000)}

    def registrar(self, tipo: str, ms: float):
        with self.lock:
            self.latencias[tipo].append(ms)

    @staticmethod
    def _percentil(valores, p):
        if not valores:
            return None
        v = sorted(valores)
        return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 3)

    def resumen(self) -> dict:
        with self.lock:
            consultas = self.hits + self.misses
            return {
                "hits": self.hits, "misses": self.misses, "errores_redis": self.errores,
                "consultas_sin_redis": self.omitidas,
                "tasa_aciertos": round(self.hits / consultas, 4) if consultas else None,
                "invalidaciones": self.invalidaciones, "invalidaciones_fallidas": self.invalidaciones_fallidas,
                "latencia_ms": {t: {"n": len(v), "p50": self._percentil(v, 50), "p95": self._percentil(v, 95)}
                                for t, v in self.latencias.items()},
            }


class Cache:
    def __init__(self):
        self.ttl = int(os.environ.get("CACHE_TTL_SEGUNDOS", "60"))
        self.cliente = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                                            socket_timeout=0.1, socket_connect_timeout=0.1,
                                            retry_on_timeout=False, decode_responses=True)
        self.omitir_hasta = 0.0
        self.metricas = Metricas()

    def _disponible(self) -> bool:
        return time.monotonic() >= self.omitir_hasta

    def _fallo(self, e: Exception) -> None:
        self.metricas.errores += 1
        self.omitir_hasta = time.monotonic() + 5
        log.warning("Redis no disponible (%r); se responde desde la BD durante 5 s", e)

    def leer(self, producto_id: int):
        """Devuelve (valor | None, estado) con estado HIT, MISS u OMITIDA (Redis caído)."""
        if not self._disponible():
            self.metricas.omitidas += 1
            return None, "OMITIDA"
        try:
            valor = self.cliente.get(clave(producto_id))
        except redis.RedisError as e:
            self._fallo(e)
            self.metricas.omitidas += 1
            return None, "OMITIDA"
        if valor is None:
            self.metricas.misses += 1
            return None, "MISS"
        self.metricas.hits += 1
        return json.loads(valor), "HIT"

    def guardar(self, producto_id: int, valor: dict) -> None:
        if not self._disponible():
            return
        try:
            self.cliente.set(clave(producto_id), json.dumps(valor, default=str), ex=self.ttl)
        except redis.RedisError as e:
            self._fallo(e)

    def invalidar(self, producto_id: int) -> bool:
        try:
            self.cliente.delete(clave(producto_id))
            self.metricas.invalidaciones += 1
            return True
        except redis.RedisError as e:
            # La entrada vieja puede vivir hasta su TTL: es la ventana de dato desactualizado documentada.
            self.metricas.invalidaciones_fallidas += 1
            self._fallo(e)
            return False

    def ttl_restante(self, producto_id: int):
        try:
            return self.cliente.ttl(clave(producto_id))
        except redis.RedisError:
            return None

    def disponible_ahora(self) -> bool:
        try:
            return bool(self.cliente.ping())
        except redis.RedisError:
            return False
