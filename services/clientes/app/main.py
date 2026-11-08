"""Servicio de Clientes: registro, autenticación, datos financieros/laborales e importación CSV.

No ejecuta el modelo ni modifica inventario. Emite los tokens de la demo (no hay quinto servicio).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, EmailStr, Field, model_validator

from credimarket_comun.auth import Usuario, emitir_token, requiere_rol, usuario_actual
from credimarket_comun.db import aplicar_esquema, crear_pool, db_lista
from credimarket_comun.web import configurar_app, configurar_logs

from .importacion import CAMPOS_DATOS, importar, leer_csv

configurar_logs()
SESION_MINUTOS = int(os.environ.get("SESION_MINUTOS", "30"))
COLUMNAS = ("id, cliente_externo_id, nombre, documento, email, activo, ingresos_mensuales_cop, "
            "obligaciones_mensuales_cop, tipo_empleo, antiguedad_laboral_meses, meses_historial_crediticio, "
            "creditos_previos, atrasos_previos_30d, porcentaje_pago_puntual, origen, actualizado_en")

# Perfiles de la demo (datos ficticios). La contraseña sale de DEMO_CLIENTE_PASSWORD (.env).
CLIENTES_DEMO = [
    {"email": "ana@demo.credimarket.co", "nombre": "Ana Restrepo", "documento": "1000000001",
     "cliente_externo_id": "DEMO-ANA", "ingresos_mensuales_cop": 9_500_000, "obligaciones_mensuales_cop": 600_000,
     "tipo_empleo": "dependiente", "antiguedad_laboral_meses": 84, "meses_historial_crediticio": 120,
     "creditos_previos": 5, "atrasos_previos_30d": 0, "porcentaje_pago_puntual": 98.5},
    {"email": "bruno@demo.credimarket.co", "nombre": "Bruno Gaviria", "documento": "1000000002",
     "cliente_externo_id": "DEMO-BRUNO", "ingresos_mensuales_cop": 1_600_000, "obligaciones_mensuales_cop": 1_100_000,
     "tipo_empleo": "temporal", "antiguedad_laboral_meses": 2, "meses_historial_crediticio": 10,
     "creditos_previos": 3, "atrasos_previos_30d": 5, "porcentaje_pago_puntual": 45.0},
    {"email": "carla@demo.credimarket.co", "nombre": "Carla Mejía", "documento": "1000000003",
     "cliente_externo_id": "DEMO-CARLA", "ingresos_mensuales_cop": 8_200_000, "obligaciones_mensuales_cop": 900_000,
     "tipo_empleo": "dependiente", "antiguedad_laboral_meses": 60, "meses_historial_crediticio": 96,
     "creditos_previos": 4, "atrasos_previos_30d": 0, "porcentaje_pago_puntual": 96.0},
]

pool = None


def hash_password(password: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), sal, 200_000)
    return f"pbkdf2_sha256$200000${sal.hex()}${h.hex()}"


def verificar_password(password: str, guardado: str) -> bool:
    try:
        _, iteraciones, sal, h = guardado.split("$")
        calculado = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(sal), int(iteraciones))
        return hmac.compare_digest(calculado.hex(), h)
    except Exception:
        return False


def _sembrar(conn) -> None:
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@credimarket.co")
    admin_pw = os.environ.get("ADMIN_PASSWORD")
    demo_pw = os.environ.get("DEMO_CLIENTE_PASSWORD")
    if admin_pw and conn.execute("SELECT 1 FROM usuarios WHERE email = %s", (admin_email,)).fetchone() is None:
        conn.execute("INSERT INTO usuarios (email, password_hash, rol, nombre) VALUES (%s, %s, 'admin', 'Administrador')",
                     (admin_email, hash_password(admin_pw)))
    if not demo_pw:
        return
    for c in CLIENTES_DEMO:
        if conn.execute("SELECT 1 FROM usuarios WHERE email = %s", (c["email"],)).fetchone():
            continue
        columnas = ["cliente_externo_id", "nombre", "documento", "email", *CAMPOS_DATOS, "origen"]
        valores = [c["cliente_externo_id"], c["nombre"], c["documento"], c["email"], *(c[k] for k in CAMPOS_DATOS), "demo"]
        fila = conn.execute(f"INSERT INTO clientes ({', '.join(columnas)}) VALUES ({', '.join(['%s'] * len(columnas))}) "
                            "RETURNING id", valores).fetchone()
        conn.execute("INSERT INTO usuarios (email, password_hash, rol, cliente_id, nombre) VALUES (%s,%s,'cliente',%s,%s)",
                     (c["email"], hash_password(demo_pw), fila["id"], c["nombre"]))


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    global pool
    pool = crear_pool()
    aplicar_esquema(pool, Path(__file__).with_name("esquema.sql"))
    with pool.connection() as conn:
        _sembrar(conn)
    yield
    pool.close()


app = FastAPI(title="CrediMarket · Clientes", version="1.0.0", lifespan=ciclo_vida)
configurar_app(app)


class DatosFinancieros(BaseModel):
    ingresos_mensuales_cop: int = Field(ge=0, le=100_000_000)
    obligaciones_mensuales_cop: int = Field(ge=0, le=100_000_000)
    tipo_empleo: Literal["dependiente", "independiente", "temporal"]
    antiguedad_laboral_meses: float = Field(ge=0, le=600)
    meses_historial_crediticio: float = Field(ge=0, le=600)
    creditos_previos: int = Field(ge=0, le=100)
    atrasos_previos_30d: int = Field(ge=0, le=100)
    porcentaje_pago_puntual: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _historial(self):
        if self.creditos_previos == 0 and self.porcentaje_pago_puntual is not None:
            raise ValueError("porcentaje_pago_puntual debe estar vacío si no hay créditos previos")
        return self


class Registro(DatosFinancieros):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nombre: str = Field(min_length=2, max_length=120)
    documento: str = Field(pattern=r"^[0-9]{6,12}$")


class EdicionCliente(DatosFinancieros):
    nombre: str = Field(min_length=2, max_length=120)


class Login(BaseModel):
    email: EmailStr
    password: str


def _serializar(f: dict) -> dict:
    d = dict(f)
    for k in ("antiguedad_laboral_meses", "meses_historial_crediticio", "porcentaje_pago_puntual"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    if d.get("actualizado_en"):
        d["actualizado_en"] = d["actualizado_en"].isoformat()
    return d


def _obtener(cliente_id: int) -> dict:
    with pool.connection() as conn:
        f = conn.execute(f"SELECT {COLUMNAS} FROM clientes WHERE id = %s", (cliente_id,)).fetchone()
    if f is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Cliente no encontrado."})
    return _serializar(f)


@app.get("/health")
def health():
    return {"estado": "ok", "servicio": "clientes"}


@app.get("/ready")
def ready(response: Response):
    ok = db_lista(pool)
    if not ok:
        response.status_code = 503
    return {"listo": ok, "base_datos": ok}


@app.post("/auth/login")
def login(d: Login):
    with pool.connection() as conn:
        u = conn.execute("SELECT u.*, c.activo FROM usuarios u LEFT JOIN clientes c ON c.id = u.cliente_id "
                         "WHERE u.email = %s", (d.email.lower(),)).fetchone()
    if u is None or not verificar_password(d.password, u["password_hash"]):
        raise HTTPException(401, detail={"codigo": "credenciales_invalidas", "mensaje": "Correo o contraseña incorrectos."})
    if u["rol"] == "cliente" and not u["activo"]:
        raise HTTPException(403, detail={"codigo": "cliente_inactivo", "mensaje": "El cliente está inactivo."})
    return emitir_token(str(u["id"]), u["rol"], SESION_MINUTOS, cliente_id=u["cliente_id"], nombre=u["nombre"])


@app.post("/auth/registro", status_code=201)
def registro(d: Registro):
    email = d.email.lower()
    with pool.connection() as conn:
        with conn.transaction():
            if conn.execute("SELECT 1 FROM usuarios WHERE email = %s", (email,)).fetchone():
                raise HTTPException(409, detail={"codigo": "email_registrado", "mensaje": "Ese correo ya está registrado."})
            if conn.execute("SELECT 1 FROM clientes WHERE documento = %s", (d.documento,)).fetchone():
                raise HTTPException(409, detail={"codigo": "documento_registrado", "mensaje": "Ese documento ya está registrado."})
            columnas = ["cliente_externo_id", "nombre", "documento", "email", *CAMPOS_DATOS, "origen"]
            valores = [f"WEB-{d.documento}", d.nombre, d.documento, email, *(getattr(d, c) for c in CAMPOS_DATOS),
                       "registro"]
            c = conn.execute(f"INSERT INTO clientes ({', '.join(columnas)}) VALUES ({', '.join(['%s'] * len(columnas))}) "
                             "RETURNING id", valores).fetchone()
            u = conn.execute("INSERT INTO usuarios (email, password_hash, rol, cliente_id, nombre) "
                             "VALUES (%s,%s,'cliente',%s,%s) RETURNING id",
                             (email, hash_password(d.password), c["id"], d.nombre)).fetchone()
    return emitir_token(str(u["id"]), "cliente", SESION_MINUTOS, cliente_id=c["id"], nombre=d.nombre)


@app.get("/clientes/me")
def mi_perfil(u: Usuario = Depends(requiere_rol("cliente"))):
    return _obtener(u.cliente_id)


@app.put("/clientes/me")
def editar_mi_perfil(d: EdicionCliente, u: Usuario = Depends(requiere_rol("cliente"))):
    return _actualizar(u.cliente_id, d)


def _actualizar(cliente_id: int, d: EdicionCliente) -> dict:
    campos = ["nombre", *CAMPOS_DATOS]
    with pool.connection() as conn:
        f = conn.execute(f"UPDATE clientes SET {', '.join(f'{c} = %s' for c in campos)}, actualizado_en = now() "
                         f"WHERE id = %s RETURNING {COLUMNAS}", [*(getattr(d, c) for c in campos), cliente_id]).fetchone()
    if f is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Cliente no encontrado."})
    return _serializar(f)


@app.get("/clientes")
def listar(buscar: str | None = None, limite: int = Query(50, ge=1, le=200), desde: int = Query(0, ge=0),
           _: Usuario = Depends(requiere_rol("admin"))):
    sql = f"SELECT {COLUMNAS} FROM clientes"
    params: list = []
    if buscar:
        sql += " WHERE nombre ILIKE %s OR cliente_externo_id ILIKE %s OR coalesce(email, '') ILIKE %s"
        params = [f"%{buscar}%"] * 3
    with pool.connection() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM ({sql}) t", params).fetchone()["n"]
        filas = conn.execute(sql + " ORDER BY id LIMIT %s OFFSET %s", [*params, limite, desde]).fetchall()
    return {"total": total, "clientes": [_serializar(f) for f in filas]}


@app.get("/clientes/{cliente_id}")
def detalle(cliente_id: int, u: Usuario = Depends(usuario_actual)):
    if not u.es_admin and u.cliente_id != cliente_id:
        raise HTTPException(403, detail={"codigo": "prohibido", "mensaje": "Solo puede consultar sus propios datos."})
    return _obtener(cliente_id)


@app.put("/clientes/{cliente_id}")
def editar(cliente_id: int, d: EdicionCliente, _: Usuario = Depends(requiere_rol("admin"))):
    return _actualizar(cliente_id, d)


@app.patch("/clientes/{cliente_id}/activo")
def cambiar_activo(cliente_id: int, activo: bool, _: Usuario = Depends(requiere_rol("admin"))):
    with pool.connection() as conn:
        f = conn.execute(f"UPDATE clientes SET activo = %s, actualizado_en = now() WHERE id = %s RETURNING {COLUMNAS}",
                         (activo, cliente_id)).fetchone()
    if f is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Cliente no encontrado."})
    return _serializar(f)


@app.post("/clientes/importar")
async def importar_csv(archivo: UploadFile = File(...), u: Usuario = Depends(requiere_rol("admin"))):
    contenido = await archivo.read()
    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(413, detail={"codigo": "archivo_grande", "mensaje": "El archivo supera 5 MB."})
    try:
        filas = leer_csv(contenido)
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(422, detail={"codigo": "csv_invalido", "mensaje": str(e)})
    with pool.connection() as conn:
        with conn.transaction():
            r = importar(conn, filas)
            conn.execute("INSERT INTO importaciones (archivo, sha256, filas, insertados, actualizados, sin_cambios, "
                         "rechazados, realizada_por) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (archivo.filename or "sin-nombre", hashlib.sha256(contenido).hexdigest(), r["filas"],
                          r["insertados"], r["actualizados"], r["sin_cambios"], r["rechazados"], u.sub))
    return r


@app.get("/clientes-importaciones")
def historial_importaciones(_: Usuario = Depends(requiere_rol("admin"))):
    with pool.connection() as conn:
        filas = conn.execute("SELECT * FROM importaciones ORDER BY id DESC LIMIT 20").fetchall()
    for f in filas:
        f["realizada_en"] = f["realizada_en"].isoformat()
    return filas


@app.get("/internos/clientes/{cliente_id}")
def interno(cliente_id: int, _: Usuario = Depends(requiere_rol("servicio"))):
    """Datos que Préstamos necesita para evaluar riesgo. Solo para llamadas entre servicios."""
    c = _obtener(cliente_id)
    return {k: c[k] for k in ("id", "cliente_externo_id", "activo", *CAMPOS_DATOS)}
