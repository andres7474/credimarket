"""Servicio de Préstamos: recibe solicitudes, reúne datos, ejecuta el modelo, aplica reglas,
gestiona estados y coordina reservas, entregas y cancelaciones con Inventario vía RabbitMQ.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from credimarket_comun import mensajeria as mq
from credimarket_comun.auth import Usuario, requiere_rol, token_servicio, usuario_actual
from credimarket_comun.db import aplicar_esquema, crear_pool, db_lista
from credimarket_comun.web import configurar_app, configurar_logs
from credimarket_ml.features import PLAZOS_VALIDOS

from . import estados as E
from .modelo import EntradaRiesgo, ModeloActivo, psi

configurar_logs()
log = logging.getLogger("prestamos")

CLIENTES_URL = os.environ.get("CLIENTES_URL", "http://localhost:8002")
PRODUCTOS_URL = os.environ.get("PRODUCTOS_URL", "http://localhost:8001")
MONTO_MIN = int(os.environ.get("MONTO_MIN_COP", "300000"))
MONTO_MAX = int(os.environ.get("MONTO_MAX_COP", "15000000"))
CANTIDAD_MAX = 5
PENDIENTE_TIMEOUT_S = int(os.environ.get("PENDIENTE_TIMEOUT_SEGUNDOS", "600"))
TIMEOUT_HTTP_S = 2.0

pool = None
modelo = ModeloActivo()
estado_mq = mq.Estado()
hilos: list = []


# ------------------------------------------------------------------ utilidades

def _serializar(p: dict) -> dict:
    d = dict(p)
    for k in ("id", "reserva_id", "correlation_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    for k in ("precio_unitario", "monto", "probabilidad", "umbral"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    for k in ("creado_en", "actualizado_en", "reserva_expira_en"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    d["descripcion_estado"] = E.DESCRIPCION.get(d["estado"])
    d["es_final"] = d["estado"] in E.FINALES
    return d


def _historial(conn, prestamo_id) -> list[dict]:
    filas = conn.execute("SELECT estado_anterior, estado_nuevo, motivo, evento_id, aplicado, creado_en "
                         "FROM historial_estados WHERE prestamo_id = %s ORDER BY id", (prestamo_id,)).fetchall()
    for f in filas:
        f["creado_en"] = f["creado_en"].isoformat()
        f["evento_id"] = str(f["evento_id"]) if f["evento_id"] else None
    return filas


def _registrar(cur, prestamo_id, anterior, nuevo, motivo, evento_id=None, aplicado=True) -> None:
    cur.execute("INSERT INTO historial_estados (prestamo_id, estado_anterior, estado_nuevo, motivo, evento_id, aplicado) "
                "VALUES (%s,%s,%s,%s,%s,%s)", (prestamo_id, anterior, nuevo, motivo, evento_id, aplicado))


def _transicion(cur, p: dict, disparador: str, motivo: str, evento_id=None, extra: dict | None = None) -> str | None:
    """Aplica una transición válida o registra el evento como ignorado. Devuelve el estado nuevo."""
    nuevo = E.siguiente(p["estado"], disparador)
    if nuevo is None:
        _registrar(cur, p["id"], p["estado"], p["estado"],
                   f"{disparador} ignorado en estado {p['estado']} (evento tardío o fuera de orden)", evento_id, False)
        return None
    sets = {"estado": nuevo, "motivo": motivo, **(extra or {})}
    cur.execute(f"UPDATE prestamos SET {', '.join(f'{k} = %s' for k in sets)}, actualizado_en = now() WHERE id = %s",
                [*sets.values(), p["id"]])
    _registrar(cur, p["id"], p["estado"], nuevo, motivo, evento_id)
    return nuevo


def _mensaje(tipo: str, p: dict, payload: dict) -> dict:
    return mq.crear_mensaje(tipo, str(p["id"]), payload, str(p["correlation_id"]), "prestamos")


def _http_interno(url: str) -> httpx.Response:
    return httpx.get(url, headers={"Authorization": f"Bearer {token_servicio('prestamos')}"}, timeout=TIMEOUT_HTTP_S)


# ------------------------------------------------------------------ eventos de Inventario

def manejar_evento(m: dict) -> None:
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            if not mq.registrar_procesado(cur, m):
                estado_mq.duplicados += 1
                log.info("Evento duplicado %s (%s): sin efecto", m["event_id"], m["type"])
                return
            p = cur.execute("SELECT * FROM prestamos WHERE id = %s FOR UPDATE", (m["prestamo_id"],)).fetchone()
            if p is None:
                raise RuntimeError(f"préstamo {m['prestamo_id']} desconocido")  # se reintenta y termina en el DLQ
            pl = m["payload"]
            motivos = {
                "InventarioReservado": "Inventario confirmó la reserva",
                "InventarioInsuficiente": f"Sin unidades suficientes (disponibles: {pl.get('disponible')})",
                "ReservaLiberada": "Reserva liberada por Inventario",
                "SalidaConfirmada": "Inventario confirmó la salida (entrega simulada)",
                "EntregaRechazada": f"Entrega rechazada: {pl.get('motivo')}",
                "ReservaVencida": "La reserva venció sin entrega",
            }
            extra = None
            if m["type"] == "InventarioReservado":
                extra = {"reserva_id": pl.get("reserva_id"), "reserva_expira_en": pl.get("expira_en")}
            if p["estado"] == E.CANCELACION_PENDIENTE and m["type"] in ("InventarioInsuficiente", "ReservaVencida"):
                motivos[m["type"]] = "Cancelación completada: no quedó reserva por liberar"
            nuevo = _transicion(cur, p, m["type"], motivos[m["type"]], m["event_id"], extra)
            mq.anotar_resultado(cur, m, f"{p['estado']} -> {nuevo}" if nuevo else f"ignorado en {p['estado']}")


class VigilanteTiempos(threading.Thread):
    """Si Inventario no responde en PENDIENTE_TIMEOUT_SEGUNDOS, se cancela y se pide liberar."""

    def __init__(self):
        super().__init__(daemon=True, name="vigilante-tiempos")

    def run(self) -> None:
        while True:
            time.sleep(5)
            try:
                with pool.connection() as conn:
                    with conn.transaction():
                        cur = conn.cursor()
                        vencidos = cur.execute(
                            "SELECT * FROM prestamos WHERE estado = %s AND actualizado_en < now() - make_interval(secs => %s) "
                            "ORDER BY actualizado_en LIMIT 50 FOR UPDATE SKIP LOCKED",
                            (E.PENDIENTE_INVENTARIO, PENDIENTE_TIMEOUT_S)).fetchall()
                        for p in vencidos:
                            if _transicion(cur, p, "tiempo_agotado",
                                           f"Sin respuesta de Inventario en {PENDIENTE_TIMEOUT_S} s; se cancela"):
                                mq.encolar(cur, _mensaje("LiberacionSolicitada", p, {
                                    "producto_id": p["producto_id"], "cantidad": p["cantidad"], "motivo": "tiempo_agotado"}))
            except Exception as e:
                log.warning("Vigilante de tiempos: %r", e)


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    global pool
    pool = crear_pool()
    aplicar_esquema(pool, Path(__file__).with_name("esquema.sql"))
    with pool.connection() as conn:
        conn.execute(mq.SQL_OUTBOX)
    try:
        modelo.cargar()
    except Exception:
        pass  # /ready informará "no disponible"
    hilos.extend([mq.RelayOutbox(pool, estado_mq), mq.Consumidor(mq.COLA_PRESTAMOS, manejar_evento, estado_mq),
                  VigilanteTiempos()])
    for h in hilos:
        h.start()
    yield
    pool.close()


app = FastAPI(title="CrediMarket · Préstamos", version="1.0.0", lifespan=ciclo_vida)
configurar_app(app)


# ------------------------------------------------------------------ API

class SolicitudPrestamo(BaseModel):
    producto_id: int = Field(gt=0)
    cantidad: int = Field(ge=1, le=CANTIDAD_MAX)
    plazo_meses: int

    model_config = {"extra": "forbid"}

    @field_validator("plazo_meses")
    @classmethod
    def _plazo(cls, v):
        if v not in PLAZOS_VALIDOS:
            raise ValueError(f"el plazo debe ser uno de {PLAZOS_VALIDOS} meses")
        return v


@app.get("/health")
def health():
    return {"estado": "ok", "servicio": "prestamos"}


@app.get("/ready")
def ready(response: Response):
    bd = db_lista(pool)
    listo = bd and modelo.listo and estado_mq.relay_conectado and estado_mq.consumidor_conectado
    if not listo:
        response.status_code = 503
    return {"listo": listo, "base_datos": bd, "modelo": modelo.listo, "modelo_error": modelo.error,
            "broker_publicacion": estado_mq.relay_conectado, "broker_consumo": estado_mq.consumidor_conectado}


def _respuesta_existente(conn, cliente_id: int, clave: str, hash_solicitud: str, response: Response):
    previa = conn.execute("SELECT i.hash_solicitud, p.* FROM idempotencia i JOIN prestamos p ON p.id = i.prestamo_id "
                          "WHERE i.cliente_id = %s AND i.clave = %s", (cliente_id, clave)).fetchone()
    if previa is None:
        return None
    if previa["hash_solicitud"] != hash_solicitud:
        raise HTTPException(409, detail={"codigo": "idempotencia_conflicto",
                                         "mensaje": "Esa Idempotency-Key ya se usó con un contenido distinto."})
    previa.pop("hash_solicitud")
    response.status_code = 201 if previa["estado"] == E.RECHAZADA else 202
    response.headers["Idempotent-Replay"] = "true"
    response.headers["Location"] = f"/prestamos/{previa['id']}"
    return _serializar(previa)


@app.post("/prestamos", status_code=202)
def solicitar(s: SolicitudPrestamo, response: Response,
              idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=100),
              u: Usuario = Depends(requiere_rol("cliente"))):
    cliente_id = u.cliente_id  # sale del token, nunca del cuerpo
    hash_solicitud = hashlib.sha256(json.dumps(s.model_dump(), sort_keys=True).encode()).hexdigest()

    with pool.connection() as conn:
        previa = _respuesta_existente(conn, cliente_id, idempotency_key, hash_solicitud, response)
    if previa:
        return previa
    if not modelo.listo:
        raise HTTPException(503, detail={"codigo": "modelo_no_disponible", "mensaje": "El modelo de riesgo no está disponible."})

    # 1. Recolección de datos por API (nunca por acceso a tablas de otro servicio).
    try:
        rc = _http_interno(f"{CLIENTES_URL}/internos/clientes/{cliente_id}")
        rp = _http_interno(f"{PRODUCTOS_URL}/internos/productos/{s.producto_id}/cotizacion")
    except httpx.HTTPError as e:
        raise HTTPException(503, detail={"codigo": "dependencia_no_disponible",
                                         "mensaje": f"No fue posible consultar Clientes o Productos: {type(e).__name__}. "
                                                    "Reintente con la misma Idempotency-Key."})
    if rp.status_code == 404:
        raise HTTPException(422, detail={"codigo": "producto_inexistente", "mensaje": "El producto no existe."})
    if rc.status_code != 200 or rp.status_code != 200:
        raise HTTPException(503, detail={"codigo": "dependencia_no_disponible",
                                         "mensaje": "Clientes o Productos respondieron con error. Reintente."})
    cliente, cotizacion = rc.json(), rp.json()
    if not cotizacion["activo"]:
        raise HTTPException(422, detail={"codigo": "producto_inactivo", "mensaje": "El producto no está disponible."})

    # El monto lo calcula el servicio con la cotización vigente de la fuente de verdad.
    monto = round(cotizacion["precio"] * s.cantidad, 2)

    # 2. Reglas de negocio previas al modelo.
    motivo_regla = None
    if not cliente["activo"]:
        motivo_regla = "Cliente inactivo"
    elif not (MONTO_MIN <= monto <= MONTO_MAX):
        motivo_regla = f"Monto {monto:,.0f} COP fuera del rango permitido [{MONTO_MIN:,} - {MONTO_MAX:,}]"
    elif any(cliente.get(c) is None for c in ("ingresos_mensuales_cop", "obligaciones_mensuales_cop",
                                               "creditos_previos", "atrasos_previos_30d")) \
            or not cliente.get("ingresos_mensuales_cop"):
        motivo_regla = "Información financiera mínima incompleta (ingresos, obligaciones e historial)"

    # 3. Evaluación de riesgo con las mismas características del entrenamiento.
    riesgo, entrada = None, None
    if motivo_regla is None:
        entrada = EntradaRiesgo(**{k: cliente.get(k) for k in (
            "ingresos_mensuales_cop", "obligaciones_mensuales_cop", "tipo_empleo", "antiguedad_laboral_meses",
            "meses_historial_crediticio", "creditos_previos", "atrasos_previos_30d", "porcentaje_pago_puntual")},
            monto_solicitado_cop=monto, plazo_meses=s.plazo_meses)
        riesgo = modelo.predecir(entrada)

    if motivo_regla:
        estado, motivo = E.RECHAZADA, f"Regla de negocio: {motivo_regla}"
    elif riesgo["decision_riesgo"] == "DESFAVORABLE":
        estado = E.RECHAZADA
        motivo = (f"Riesgo desfavorable: probabilidad de incumplimiento {riesgo['probabilidad_incumplimiento']:.3f} "
                  f">= umbral {riesgo['umbral']} (modelo {riesgo['model_version']})")
    else:
        estado, motivo = E.PENDIENTE_INVENTARIO, "Riesgo favorable; reserva de inventario solicitada"

    prestamo_id, correlation_id = uuid.uuid4(), uuid.uuid4()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO prestamos (id, cliente_id, producto_id, cantidad, plazo_meses, precio_unitario, "
                    "version_cotizacion, monto, estado, motivo, probabilidad, decision_riesgo, umbral, model_version, "
                    "model_run_id, correlation_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (prestamo_id, cliente_id, s.producto_id, s.cantidad, s.plazo_meses, cotizacion["precio"],
                     cotizacion["version"], monto, estado, motivo,
                     riesgo and riesgo["probabilidad_incumplimiento"], riesgo and riesgo["decision_riesgo"],
                     riesgo and riesgo["umbral"], riesgo and riesgo["model_version"], riesgo and riesgo["run_id"],
                     correlation_id))
                cur.execute("INSERT INTO idempotencia (cliente_id, clave, hash_solicitud, prestamo_id) "
                            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING prestamo_id",
                            (cliente_id, idempotency_key, hash_solicitud, prestamo_id))
                if cur.fetchone() is None:
                    raise _Duplicada()  # otra petición con la misma clave ganó: se deshace todo
                _registrar(cur, prestamo_id, None, estado, motivo)
                if riesgo:
                    cur.execute("INSERT INTO predicciones (prestamo_id, origen, model_version, entradas, probabilidad, "
                                "decision) VALUES (%s,'prestamo',%s,%s,%s,%s)",
                                (prestamo_id, riesgo["model_version"], Jsonb(entrada.model_dump()),
                                 riesgo["probabilidad_incumplimiento"], riesgo["decision_riesgo"]))
                if estado == E.PENDIENTE_INVENTARIO:
                    # Solo lo que Inventario necesita: nada de información financiera del cliente.
                    mq.encolar(cur, mq.crear_mensaje("ReservaSolicitada", str(prestamo_id),
                                                     {"producto_id": s.producto_id, "cantidad": s.cantidad},
                                                     str(correlation_id), "prestamos"))
        except _Duplicada:
            previa = _respuesta_existente(conn, cliente_id, idempotency_key, hash_solicitud, response)
            return previa
        p = conn.execute("SELECT * FROM prestamos WHERE id = %s", (prestamo_id,)).fetchone()
    response.status_code = 201 if estado == E.RECHAZADA else 202
    response.headers["Location"] = f"/prestamos/{prestamo_id}"
    return _serializar(p)


class _Duplicada(Exception):
    pass


def _cargar_autorizado(conn, prestamo_id: uuid.UUID, u: Usuario, bloquear: bool = False) -> dict:
    p = conn.execute(f"SELECT * FROM prestamos WHERE id = %s{' FOR UPDATE' if bloquear else ''}",
                     (prestamo_id,)).fetchone()
    if p is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Préstamo no encontrado."})
    if not u.es_admin and p["cliente_id"] != u.cliente_id:
        raise HTTPException(403, detail={"codigo": "prohibido", "mensaje": "No puede consultar préstamos de otro cliente."})
    return p


@app.get("/prestamos")
def listar(estado: str | None = None, limite: int = Query(50, ge=1, le=200), u: Usuario = Depends(usuario_actual)):
    sql, params = "SELECT * FROM prestamos WHERE TRUE", []
    if not u.es_admin:
        if u.rol != "cliente":
            raise HTTPException(403, detail={"codigo": "prohibido", "mensaje": "Sin permiso."})
        sql += " AND cliente_id = %s"
        params.append(u.cliente_id)
    if estado:
        sql += " AND estado = %s"
        params.append(estado)
    with pool.connection() as conn:
        filas = conn.execute(sql + " ORDER BY creado_en DESC LIMIT %s", [*params, limite]).fetchall()
    return [_serializar(f) for f in filas]


@app.get("/prestamos/{prestamo_id}")
def detalle(prestamo_id: uuid.UUID, u: Usuario = Depends(usuario_actual)):
    with pool.connection() as conn:
        p = _cargar_autorizado(conn, prestamo_id, u)
        d = _serializar(p)
        d["historial"] = _historial(conn, prestamo_id)
    return d


@app.get("/prestamos/{prestamo_id}/mensajes")
def mensajes(prestamo_id: uuid.UUID, _: Usuario = Depends(requiere_rol("admin"))):
    """Trazabilidad: comandos publicados (outbox) y eventos recibidos (inbox) de un préstamo."""
    with pool.connection() as conn:
        enviados = conn.execute("SELECT mensaje, publicado_en FROM outbox WHERE prestamo_id = %s ORDER BY id",
                                (prestamo_id,)).fetchall()
        recibidos = conn.execute("SELECT event_id, tipo, procesado_en, resultado FROM mensajes_procesados "
                                 "WHERE prestamo_id = %s ORDER BY procesado_en", (prestamo_id,)).fetchall()
    return {"enviados": [{"mensaje": e["mensaje"], "publicado_en": e["publicado_en"] and e["publicado_en"].isoformat()}
                         for e in enviados],
            "recibidos": [{**r, "event_id": str(r["event_id"]), "procesado_en": r["procesado_en"].isoformat()}
                          for r in recibidos]}


@app.post("/prestamos/{prestamo_id}/cancelar")
def cancelar(prestamo_id: uuid.UUID, u: Usuario = Depends(usuario_actual)):
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            p = _cargar_autorizado(conn, prestamo_id, u, bloquear=True)
            quien = "administrador" if u.es_admin else "cliente"
            if not _transicion(cur, p, "cancelar", f"Cancelación solicitada por el {quien}; liberando reserva"):
                raise HTTPException(409, detail={"codigo": "estado_no_permite",
                                                 "mensaje": f"No se puede cancelar un préstamo en estado {p['estado']}."})
            mq.encolar(cur, _mensaje("LiberacionSolicitada", p, {"producto_id": p["producto_id"],
                                                                  "cantidad": p["cantidad"], "motivo": "cancelacion"}))
        return _serializar(conn.execute("SELECT * FROM prestamos WHERE id = %s", (prestamo_id,)).fetchone())


@app.post("/prestamos/{prestamo_id}/entregar")
def entregar(prestamo_id: uuid.UUID, u: Usuario = Depends(requiere_rol("admin"))):
    """Entrega simulada: se pide a Inventario confirmar la salida; COMPLETADA llega con SalidaConfirmada."""
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            p = _cargar_autorizado(conn, prestamo_id, u, bloquear=True)
            if not _transicion(cur, p, "entregar", "Entrega solicitada por el administrador"):
                raise HTTPException(409, detail={"codigo": "estado_no_permite",
                                                 "mensaje": f"Solo se entrega un préstamo APROBADO (estado: {p['estado']})."})
            mq.encolar(cur, _mensaje("EntregaSolicitada", p, {"reserva_id": str(p["reserva_id"]),
                                                               "producto_id": p["producto_id"], "cantidad": p["cantidad"]}))
        return _serializar(conn.execute("SELECT * FROM prestamos WHERE id = %s", (prestamo_id,)).fetchone())


@app.post("/riesgo/predict")
def predecir(entrada: EntradaRiesgo, _: Usuario = Depends(requiere_rol("admin", "servicio"))):
    """Prueba el pipeline con características válidas. No crea préstamos ni reserva inventario."""
    if not modelo.listo:
        raise HTTPException(503, detail={"codigo": "modelo_no_disponible", "mensaje": "Modelo no disponible."})
    r = modelo.predecir(entrada)
    with pool.connection() as conn:
        conn.execute("INSERT INTO predicciones (origen, model_version, entradas, probabilidad, decision) "
                     "VALUES ('predict', %s, %s, %s, %s)",
                     (r["model_version"], Jsonb(entrada.model_dump()), r["probabilidad_incumplimiento"],
                      r["decision_riesgo"]))
    return r


@app.get("/modelo/info")
def modelo_info(response: Response):
    if not modelo.listo:
        response.status_code = 503
    return modelo.info()


@app.post("/modelo/recargar")
def recargar(_: Usuario = Depends(requiere_rol("admin"))):
    """Recarga explícita tras ml/promover.py (promoción o rollback)."""
    anterior = modelo.info()["model_version"]
    try:
        modelo.cargar()
    except Exception as e:
        raise HTTPException(500, detail={"codigo": "carga_fallida",
                                         "mensaje": f"No se cargó la nueva versión; sigue activa {anterior}: {e!r}"})
    return {"anterior": anterior, "activa": modelo.info()["model_version"], "info": modelo.info()}


@app.get("/modelo/monitoreo")
def monitoreo(ventana: int = Query(500, ge=30, le=5000), _: Usuario = Depends(requiere_rol("admin"))):
    """Volumen y tasa de decisiones por versión, y drift (PSI) de las entradas recientes frente a entrenamiento."""
    with pool.connection() as conn:
        por_version = conn.execute(
            "SELECT model_version, count(*) AS n, avg(probabilidad)::float AS probabilidad_media, "
            "avg((decision = 'DESFAVORABLE')::int)::float AS tasa_desfavorable, max(creado_en) AS ultima "
            "FROM predicciones GROUP BY model_version ORDER BY model_version").fetchall()
        recientes = conn.execute("SELECT entradas FROM predicciones ORDER BY id DESC LIMIT %s", (ventana,)).fetchall()
        estados = conn.execute("SELECT estado, count(*) AS n FROM prestamos GROUP BY estado ORDER BY estado").fetchall()
    for f in por_version:
        f["ultima"] = f["ultima"].isoformat()
    referencia = (modelo.manifiesto or {}).get("referencia_drift", {})
    drift = {}
    for variable, ref in referencia.items():
        valor = psi(ref, [r["entradas"].get(variable) for r in recientes])
        if valor is not None:
            drift[variable] = {"psi": valor, "alerta": "alta" if valor > 0.25 else "moderada" if valor > 0.1 else "estable"}
    return {"version_activa": modelo.info()["model_version"], "por_version": por_version, "prestamos_por_estado": estados,
            "drift": {"ventana": len(recientes), "variables": drift,
                      "criterio": "PSI < 0,1 estable; 0,1-0,25 moderado; > 0,25 alto (revisar o reentrenar)"},
            "mensajeria": estado_mq.como_dict()}
