"""Servicio de Inventario: existencias, reservas atómicas, salidas, liberaciones y movimientos.

Recibe comandos de Préstamos por RabbitMQ y responde con eventos (vía outbox).
La reserva es un UPDATE condicional: nunca deja existencias negativas (además hay CHECK en la BD).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from credimarket_comun import mensajeria as mq
from credimarket_comun.auth import Usuario, requiere_rol, token_servicio
from credimarket_comun.db import aplicar_esquema, crear_pool, db_lista
from credimarket_comun.web import configurar_app, configurar_logs

configurar_logs()
log = logging.getLogger("inventario")
RESERVA_TTL_S = int(os.environ.get("RESERVA_TTL_SEGUNDOS", "1800"))
PRODUCTOS_URL = os.environ.get("PRODUCTOS_URL", "http://localhost:8001")
# Existencias iniciales de la demo (producto_id: unidades). El 6 tiene una sola unidad.
INICIALES = {1: 20, 2: 30, 3: 10, 4: 5, 5: 40, 6: 1}

pool = None
estado_mq = mq.Estado()
hilos: list = []


def _mov(cur, producto_id, tipo, cantidad, prestamo_id=None, referencia=None) -> None:
    cur.execute(
        "INSERT INTO movimientos (producto_id, tipo, cantidad, prestamo_id, referencia, disponible_despues, reservado_despues) "
        "SELECT %s, %s, %s, %s, %s, disponible, reservado FROM existencias WHERE producto_id = %s",
        (producto_id, tipo, cantidad, prestamo_id, referencia, producto_id))


def _evento(cur, tipo: str, m: dict, payload: dict) -> None:
    mq.encolar(cur, mq.crear_mensaje(tipo, m["prestamo_id"], payload, m["correlation_id"], "inventario"))


def _devolver_stock(cur, r: dict) -> None:
    cur.execute("UPDATE existencias SET disponible = disponible + %s, reservado = reservado - %s, actualizado_en = now() "
                "WHERE producto_id = %s", (r["cantidad"], r["cantidad"], r["producto_id"]))


# ------------------------------------------------------------------ comandos

def reservar(cur, m: dict) -> str:
    pl = m["payload"]
    producto_id, cantidad = int(pl["producto_id"]), int(pl["cantidad"])
    if cantidad <= 0:
        raise mq.MensajeInvalido("cantidad debe ser positiva")
    reserva_id = uuid.uuid4()
    expira = datetime.now(timezone.utc) + timedelta(seconds=RESERVA_TTL_S)
    # La fila de la reserva se crea primero: UNIQUE(prestamo_id) serializa comandos del mismo préstamo.
    cur.execute("INSERT INTO reservas (id, prestamo_id, producto_id, cantidad, estado, expira_en) "
                "VALUES (%s,%s,%s,%s,'RESERVADA',%s) ON CONFLICT (prestamo_id) DO NOTHING RETURNING id",
                (reserva_id, m["prestamo_id"], producto_id, cantidad, expira))
    if cur.fetchone() is None:
        return "ya existía una reserva para este préstamo (o fue liberada antes): sin efecto"
    # Reserva atómica: solo descuenta si alcanza.
    cur.execute("UPDATE existencias SET disponible = disponible - %s, reservado = reservado + %s, actualizado_en = now() "
                "WHERE producto_id = %s AND disponible >= %s RETURNING disponible",
                (cantidad, cantidad, producto_id, cantidad))
    if cur.fetchone() is None:
        fila = cur.execute("SELECT disponible FROM existencias WHERE producto_id = %s", (producto_id,)).fetchone()
        disponible = fila["disponible"] if fila else 0
        cur.execute("UPDATE reservas SET estado = 'RECHAZADA', motivo = 'sin existencias', expira_en = NULL WHERE id = %s",
                    (reserva_id,))
        _evento(cur, "InventarioInsuficiente", m, {"producto_id": producto_id, "cantidad": cantidad,
                                                    "disponible": disponible})
        return "insuficiente"
    _mov(cur, producto_id, "RESERVA", cantidad, m["prestamo_id"])
    _evento(cur, "InventarioReservado", m, {"reserva_id": str(reserva_id), "producto_id": producto_id,
                                             "cantidad": cantidad, "expira_en": expira.isoformat()})
    return "reservada"


def liberar(cur, m: dict) -> str:
    pl = m["payload"]
    r = cur.execute("SELECT * FROM reservas WHERE prestamo_id = %s FOR UPDATE", (m["prestamo_id"],)).fetchone()
    if r is None:
        # La liberación llegó antes que la reserva: se deja una marca para que la reserva posterior no tenga efecto.
        cur.execute("INSERT INTO reservas (id, prestamo_id, producto_id, cantidad, estado, motivo) "
                    "VALUES (%s,%s,%s,0,'LIBERADA','liberación recibida antes que la reserva') "
                    "ON CONFLICT (prestamo_id) DO NOTHING RETURNING id",
                    (uuid.uuid4(), m["prestamo_id"], int(pl["producto_id"])))
        if cur.fetchone() is None:
            raise RuntimeError("la reserva se creó en paralelo; se reintenta")
        _evento(cur, "ReservaLiberada", m, {"producto_id": int(pl["producto_id"]), "cantidad_liberada": 0})
        return "marca de liberación (no había reserva)"
    if r["estado"] == "CONFIRMADA":
        log.warning("Liberación pedida para una reserva ya entregada (%s): se ignora", m["prestamo_id"])
        return "ignorada: ya entregada"
    liberada = 0
    if r["estado"] == "RESERVADA":
        _devolver_stock(cur, r)
        cur.execute("UPDATE reservas SET estado = 'LIBERADA', motivo = %s, actualizado_en = now() WHERE id = %s",
                    (pl.get("motivo", "cancelacion"), r["id"]))
        _mov(cur, r["producto_id"], "LIBERACION", r["cantidad"], m["prestamo_id"], pl.get("motivo"))
        liberada = r["cantidad"]
    _evento(cur, "ReservaLiberada", m, {"producto_id": r["producto_id"], "cantidad_liberada": liberada,
                                        "estado_reserva_previo": r["estado"]})
    return f"liberada ({liberada} unidades)"


def entregar(cur, m: dict) -> str:
    r = cur.execute("SELECT * FROM reservas WHERE prestamo_id = %s FOR UPDATE", (m["prestamo_id"],)).fetchone()
    if r is None or r["estado"] != "RESERVADA":
        _evento(cur, "EntregaRechazada", m, {"motivo": f"no hay reserva activa (estado: {r['estado'] if r else 'inexistente'})"})
        return "entrega rechazada"
    if r["expira_en"] <= datetime.now(timezone.utc):
        _vencer(cur, r, m["correlation_id"])
        return "reserva vencida al entregar"
    cur.execute("UPDATE existencias SET reservado = reservado - %s, actualizado_en = now() WHERE producto_id = %s",
                (r["cantidad"], r["producto_id"]))
    cur.execute("UPDATE reservas SET estado = 'CONFIRMADA', actualizado_en = now() WHERE id = %s", (r["id"],))
    _mov(cur, r["producto_id"], "SALIDA", r["cantidad"], m["prestamo_id"], "entrega simulada")
    _evento(cur, "SalidaConfirmada", m, {"reserva_id": str(r["id"]), "producto_id": r["producto_id"],
                                         "cantidad": r["cantidad"]})
    return "salida confirmada"


def _vencer(cur, r: dict, correlation_id: str) -> None:
    _devolver_stock(cur, r)
    cur.execute("UPDATE reservas SET estado = 'VENCIDA', motivo = 'sin entrega antes del vencimiento', "
                "actualizado_en = now() WHERE id = %s", (r["id"],))
    _mov(cur, r["producto_id"], "VENCIMIENTO", r["cantidad"], r["prestamo_id"])
    mq.encolar(cur, mq.crear_mensaje("ReservaVencida", str(r["prestamo_id"]),
                                     {"reserva_id": str(r["id"]), "producto_id": r["producto_id"],
                                      "cantidad": r["cantidad"]}, correlation_id, "inventario"))


MANEJADORES = {"ReservaSolicitada": reservar, "LiberacionSolicitada": liberar, "EntregaSolicitada": entregar}


def manejar_comando(m: dict) -> None:
    manejador = MANEJADORES.get(m["type"])
    if manejador is None:
        raise mq.MensajeInvalido(f"Inventario no procesa {m['type']}")
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            if not mq.registrar_procesado(cur, m):  # efecto y deduplicación en la misma transacción
                estado_mq.duplicados += 1
                log.info("Comando duplicado %s (%s): sin efecto", m["event_id"], m["type"])
                return
            resultado = manejador(cur, m)
            mq.anotar_resultado(cur, m, resultado)
            log.info("%s %s: %s", m["type"], m["prestamo_id"], resultado)


class VigilanteReservas(threading.Thread):
    """Vence reservas no entregadas a tiempo, devuelve las unidades y avisa a Préstamos."""

    def __init__(self):
        super().__init__(daemon=True, name="vigilante-reservas")

    def run(self) -> None:
        while True:
            time.sleep(5)
            try:
                with pool.connection() as conn:
                    with conn.transaction():
                        cur = conn.cursor()
                        for r in cur.execute("SELECT * FROM reservas WHERE estado = 'RESERVADA' AND expira_en <= now() "
                                             "ORDER BY expira_en LIMIT 50 FOR UPDATE SKIP LOCKED").fetchall():
                            _vencer(cur, r, str(uuid.uuid4()))
                            log.info("Reserva %s vencida", r["id"])
            except Exception as e:
                log.warning("Vigilante de reservas: %r", e)


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    global pool
    pool = crear_pool()
    aplicar_esquema(pool, Path(__file__).with_name("esquema.sql"))
    with pool.connection() as conn:
        conn.execute(mq.SQL_OUTBOX)
        if conn.execute("SELECT count(*) AS n FROM existencias").fetchone()["n"] == 0:
            with conn.transaction():
                for producto_id, unidades in INICIALES.items():
                    conn.execute("INSERT INTO existencias (producto_id, disponible) VALUES (%s, %s)", (producto_id, unidades))
                    _mov(conn.cursor(), producto_id, "ENTRADA", unidades, referencia="inventario inicial")
    hilos.extend([mq.RelayOutbox(pool, estado_mq), mq.Consumidor(mq.COLA_INVENTARIO, manejar_comando, estado_mq),
                  VigilanteReservas()])
    for h in hilos:
        h.start()
    yield
    pool.close()


app = FastAPI(title="CrediMarket · Inventario", version="1.0.0", lifespan=ciclo_vida)
configurar_app(app)


class Entrada(BaseModel):
    cantidad: int = Field(ge=1, le=10_000)
    referencia: str = Field(default="entrada manual", max_length=120)


def _fechas(filas: list[dict]) -> list[dict]:
    for f in filas:
        for k, v in list(f.items()):
            if isinstance(v, datetime):
                f[k] = v.isoformat()
            elif isinstance(v, uuid.UUID):
                f[k] = str(v)
    return filas


@app.get("/health")
def health():
    return {"estado": "ok", "servicio": "inventario"}


@app.get("/ready")
def ready(response: Response):
    bd = db_lista(pool)
    listo = bd and estado_mq.relay_conectado and estado_mq.consumidor_conectado
    if not listo:
        response.status_code = 503
    return {"listo": listo, "base_datos": bd, "broker_publicacion": estado_mq.relay_conectado,
            "broker_consumo": estado_mq.consumidor_conectado}


@app.get("/existencias")
def existencias():
    with pool.connection() as conn:
        return _fechas(conn.execute("SELECT * FROM existencias ORDER BY producto_id").fetchall())


@app.get("/existencias/{producto_id}")
def existencia(producto_id: int):
    with pool.connection() as conn:
        f = conn.execute("SELECT * FROM existencias WHERE producto_id = %s", (producto_id,)).fetchone()
    if f is None:
        return {"producto_id": producto_id, "disponible": 0, "reservado": 0}
    return _fechas([f])[0]


@app.post("/existencias/{producto_id}/entradas", status_code=201)
def registrar_entrada(producto_id: int, e: Entrada, _: Usuario = Depends(requiere_rol("admin"))):
    # El producto debe existir en el catálogo (consulta por API, no por tabla ajena).
    try:
        r = httpx.get(f"{PRODUCTOS_URL}/internos/productos/{producto_id}/cotizacion",
                      headers={"Authorization": f"Bearer {token_servicio('inventario')}"}, timeout=2)
    except httpx.HTTPError:
        raise HTTPException(503, detail={"codigo": "productos_no_disponible", "mensaje": "No se pudo validar el producto."})
    if r.status_code == 404:
        raise HTTPException(422, detail={"codigo": "producto_inexistente", "mensaje": "El producto no existe en el catálogo."})
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            cur.execute("INSERT INTO existencias (producto_id, disponible) VALUES (%s, %s) ON CONFLICT (producto_id) "
                        "DO UPDATE SET disponible = existencias.disponible + EXCLUDED.disponible, actualizado_en = now()",
                        (producto_id, e.cantidad))
            _mov(cur, producto_id, "ENTRADA", e.cantidad, referencia=e.referencia)
        return _fechas([conn.execute("SELECT * FROM existencias WHERE producto_id = %s", (producto_id,)).fetchone()])[0]


@app.get("/reservas")
def reservas(estado: str | None = None, limite: int = Query(100, ge=1, le=500),
             _: Usuario = Depends(requiere_rol("admin"))):
    sql, params = "SELECT * FROM reservas", []
    if estado:
        sql += " WHERE estado = %s"
        params.append(estado)
    with pool.connection() as conn:
        return _fechas(conn.execute(sql + " ORDER BY creado_en DESC LIMIT %s", [*params, limite]).fetchall())


@app.get("/movimientos")
def movimientos(producto_id: int | None = None, limite: int = Query(100, ge=1, le=500),
                _: Usuario = Depends(requiere_rol("admin"))):
    sql, params = "SELECT * FROM movimientos", []
    if producto_id:
        sql += " WHERE producto_id = %s"
        params.append(producto_id)
    with pool.connection() as conn:
        return _fechas(conn.execute(sql + " ORDER BY id DESC LIMIT %s", [*params, limite]).fetchall())


@app.get("/mensajeria/outbox")
def outbox(prestamo_id: uuid.UUID, _: Usuario = Depends(requiere_rol("admin"))):
    """Eventos que Inventario publicó para un préstamo (para trazabilidad y pruebas de reentrega)."""
    with pool.connection() as conn:
        filas = conn.execute("SELECT mensaje, publicado_en FROM outbox WHERE prestamo_id = %s ORDER BY id",
                             (prestamo_id,)).fetchall()
    return [{"mensaje": f["mensaje"], "publicado_en": f["publicado_en"] and f["publicado_en"].isoformat()} for f in filas]


@app.get("/mensajeria/estado")
def mensajeria(_: Usuario = Depends(requiere_rol("admin"))):
    with pool.connection() as conn:
        pendientes = conn.execute("SELECT count(*) AS n FROM outbox WHERE publicado_en IS NULL").fetchone()["n"]
        procesados = conn.execute("SELECT tipo, count(*) AS n FROM mensajes_procesados GROUP BY tipo").fetchall()
    return {"outbox_pendientes": pendientes, "procesados": procesados, **estado_mq.como_dict()}
