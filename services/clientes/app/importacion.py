"""Importación masiva de clientes por CSV.

Política (documentada en docs/):
  * Clave externa única: columna `cliente_id` del CSV -> clientes.cliente_externo_id.
  * Si la clave no existe: se INSERTA.
  * Si existe y los datos financieros/laborales son iguales: SIN CAMBIOS (no se toca la fila).
  * Si existe y difieren: se ACTUALIZAN esos datos (el CSV es la fuente más reciente).
    Nunca se modifican credenciales, rol, estado activo, email ni documento de un cliente existente.
  * Filas inválidas se RECHAZAN con el detalle de errores; no afectan a las demás (savepoint por fila).
  * Columnas extra (solicitud_id, monto_solicitado_cop, ...) se ignoran: el archivo
    data/solicitudes_nuevas.csv se puede importar tal cual.
Importar dos veces el mismo archivo no duplica clientes: la segunda vez todo queda "sin cambios".
"""
from __future__ import annotations

import csv
import io
import math

CAMPOS_NUMERICOS = {
    "ingresos_mensuales_cop": (int, 0, 100_000_000, True),
    "obligaciones_mensuales_cop": (int, 0, 100_000_000, True),
    "antiguedad_laboral_meses": (float, 0, 600, False),
    "meses_historial_crediticio": (float, 0, 600, False),
    "creditos_previos": (int, 0, 100, True),
    "atrasos_previos_30d": (int, 0, 100, True),
    "porcentaje_pago_puntual": (float, 0, 100, False),
}
EMPLEOS = {"dependiente", "independiente", "temporal"}
CAMPOS_DATOS = list(CAMPOS_NUMERICOS) + ["tipo_empleo"]
MAX_FILAS = 20_000


def _numero(texto: str, tipo, nombre: str, minimo, maximo, obligatorio: bool, errores: list):
    texto = (texto or "").strip()
    if texto == "":
        if obligatorio:
            errores.append(f"{nombre} es obligatorio")
        return None
    try:
        valor = float(texto)
    except ValueError:
        errores.append(f"{nombre} no es numérico: {texto!r}")
        return None
    if math.isnan(valor) or valor < minimo or valor > maximo:
        errores.append(f"{nombre} fuera de rango [{minimo}, {maximo}]: {texto}")
        return None
    if tipo is int:
        if valor != int(valor):
            errores.append(f"{nombre} debe ser entero: {texto}")
            return None
        return int(valor)
    return round(valor, 2)


def validar_fila(fila: dict) -> tuple[dict | None, list[str]]:
    errores: list[str] = []
    clave = (fila.get("cliente_id") or "").strip()
    if not clave:
        errores.append("cliente_id es obligatorio")
    elif len(clave) > 40:
        errores.append("cliente_id demasiado largo")
    datos = {"cliente_externo_id": clave}
    for nombre, (tipo, lo, hi, obligatorio) in CAMPOS_NUMERICOS.items():
        datos[nombre] = _numero(fila.get(nombre), tipo, nombre, lo, hi, obligatorio, errores)
    empleo = (fila.get("tipo_empleo") or "").strip().lower()
    if empleo and empleo not in EMPLEOS:
        errores.append(f"tipo_empleo inválido: {empleo!r}")
    datos["tipo_empleo"] = empleo or None
    if datos.get("creditos_previos") == 0 and datos.get("porcentaje_pago_puntual") is not None:
        errores.append("porcentaje_pago_puntual debe estar vacío si no hay créditos previos")
    datos["nombre"] = (fila.get("nombre") or "").strip() or f"Cliente {clave}"
    return (None if errores else datos), errores


def leer_csv(contenido: bytes) -> list[dict]:
    texto = contenido.decode("utf-8-sig")
    lector = csv.DictReader(io.StringIO(texto))
    if not lector.fieldnames or "cliente_id" not in lector.fieldnames:
        raise ValueError("El CSV debe tener encabezado y la columna cliente_id.")
    faltan = [c for c in CAMPOS_DATOS if c not in lector.fieldnames]
    if faltan:
        raise ValueError(f"Faltan columnas: {', '.join(faltan)}")
    filas = list(lector)
    if len(filas) > MAX_FILAS:
        raise ValueError(f"Máximo {MAX_FILAS} filas por archivo.")
    return filas


def _iguales(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(float(a) - float(b)) < 1e-6


def importar(conn, filas: list[dict]) -> dict:
    resultado = {"filas": len(filas), "insertados": 0, "actualizados": 0, "sin_cambios": 0, "rechazados": 0,
                 "errores": []}
    vistos: set[str] = set()
    for numero, fila in enumerate(filas, start=2):  # fila 1 = encabezado
        datos, errores = validar_fila(fila)
        clave = (fila.get("cliente_id") or "").strip()
        if datos and clave in vistos:
            errores = [f"cliente_id repetido dentro del archivo: {clave}"]
            datos = None
        if datos is None:
            resultado["rechazados"] += 1
            resultado["errores"].append({"fila": numero, "cliente_id": clave or None, "errores": errores})
            continue
        vistos.add(clave)
        try:
            with conn.transaction():  # savepoint: un error en esta fila no aborta las demás
                actual = conn.execute(
                    f"SELECT id, {', '.join(CAMPOS_DATOS)} FROM clientes WHERE cliente_externo_id = %s FOR UPDATE",
                    (clave,)).fetchone()
                if actual is None:
                    columnas = ["cliente_externo_id", "nombre", *CAMPOS_DATOS, "origen"]
                    valores = [datos["cliente_externo_id"], datos["nombre"], *(datos[c] for c in CAMPOS_DATOS),
                               "importacion"]
                    conn.execute(f"INSERT INTO clientes ({', '.join(columnas)}) VALUES ({', '.join(['%s'] * len(columnas))})",
                                 valores)
                    resultado["insertados"] += 1
                elif all(_iguales(actual[c], datos[c]) for c in CAMPOS_DATOS):
                    resultado["sin_cambios"] += 1
                else:
                    sets = ", ".join(f"{c} = %s" for c in CAMPOS_DATOS)
                    conn.execute(f"UPDATE clientes SET {sets}, actualizado_en = now() WHERE id = %s",
                                 [*(datos[c] for c in CAMPOS_DATOS), actual["id"]])
                    resultado["actualizados"] += 1
        except Exception as e:  # p. ej. violación de una restricción
            resultado["rechazados"] += 1
            resultado["errores"].append({"fila": numero, "cliente_id": clave, "errores": [f"error de base de datos: {e}"]})
    return resultado
