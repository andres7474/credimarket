"""Pruebas de aceptación de CrediMarket (sección 12 del enunciado) contra el sistema levantado.

Uso (con docker compose up ya corriendo, desde la raíz del repositorio):
    python scripts/pruebas/aceptacion.py               # todas
    python scripts/pruebas/aceptacion.py 4 5 7         # solo algunas
    python scripts/pruebas/aceptacion.py --rapido      # sin esperar la expiración de la caché

Las pruebas 6 y 8 detienen y arrancan contenedores con `docker compose stop/start`.
La 11 cambia la versión promovida (v2 -> v1 -> v2). La 17 deja 2 mensajes en credimarket.dlq.
La 18 (vencimiento) solo se ejecuta si RESERVA_TTL_SEGUNDOS <= 120.
Las credenciales se leen de .env (nunca están en el código).
Requisitos: pip install requests pika
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import csv
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pika
import requests

RAIZ = Path(__file__).resolve().parents[2]


def leer_env() -> dict:
    valores = {}
    ruta = RAIZ / ".env"
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            if "=" in linea and not linea.strip().startswith("#"):
                k, v = linea.split("=", 1)
                valores[k.strip()] = v.strip()
    claves = {"JWT_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD", "DEMO_CLIENTE_PASSWORD", "RABBITMQ_USER", "RABBITMQ_PASSWORD"}
    valores.update({k: v for k, v in os.environ.items() if k in claves or k.endswith("_URL")})
    return valores


ENV = leer_env()
URL = {s: ENV.get(f"{s.upper()}_URL", f"http://localhost:{p}")
       for s, p in (("productos", 8001), ("clientes", 8002), ("prestamos", 8003), ("inventario", 8004))}
RABBIT = ENV.get("RABBITMQ_HOST_URL",
                 f"amqp://{ENV.get('RABBITMQ_USER', 'guest')}:{ENV.get('RABBITMQ_PASSWORD', 'guest')}@localhost:5672/%2F")
DETENER = os.environ.get("CMD_DETENER", "docker compose stop {servicio}")
INICIAR = os.environ.get("CMD_INICIAR", "docker compose start {servicio}")
RAPIDO = "--rapido" in sys.argv
resultados: list[tuple[str, bool, str]] = []


# ------------------------------------------------------------------ utilidades

def login(email: str, password: str) -> dict:
    r = requests.post(f"{URL['clientes']}/auth/login", json={"email": email, "password": password}, timeout=5)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


ADMIN = login(ENV.get("ADMIN_EMAIL", "admin@credimarket.co"), ENV["ADMIN_PASSWORD"])
ANA = login("ana@demo.credimarket.co", ENV["DEMO_CLIENTE_PASSWORD"])
BRUNO = login("bruno@demo.credimarket.co", ENV["DEMO_CLIENTE_PASSWORD"])
CARLA = login("carla@demo.credimarket.co", ENV["DEMO_CLIENTE_PASSWORD"])


def solicitar(quien, producto_id, cantidad=1, plazo=12, clave=None):
    clave = clave or str(uuid.uuid4())
    return requests.post(f"{URL['prestamos']}/prestamos", headers={**quien, "Idempotency-Key": clave},
                         json={"producto_id": producto_id, "cantidad": cantidad, "plazo_meses": plazo}, timeout=10)


def prestamo(quien, pid):
    return requests.get(f"{URL['prestamos']}/prestamos/{pid}", headers=quien, timeout=5).json()


def esperar(quien, pid, estados, limite=30):
    fin = time.time() + limite
    while time.time() < fin:
        p = prestamo(quien, pid)
        if p["estado"] in estados:
            return p
        time.sleep(0.5)
    return prestamo(quien, pid)


def existencia(producto_id):
    return requests.get(f"{URL['inventario']}/existencias/{producto_id}", timeout=5).json()


def reservas_de(pid):
    return [r for r in requests.get(f"{URL['inventario']}/reservas?limite=500", headers=ADMIN, timeout=5).json()
            if r["prestamo_id"] == pid]


def publicar(mensaje: dict | bytes) -> None:
    """Publica directamente en RabbitMQ (simula reentregas, eventos tardíos o mensajes corruptos)."""
    rutas = {"ReservaSolicitada": ("credimarket.comandos", "inventario.reservar"),
             "LiberacionSolicitada": ("credimarket.comandos", "inventario.liberar"),
             "InventarioReservado": ("credimarket.eventos", "inventario.reservado")}
    tipo = mensaje.get("type") if isinstance(mensaje, dict) else None
    exchange, rk = rutas.get(tipo, ("credimarket.eventos", "inventario.reservado"))
    conexion = pika.BlockingConnection(pika.URLParameters(RABBIT))
    canal = conexion.channel()
    canal.confirm_delivery()
    cuerpo = mensaje if isinstance(mensaje, (bytes, bytearray)) else json.dumps(mensaje).encode()
    canal.basic_publish(exchange, rk, cuerpo, properties=pika.BasicProperties(delivery_mode=2), mandatory=True)
    conexion.close()


def mensajes_dlq() -> int:
    conexion = pika.BlockingConnection(pika.URLParameters(RABBIT))
    n = conexion.channel().queue_declare("credimarket.dlq", passive=True).method.message_count
    conexion.close()
    return n


def sh(plantilla: str, servicio: str) -> None:
    subprocess.run(plantilla.format(servicio=servicio), shell=True, check=True, cwd=RAIZ,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def esperar_listo(servicio: str, limite=60) -> None:
    fin = time.time() + limite
    while time.time() < fin:
        try:
            if requests.get(f"{URL[servicio]}/ready", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)


def caso(numero: int, titulo: str):
    def decorador(funcion):
        funcion.numero, funcion.titulo = numero, titulo
        return funcion
    return decorador


def verificar(condicion: bool, detalle: str) -> None:
    if not condicion:
        raise AssertionError(detalle)
    print(f"     ✓ {detalle}")


def producto_sin_stock() -> int:
    r = requests.post(f"{URL['productos']}/productos", headers=ADMIN, timeout=5, json={
        "sku": f"SIN-STOCK-{uuid.uuid4().hex[:6]}", "nombre": "Producto sin existencias", "categoria": "otros",
        "precio": 2_000_000})
    return r.json()["id"]


# ------------------------------------------------------------------ casos

@caso(1, "Riesgo favorable con existencias: reserva, aprobación y entrega")
def c1():
    antes = existencia(1)
    r = solicitar(ANA, 1)
    verificar(r.status_code == 202 and r.json()["estado"] == "PENDIENTE_INVENTARIO", "HTTP 202 y PENDIENTE_INVENTARIO")
    pid = r.json()["id"]
    p = esperar(ANA, pid, {"APROBADA"})
    verificar(p["estado"] == "APROBADA" and p["reserva_id"], "APROBADA solo después de la reserva confirmada")
    verificar(existencia(1)["reservado"] == antes["reservado"] + 1, "una unidad pasó a reservado")
    requests.post(f"{URL['prestamos']}/prestamos/{pid}/entregar", headers=ADMIN, timeout=5).raise_for_status()
    p = esperar(ANA, pid, {"COMPLETADA"})
    verificar(p["estado"] == "COMPLETADA", "COMPLETADA tras SalidaConfirmada")
    despues = existencia(1)
    verificar(despues["disponible"] == antes["disponible"] - 1 and despues["reservado"] == antes["reservado"],
              f"existencias coherentes: disponible {antes['disponible']} -> {despues['disponible']}")


@caso(2, "Solicitud rechazada por riesgo: decisión y versión registradas, sin reserva")
def c2():
    r = solicitar(BRUNO, 1)
    p = r.json()
    verificar(r.status_code == 201 and p["estado"] == "RECHAZADA", "RECHAZADA inmediatamente (HTTP 201)")
    verificar(p["model_version"] and p["probabilidad"] >= p["umbral"],
              f"probabilidad {p['probabilidad']:.3f} >= umbral {p['umbral']} con modelo {p['model_version']}")
    time.sleep(1)
    verificar(reservas_de(p["id"]) == [], "Inventario no tiene ninguna reserva para este préstamo")


@caso(3, "Riesgo favorable sin existencias: CANCELADA_SIN_STOCK, sin inventario negativo")
def c3():
    pid_producto = producto_sin_stock()
    r = solicitar(ANA, pid_producto)
    p = esperar(ANA, r.json()["id"], {"CANCELADA_SIN_STOCK"})
    verificar(p["estado"] == "CANCELADA_SIN_STOCK", "CANCELADA_SIN_STOCK")
    e = existencia(pid_producto)
    verificar(e["disponible"] >= 0 and e["reservado"] == 0, f"existencias no negativas: {e['disponible']}")


@caso(4, "Dos solicitudes simultáneas por la última unidad: solo una obtiene la reserva")
def c4():
    e = existencia(6)
    if e["disponible"] != 1:
        print(f"     (ajuste: el producto 6 tiene {e['disponible']} disponibles; se esperaba 1)")
    with cf.ThreadPoolExecutor(2) as ex:
        r1, r2 = ex.map(lambda q: solicitar(q, 6), [ANA, CARLA])
    finales = [esperar(ANA, r1.json()["id"], {"APROBADA", "CANCELADA_SIN_STOCK"})["estado"],
               esperar(CARLA, r2.json()["id"], {"APROBADA", "CANCELADA_SIN_STOCK"})["estado"]]
    verificar(sorted(finales) == ["APROBADA", "CANCELADA_SIN_STOCK"], f"resultados: {finales}")
    verificar(existencia(6)["disponible"] == 0, "disponible quedó en 0, nunca negativo")
    ganador, quien = (r1, ANA) if finales[0] == "APROBADA" else (r2, CARLA)
    requests.post(f"{URL['prestamos']}/prestamos/{ganador.json()['id']}/cancelar", headers=quien, timeout=5)
    esperar(quien, ganador.json()["id"], {"CANCELADA"})
    verificar(existencia(6)["disponible"] == 1, "se cancela el ganador para dejar la unidad lista para repetir")


@caso(5, "Misma petición y mismo evento repetidos: un préstamo y un único efecto")
def c5():
    clave = str(uuid.uuid4())
    antes = existencia(2)
    with cf.ThreadPoolExecutor(5) as ex:
        respuestas = list(ex.map(lambda _: solicitar(ANA, 2, clave=clave), range(5)))
    ids = {r.json()["id"] for r in respuestas}
    verificar(len(ids) == 1, f"5 peticiones concurrentes con la misma Idempotency-Key -> 1 préstamo ({ids.pop()})")
    pid = respuestas[0].json()["id"]
    distinto = solicitar(ANA, 2, cantidad=2, clave=clave)
    verificar(distinto.status_code == 409, "misma clave con contenido distinto -> HTTP 409")
    esperar(ANA, pid, {"APROBADA"})
    comando = next(m["mensaje"] for m in requests.get(f"{URL['prestamos']}/prestamos/{pid}/mensajes",
                                                       headers=ADMIN, timeout=5).json()["enviados"]
                   if m["mensaje"]["type"] == "ReservaSolicitada")
    publicar(comando)                                   # mismo event_id
    otro = copy.deepcopy(comando)
    otro["event_id"] = str(uuid.uuid4())
    publicar(otro)                                      # mismo préstamo, otro event_id
    evento = next(m["mensaje"] for m in requests.get(f"{URL['inventario']}/mensajeria/outbox?prestamo_id={pid}",
                                                      headers=ADMIN, timeout=5).json()
                  if m["mensaje"]["type"] == "InventarioReservado")
    publicar(evento)                                    # evento repetido hacia Préstamos
    time.sleep(3)
    verificar(len(reservas_de(pid)) == 1, "una sola reserva en Inventario")
    verificar(existencia(2)["reservado"] == antes["reservado"] + 1, "el stock se reservó una sola vez")
    recibidos = requests.get(f"{URL['prestamos']}/prestamos/{pid}/mensajes", headers=ADMIN, timeout=5).json()["recibidos"]
    verificar(len([r for r in recibidos if r["tipo"] == "InventarioReservado"]) == 1,
              "Préstamos registró el evento InventarioReservado una sola vez")
    requests.post(f"{URL['prestamos']}/prestamos/{pid}/cancelar", headers=ANA, timeout=5)
    esperar(ANA, pid, {"CANCELADA"})


@caso(6, "Caída del consumidor y del broker: recuperación sin pérdidas ni duplicados")
def c6():
    sh(DETENER, "inventario")
    r = solicitar(CARLA, 3)
    pid = r.json()["id"]
    verificar(r.status_code == 202, "con Inventario caído la solicitud se acepta (202) y queda pendiente")
    time.sleep(3)
    verificar(prestamo(CARLA, pid)["estado"] == "PENDIENTE_INVENTARIO", "sigue PENDIENTE_INVENTARIO (comando en la cola)")
    sh(INICIAR, "inventario")
    esperar_listo("inventario")
    verificar(esperar(CARLA, pid, {"APROBADA"}, 60)["estado"] == "APROBADA", "al volver Inventario se aprueba")
    sh(DETENER, "rabbitmq")
    r2 = solicitar(CARLA, 3)
    pid2 = r2.json()["id"]
    verificar(r2.status_code == 202, "con RabbitMQ caído la solicitud se guarda (outbox) y responde 202")
    sh(INICIAR, "rabbitmq")
    verificar(esperar(CARLA, pid2, {"APROBADA"}, 120)["estado"] == "APROBADA",
              "al volver RabbitMQ el outbox publica y el préstamo se aprueba")
    verificar(len(reservas_de(pid)) == 1 and len(reservas_de(pid2)) == 1, "una reserva por préstamo, sin duplicados")
    for p in (pid, pid2):
        requests.post(f"{URL['prestamos']}/prestamos/{p}/cancelar", headers=CARLA, timeout=5)


@caso(7, "Cancelación y evento tardío: reserva liberada y préstamo no reactivado")
def c7():
    antes = existencia(1)
    pid = solicitar(ANA, 1).json()["id"]
    esperar(ANA, pid, {"APROBADA"})
    c = requests.post(f"{URL['prestamos']}/prestamos/{pid}/cancelar", headers=ANA, timeout=5).json()
    verificar(c["estado"] == "CANCELACION_PENDIENTE", "pasa a CANCELACION_PENDIENTE mientras se libera")
    verificar(esperar(ANA, pid, {"CANCELADA"})["estado"] == "CANCELADA", "CANCELADA tras ReservaLiberada")
    verificar(existencia(1)["disponible"] == antes["disponible"], "la unidad volvió a disponible")
    tardio = next(m["mensaje"] for m in requests.get(f"{URL['inventario']}/mensajeria/outbox?prestamo_id={pid}",
                                                      headers=ADMIN, timeout=5).json()
                  if m["mensaje"]["type"] == "InventarioReservado")
    tardio["event_id"] = str(uuid.uuid4())   # un evento nuevo que llega tarde
    publicar(tardio)
    time.sleep(2)
    p = prestamo(ANA, pid)
    verificar(p["estado"] == "CANCELADA", "el InventarioReservado tardío no reactiva el préstamo")
    verificar(any(not h["aplicado"] for h in p["historial"]), "el historial registra el evento como ignorado")


@caso(8, "Redis: miss, hit, expiración, invalidación y caída")
def c8():
    requests.post(f"{URL['productos']}/cache/metricas/reiniciar", headers=ADMIN, timeout=5)
    requests.put(f"{URL['productos']}/productos/2", headers=ADMIN, json={"descripcion": "6,5 pulgadas, 256 GB"}, timeout=5)
    a = requests.get(f"{URL['productos']}/productos/2", timeout=5).headers["X-Cache"]
    b = requests.get(f"{URL['productos']}/productos/2", timeout=5).headers["X-Cache"]
    verificar((a, b) == ("MISS", "HIT"), f"primera consulta {a}, segunda {b}")
    ttl = requests.get(f"{URL['productos']}/cache/productos/2", headers=ADMIN, timeout=5).json()["ttl_restante_s"]
    verificar(ttl and ttl > 0, f"la clave productos:v1:2 tiene TTL {ttl} s")
    if not RAPIDO:
        time.sleep(ttl + 1)
        c = requests.get(f"{URL['productos']}/productos/2", timeout=5).headers["X-Cache"]
        verificar(c == "MISS", "tras el TTL la entrada expiró (MISS)")
    sh(DETENER, "redis")
    t = time.time()
    r = requests.get(f"{URL['productos']}/productos/2", timeout=5)
    verificar(r.status_code == 200 and r.headers["X-Cache"] == "OMITIDA",
              f"con Redis caído responde desde la BD en {1000 * (time.time() - t):.0f} ms")
    sh(INICIAR, "redis")
    time.sleep(7)
    requests.get(f"{URL['productos']}/productos/2", timeout=5)
    verificar(requests.get(f"{URL['productos']}/productos/2", timeout=5).headers["X-Cache"] == "HIT",
              "al volver Redis la caché se recupera")
    print("     métricas:", json.dumps(requests.get(f"{URL['productos']}/cache/metricas", headers=ADMIN, timeout=5).json()))


@caso(9, "Precio modificado: invalidación y monto con la cotización vigente")
def c9():
    actual = requests.get(f"{URL['productos']}/productos/3", timeout=5).json()
    requests.get(f"{URL['productos']}/productos/3", timeout=5)  # queda en caché
    nuevo = actual["precio"] + 100_000
    act = requests.put(f"{URL['productos']}/productos/3", headers=ADMIN, json={"precio": nuevo}, timeout=5).json()
    r = requests.get(f"{URL['productos']}/productos/3", timeout=5)
    verificar(r.headers["X-Cache"] == "MISS" and r.json()["precio"] == nuevo,
              f"tras el cambio la caché se invalidó: precio {actual['precio']:,.0f} -> {nuevo:,.0f}")
    p = solicitar(CARLA, 3).json()
    verificar(p["precio_unitario"] == nuevo and p["version_cotizacion"] == act["version"],
              f"el préstamo usó el precio vigente y la versión {act['version']} de la cotización")
    requests.post(f"{URL['prestamos']}/prestamos/{p['id']}/cancelar", headers=CARLA, timeout=5)


@caso(10, "Modelo exportado vs. API: predicciones equivalentes y versión identificable")
def c10():
    info = requests.get(f"{URL['prestamos']}/modelo/info", timeout=5).json()
    version = info["model_version"]
    referencia = {r["solicitud_id"]: float(r["probabilidad_incumplimiento"]) for r in
                  csv.DictReader(open(RAIZ / "models" / version / "predicciones_referencia.csv", encoding="utf-8"))}
    filas = list(csv.DictReader(open(RAIZ / "data" / "solicitudes_nuevas.csv", encoding="utf-8")))[:25]
    campos = ["ingresos_mensuales_cop", "obligaciones_mensuales_cop", "tipo_empleo", "antiguedad_laboral_meses",
              "meses_historial_crediticio", "creditos_previos", "atrasos_previos_30d", "porcentaje_pago_puntual",
              "monto_solicitado_cop", "plazo_meses"]
    diferencia = 0.0
    for f in filas:
        entrada = {c: (None if f[c] == "" else (f[c] if c == "tipo_empleo" else float(f[c]))) for c in campos}
        for c in ("creditos_previos", "atrasos_previos_30d", "plazo_meses"):
            entrada[c] = int(entrada[c])
        r = requests.post(f"{URL['prestamos']}/riesgo/predict", headers=ADMIN, json=entrada, timeout=5).json()
        diferencia = max(diferencia, abs(r["probabilidad_incumplimiento"] - referencia[f["solicitud_id"]]))
        assert r["model_version"] == version
    verificar(diferencia < 1e-5, f"25 solicitudes: diferencia máxima {diferencia:.2e} con la versión {version} "
                                 f"(run {info['run_id']})")


@caso(11, "Promoción y rollback entre dos versiones con trazabilidad")
def c11():
    antes = requests.get(f"{URL['prestamos']}/modelo/info", timeout=5).json()["model_version"]
    otra = "v1" if antes == "v2" else "v2"
    entrada = {"ingresos_mensuales_cop": 3_000_000, "obligaciones_mensuales_cop": 900_000, "tipo_empleo": "independiente",
               "antiguedad_laboral_meses": 12, "meses_historial_crediticio": 30, "creditos_previos": 2,
               "atrasos_previos_30d": 1, "porcentaje_pago_puntual": 80, "monto_solicitado_cop": 4_000_000,
               "plazo_meses": 24}
    p_antes = requests.post(f"{URL['prestamos']}/riesgo/predict", headers=ADMIN, json=entrada, timeout=5).json()
    for version, motivo in ((otra, "prueba de aceptación: rollback"), (antes, "prueba de aceptación: restaurar")):
        subprocess.run([sys.executable, "ml/promover.py", "--version", version, "--motivo", motivo], cwd=RAIZ, check=True,
                       stdout=subprocess.DEVNULL)
        r = requests.post(f"{URL['prestamos']}/modelo/recargar", headers=ADMIN, timeout=10).json()
        verificar(r["activa"] == version, f"{r['anterior']} -> {r['activa']} (run {r['info']['run_id']})")
        if version == otra:
            p = requests.post(f"{URL['prestamos']}/riesgo/predict", headers=ADMIN, json=entrada, timeout=5).json()
            verificar(p["probabilidad_incumplimiento"] != p_antes["probabilidad_incumplimiento"],
                      f"la misma entrada da {p_antes['probabilidad_incumplimiento']:.3f} con {antes} y "
                      f"{p['probabilidad_incumplimiento']:.3f} con {otra}")
    historial = (RAIZ / "models" / "historial_promociones.jsonl").read_text(encoding="utf-8").strip().splitlines()
    verificar(len(historial) >= 2, f"historial_promociones.jsonl tiene {len(historial)} cambios registrados")


@caso(12, "Importar dos veces el mismo CSV no duplica clientes")
def c12():
    ruta = RAIZ / "data" / "solicitudes_nuevas.csv"
    resumenes = []
    for _ in range(2):
        with open(ruta, "rb") as f:
            resumenes.append(requests.post(f"{URL['clientes']}/clientes/importar", headers=ADMIN, timeout=30,
                                           files={"archivo": ("solicitudes_nuevas.csv", f, "text/csv")}).json())
    a, b = resumenes
    print(f"     primera: insertados={a['insertados']} actualizados={a['actualizados']} sin_cambios={a['sin_cambios']} "
          f"rechazados={a['rechazados']}")
    verificar(b["insertados"] == 0 and b["actualizados"] == 0 and b["sin_cambios"] == a["filas"] - a["rechazados"],
              f"segunda: 0 insertados, {b['sin_cambios']} sin cambios")
    with open(RAIZ / "data" / "clientes_con_errores.csv", "rb") as f:
        e = requests.post(f"{URL['clientes']}/clientes/importar", headers=ADMIN, timeout=30,
                          files={"archivo": ("clientes_con_errores.csv", f, "text/csv")}).json()
    verificar(e["rechazados"] > 0 and all("fila" in x for x in e["errores"]),
              f"archivo con errores: {e['rechazados']} filas rechazadas con detalle por fila")


@caso(13, "Un cliente no puede ver préstamos ajenos")
def c13():
    pid = solicitar(BRUNO, 1).json()["id"]
    r = requests.get(f"{URL['prestamos']}/prestamos/{pid}", headers=CARLA, timeout=5)
    verificar(r.status_code == 403, "Carla consulta un préstamo de Bruno -> HTTP 403")
    r = requests.post(f"{URL['prestamos']}/prestamos/{pid}/cancelar", headers=CARLA, timeout=5)
    verificar(r.status_code == 403, "y tampoco puede cancelarlo -> HTTP 403")
    r = requests.post(f"{URL['prestamos']}/riesgo/predict", headers=CARLA, json={}, timeout=5)
    verificar(r.status_code == 403, "un cliente no puede usar el endpoint interno /riesgo/predict -> HTTP 403")


@caso(16, "Entradas inválidas y sesión vencida (nivel API)")
def c16():
    r = solicitar(ANA, 1, cantidad=0, plazo=7)
    verificar(r.status_code == 422 and len(r.json()["detail"]["errores"]) == 2, "formulario inválido -> 422 con 2 errores")
    import jwt  # PyJWT
    vencido = jwt.encode({"iss": "credimarket", "sub": "1", "rol": "cliente", "cliente_id": 1,
                          "iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600}, ENV["JWT_SECRET"], "HS256")
    r = requests.get(f"{URL['prestamos']}/prestamos", headers={"Authorization": f"Bearer {vencido}"}, timeout=5)
    verificar(r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_vencida", "token vencido -> 401 sesion_vencida")


@caso(17, "Dead letter: mensajes inválidos o que fallan siempre terminan en el DLQ")
def c17():
    antes = mensajes_dlq()
    publicar(b"esto no es JSON")
    fantasma = {"event_id": str(uuid.uuid4()), "type": "InventarioReservado", "schema_version": 1,
                "occurred_at": "2026-01-01T00:00:00Z", "correlation_id": str(uuid.uuid4()),
                "prestamo_id": str(uuid.uuid4()), "payload": {"reserva_id": str(uuid.uuid4())}}
    publicar(fantasma)  # préstamo inexistente: el manejador falla en cada intento
    fin = time.time() + 30
    while time.time() < fin and mensajes_dlq() < antes + 2:
        time.sleep(1)
    verificar(mensajes_dlq() >= antes + 2, f"DLQ: {antes} -> {mensajes_dlq()} (inválido directo; fallido tras reintentos)")


@caso(18, "Vencimiento de la reserva (requiere RESERVA_TTL_SEGUNDOS <= 120 en .env)")
def c18():
    from datetime import datetime, timezone
    antes = existencia(5)
    pid = solicitar(ANA, 5).json()["id"]
    p = esperar(ANA, pid, {"APROBADA"})
    restante = (datetime.fromisoformat(p["reserva_expira_en"]) - datetime.now(timezone.utc)).total_seconds()
    if restante > 120:
        requests.post(f"{URL['prestamos']}/prestamos/{pid}/cancelar", headers=ANA, timeout=5)
        print(f"     (omitida: la reserva vence en {restante:.0f} s; use RESERVA_TTL_SEGUNDOS=60 y reinicie inventario)")
        return
    p = esperar(ANA, pid, {"VENCIDA"}, restante + 30)
    verificar(p["estado"] == "VENCIDA", f"sin entrega, a los {restante:.0f} s el préstamo pasó a VENCIDA ({p['motivo']})")
    verificar(existencia(5)["disponible"] == antes["disponible"], "las unidades volvieron a disponible")
    verificar([r["estado"] for r in reservas_de(pid)] == ["VENCIDA"], "la reserva quedó VENCIDA en Inventario")


CASOS = [c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c16, c17, c18]


def main() -> None:
    pedidos = {int(a) for a in sys.argv[1:] if a.isdigit()}
    for funcion in CASOS:
        if pedidos and funcion.numero not in pedidos:
            continue
        print(f"\n[{funcion.numero}] {funcion.titulo}")
        try:
            funcion()
            resultados.append((f"{funcion.numero}", True, ""))
        except Exception as e:
            print(f"     ✗ {e!r}")
            resultados.append((f"{funcion.numero}", False, repr(e)))
    print("\nResumen:", " ".join(f"{n}:{'OK' if ok else 'FALLA'}" for n, ok, _ in resultados))
    sys.exit(0 if all(ok for _, ok, _ in resultados) else 1)


if __name__ == "__main__":
    main()
