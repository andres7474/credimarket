"""Mensajería con RabbitMQ: contratos, topología, outbox y consumidores idempotentes.

Topología (declarada de forma idempotente por cada servicio al conectar):

  credimarket.comandos (direct)  --inventario.*-->  inventario.comandos  (quorum)
  credimarket.eventos  (topic)   --inventario.#-->  prestamos.eventos    (quorum)
  credimarket.dlx      (fanout)  ------------------> credimarket.dlq     (quorum)

Las colas quorum tienen x-delivery-limit = MAX_REINTENTOS: un mensaje que falla se devuelve
a la cola (nack con requeue) y, al superar el límite, RabbitMQ lo envía al dead letter
exchange. Un mensaje que no se puede interpretar va directo al DLQ (nack sin requeue).

Garantías: publicación con publisher confirms + mandatory (desde el outbox), mensajes
persistentes, ack manual SOLO después de confirmar la transacción local. La entrega es
"al menos una vez"; la deduplicación se hace con la tabla mensajes_procesados en la
misma transacción que el efecto de negocio.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

import pika
from pika.exceptions import NackError, UnroutableError
from psycopg.types.json import Jsonb

log = logging.getLogger("mensajeria")

VERSION_ESQUEMA = 1
EXCHANGE_COMANDOS = "credimarket.comandos"
EXCHANGE_EVENTOS = "credimarket.eventos"
EXCHANGE_DLX = "credimarket.dlx"
COLA_INVENTARIO = "inventario.comandos"
COLA_PRESTAMOS = "prestamos.eventos"
COLA_DLQ = "credimarket.dlq"
MAX_REINTENTOS = int(os.environ.get("MAX_REINTENTOS", "3"))

# tipo -> (exchange, routing key)
RUTAS = {
    # comandos: Préstamos -> Inventario
    "ReservaSolicitada": (EXCHANGE_COMANDOS, "inventario.reservar"),
    "LiberacionSolicitada": (EXCHANGE_COMANDOS, "inventario.liberar"),
    "EntregaSolicitada": (EXCHANGE_COMANDOS, "inventario.entregar"),
    # eventos: Inventario -> Préstamos
    "InventarioReservado": (EXCHANGE_EVENTOS, "inventario.reservado"),
    "InventarioInsuficiente": (EXCHANGE_EVENTOS, "inventario.insuficiente"),
    "ReservaLiberada": (EXCHANGE_EVENTOS, "inventario.liberada"),
    "SalidaConfirmada": (EXCHANGE_EVENTOS, "inventario.salida_confirmada"),
    "EntregaRechazada": (EXCHANGE_EVENTOS, "inventario.entrega_rechazada"),
    "ReservaVencida": (EXCHANGE_EVENTOS, "inventario.vencida"),
}
CAMPOS_SOBRE = ("event_id", "type", "schema_version", "occurred_at", "correlation_id", "prestamo_id", "payload")


class MensajeInvalido(Exception):
    """El mensaje no cumple el contrato: no tiene sentido reintentarlo."""


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def crear_mensaje(tipo: str, prestamo_id: str, payload: dict, correlation_id: str, productor: str) -> dict:
    if tipo not in RUTAS:
        raise ValueError(f"tipo desconocido: {tipo}")
    return {
        "event_id": str(uuid.uuid4()),
        "type": tipo,
        "schema_version": VERSION_ESQUEMA,
        "occurred_at": ahora_iso(),
        "correlation_id": str(correlation_id),
        "prestamo_id": str(prestamo_id),
        "producer": productor,
        "payload": payload,
    }


def validar_mensaje(cuerpo: bytes) -> dict:
    try:
        m = json.loads(cuerpo)
    except Exception as e:
        raise MensajeInvalido(f"JSON inválido: {e}")
    if not isinstance(m, dict):
        raise MensajeInvalido("el mensaje debe ser un objeto JSON")
    faltan = [c for c in CAMPOS_SOBRE if c not in m]
    if faltan:
        raise MensajeInvalido(f"faltan campos del sobre: {faltan}")
    if m["type"] not in RUTAS:
        raise MensajeInvalido(f"tipo desconocido: {m['type']}")
    if m["schema_version"] != VERSION_ESQUEMA:
        raise MensajeInvalido(f"schema_version no soportada: {m['schema_version']}")
    try:
        uuid.UUID(str(m["event_id"]))
        uuid.UUID(str(m["prestamo_id"]))
    except ValueError:
        raise MensajeInvalido("event_id y prestamo_id deben ser UUID")
    if not isinstance(m["payload"], dict):
        raise MensajeInvalido("payload debe ser un objeto")
    return m


def parametros_conexion() -> pika.URLParameters:
    url = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F")
    p = pika.URLParameters(url)
    p.heartbeat = 30
    p.blocked_connection_timeout = 30
    p.connection_attempts = 1
    p.socket_timeout = 5
    return p


def declarar_topologia(canal) -> None:
    canal.exchange_declare(EXCHANGE_COMANDOS, exchange_type="direct", durable=True)
    canal.exchange_declare(EXCHANGE_EVENTOS, exchange_type="topic", durable=True)
    canal.exchange_declare(EXCHANGE_DLX, exchange_type="fanout", durable=True)
    argumentos = {"x-queue-type": "quorum", "x-delivery-limit": MAX_REINTENTOS,
                  "x-dead-letter-exchange": EXCHANGE_DLX}
    canal.queue_declare(COLA_INVENTARIO, durable=True, arguments=argumentos)
    canal.queue_declare(COLA_PRESTAMOS, durable=True, arguments=argumentos)
    canal.queue_declare(COLA_DLQ, durable=True, arguments={"x-queue-type": "quorum"})
    for _, rk in (v for v in RUTAS.values() if v[0] == EXCHANGE_COMANDOS):
        canal.queue_bind(COLA_INVENTARIO, EXCHANGE_COMANDOS, routing_key=rk)
    canal.queue_bind(COLA_PRESTAMOS, EXCHANGE_EVENTOS, routing_key="inventario.#")
    canal.queue_bind(COLA_DLQ, EXCHANGE_DLX)


# ---------------------------------------------------------------- outbox

SQL_OUTBOX = """
CREATE TABLE IF NOT EXISTS outbox (
    id            BIGSERIAL PRIMARY KEY,
    event_id      UUID NOT NULL UNIQUE,
    tipo          TEXT NOT NULL,
    prestamo_id   UUID NOT NULL,
    mensaje       JSONB NOT NULL,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    publicado_en  TIMESTAMPTZ,
    intentos      INT NOT NULL DEFAULT 0,
    ultimo_error  TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pendientes ON outbox (id) WHERE publicado_en IS NULL;

CREATE TABLE IF NOT EXISTS mensajes_procesados (
    event_id      UUID PRIMARY KEY,
    tipo          TEXT NOT NULL,
    prestamo_id   UUID NOT NULL,
    procesado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resultado     TEXT
);
"""


def encolar(cur, mensaje: dict) -> None:
    """Guarda el mensaje en el outbox DENTRO de la transacción del cambio de estado."""
    cur.execute(
        "INSERT INTO outbox (event_id, tipo, prestamo_id, mensaje) VALUES (%s, %s, %s, %s)",
        (mensaje["event_id"], mensaje["type"], mensaje["prestamo_id"], Jsonb(mensaje)),
    )


def registrar_procesado(cur, mensaje: dict) -> bool:
    """Inbox: True si es la primera vez que se procesa este event_id (en esta transacción)."""
    cur.execute(
        "INSERT INTO mensajes_procesados (event_id, tipo, prestamo_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
        (mensaje["event_id"], mensaje["type"], mensaje["prestamo_id"]),
    )
    return cur.fetchone() is not None


def anotar_resultado(cur, mensaje: dict, resultado: str) -> None:
    cur.execute("UPDATE mensajes_procesados SET resultado = %s WHERE event_id = %s", (resultado, mensaje["event_id"]))


class Estado:
    """Estado observable de los hilos de mensajería (para /ready y métricas)."""

    def __init__(self):
        self.relay_conectado = False
        self.consumidor_conectado = False
        self.publicados = 0
        self.consumidos = 0
        self.duplicados = 0
        self.reintentos = 0
        self.a_dlq = 0
        self.ultimo_error: str | None = None

    def como_dict(self) -> dict:
        return dict(self.__dict__)


class RelayOutbox(threading.Thread):
    """Publica en RabbitMQ lo que quedó en el outbox y lo marca como publicado.

    Si el proceso cae después de publicar y antes de marcar, el mensaje se publica otra vez:
    los consumidores deduplican por event_id.
    """

    def __init__(self, pool, estado: Estado, intervalo: float = 0.3):
        super().__init__(daemon=True, name="relay-outbox")
        self.pool, self.estado, self.intervalo = pool, estado, intervalo
        self.detener = threading.Event()

    def run(self) -> None:
        while not self.detener.is_set():
            conexion = None
            try:
                conexion = pika.BlockingConnection(parametros_conexion())
                canal = conexion.channel()
                declarar_topologia(canal)
                canal.confirm_delivery()
                self.estado.relay_conectado = True
                while not self.detener.is_set():
                    publicados = self._publicar_lote(canal)
                    conexion.process_data_events(time_limit=0 if publicados else self.intervalo)
            except Exception as e:
                self.estado.relay_conectado = False
                self.estado.ultimo_error = f"relay: {e!r}"
                log.warning("Relay del outbox desconectado: %r. Reintento en 3 s", e)
                time.sleep(3)
            finally:
                try:
                    if conexion and conexion.is_open:
                        conexion.close()
                except Exception:
                    pass

    def _publicar_lote(self, canal) -> int:
        n = 0
        with self.pool.connection() as conn:
            with conn.transaction():
                filas = conn.execute(
                    "SELECT id, mensaje FROM outbox WHERE publicado_en IS NULL ORDER BY id LIMIT 50 "
                    "FOR UPDATE SKIP LOCKED").fetchall()
                for fila in filas:
                    m = fila["mensaje"]
                    exchange, rk = RUTAS[m["type"]]
                    try:
                        canal.basic_publish(
                            exchange, rk, json.dumps(m).encode(),
                            properties=pika.BasicProperties(
                                content_type="application/json", delivery_mode=2, message_id=m["event_id"],
                                type=m["type"], correlation_id=m["correlation_id"],
                                headers={"schema_version": m["schema_version"]}),
                            mandatory=True)
                    except (UnroutableError, NackError) as e:
                        conn.execute("UPDATE outbox SET intentos = intentos + 1, ultimo_error = %s WHERE id = %s",
                                     (repr(e), fila["id"]))
                        log.error("Broker rechazó %s: %r", m["event_id"], e)
                        break
                    conn.execute("UPDATE outbox SET publicado_en = now(), intentos = intentos + 1 WHERE id = %s",
                                 (fila["id"],))
                    self.estado.publicados += 1
                    n += 1
        return n


class Consumidor(threading.Thread):
    """Consume una cola y llama al manejador; ack solo tras el commit del manejador."""

    def __init__(self, cola: str, manejador: Callable[[dict], None], estado: Estado, prefetch: int = 10):
        super().__init__(daemon=True, name=f"consumidor-{cola}")
        self.cola, self.manejador, self.estado, self.prefetch = cola, manejador, estado, prefetch
        self.detener = threading.Event()

    def run(self) -> None:
        while not self.detener.is_set():
            conexion = None
            try:
                conexion = pika.BlockingConnection(parametros_conexion())
                canal = conexion.channel()
                declarar_topologia(canal)
                canal.basic_qos(prefetch_count=self.prefetch)
                canal.basic_consume(self.cola, self._al_recibir, auto_ack=False)
                self.estado.consumidor_conectado = True
                log.info("Consumiendo %s", self.cola)
                while not self.detener.is_set():
                    conexion.process_data_events(time_limit=1)
            except Exception as e:
                self.estado.consumidor_conectado = False
                self.estado.ultimo_error = f"consumidor: {e!r}"
                log.warning("Consumidor de %s desconectado: %r. Reintento en 3 s", self.cola, e)
                time.sleep(3)
            finally:
                try:
                    if conexion and conexion.is_open:
                        conexion.close()
                except Exception:
                    pass

    def _al_recibir(self, canal, metodo, propiedades, cuerpo) -> None:
        entregas = int((propiedades.headers or {}).get("x-delivery-count", 0) or 0)
        try:
            mensaje = validar_mensaje(cuerpo)
        except MensajeInvalido as e:
            log.error("Mensaje inválido enviado al DLQ: %s", e)
            self.estado.a_dlq += 1
            canal.basic_nack(metodo.delivery_tag, requeue=False)
            return
        try:
            self.manejador(mensaje)
            self.estado.consumidos += 1
            canal.basic_ack(metodo.delivery_tag)
        except MensajeInvalido as e:
            log.error("Mensaje %s rechazado por contrato; va al DLQ: %s", mensaje["event_id"], e)
            self.estado.a_dlq += 1
            canal.basic_nack(metodo.delivery_tag, requeue=False)
        except Exception as e:
            self.estado.reintentos += 1
            self.estado.ultimo_error = f"{mensaje['type']} {mensaje['event_id']}: {e!r}"
            if entregas >= MAX_REINTENTOS:
                # Límite propio además del x-delivery-limit de la cola: no depende de la versión del broker.
                self.estado.a_dlq += 1
                log.error("%s superó %s reintentos; se envía al DLQ: %r", mensaje["event_id"], MAX_REINTENTOS, e)
                canal.basic_nack(metodo.delivery_tag, requeue=False)
                return
            log.warning("Fallo procesando %s (entrega %s): %r. Se reintenta", mensaje["event_id"], entregas + 1, e)
            time.sleep(min(0.5 * (entregas + 1), 3))  # espera creciente antes de devolverlo a la cola
            canal.basic_nack(metodo.delivery_tag, requeue=True)


def publicar_directo(mensaje: dict) -> None:
    """Solo para scripts de prueba (reentrega de un evento). Los servicios usan el outbox."""
    conexion = pika.BlockingConnection(parametros_conexion())
    try:
        canal = conexion.channel()
        declarar_topologia(canal)
        canal.confirm_delivery()
        exchange, rk = RUTAS[mensaje["type"]]
        canal.basic_publish(exchange, rk, json.dumps(mensaje).encode(),
                            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                                                            message_id=mensaje["event_id"], type=mensaje["type"]),
                            mandatory=True)
    finally:
        conexion.close()
