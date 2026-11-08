"""Servicio de Productos: catálogo, precio, categoría y versión; caché Redis cache-aside.

No administra existencias ni decide préstamos.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from credimarket_comun.auth import Usuario, requiere_rol
from credimarket_comun.db import aplicar_esquema, crear_pool, db_lista
from credimarket_comun.web import configurar_app, configurar_logs

from .cache import Cache

configurar_logs()
CATEGORIAS = Literal["computadores", "celulares", "tablets", "accesorios", "otros"]
COLUMNAS = "id, sku, nombre, descripcion, categoria, precio, version, activo, actualizado_en"
CATALOGO_INICIAL = [
    ("LAP-001", "Portátil Andino 14", "14 pulgadas, 16 GB RAM, 512 GB SSD", "computadores", 3_200_000),
    ("CEL-001", "Celular Cóndor X", "6,5 pulgadas, 256 GB", "celulares", 1_800_000),
    ("TAB-001", "Tableta Llanos 10", "10 pulgadas, 128 GB", "tablets", 1_400_000),
    ("LAP-002", "Portátil Nevado Pro 16", "16 pulgadas, 32 GB RAM, 1 TB SSD", "computadores", 7_500_000),
    ("ACC-001", "Audífonos Caribe", "Inalámbricos con cancelación de ruido", "accesorios", 450_000),
    ("LAP-003", "Portátil Última Unidad", "Edición limitada: una sola unidad en inventario", "computadores", 4_100_000),
]

pool = None
cache = Cache()


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    global pool
    pool = crear_pool()
    aplicar_esquema(pool, Path(__file__).with_name("esquema.sql"))
    with pool.connection() as conn:
        if conn.execute("SELECT count(*) AS n FROM productos").fetchone()["n"] == 0:
            for sku, nombre, desc, cat, precio in CATALOGO_INICIAL:
                fila = conn.execute(
                    "INSERT INTO productos (sku, nombre, descripcion, categoria, precio) VALUES (%s,%s,%s,%s,%s) "
                    "RETURNING id", (sku, nombre, desc, cat, precio)).fetchone()
                conn.execute("INSERT INTO historial_precios (producto_id, version, precio) VALUES (%s, 1, %s)",
                             (fila["id"], precio))
    yield
    pool.close()


app = FastAPI(title="CrediMarket · Productos", version="1.0.0", lifespan=ciclo_vida)
configurar_app(app)


class ProductoNuevo(BaseModel):
    sku: str = Field(min_length=3, max_length=40)
    nombre: str = Field(min_length=2, max_length=120)
    descripcion: str = Field(default="", max_length=500)
    categoria: CATEGORIAS
    precio: Decimal = Field(gt=0, le=100_000_000, decimal_places=2)


class ProductoCambio(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=120)
    descripcion: str | None = Field(default=None, max_length=500)
    categoria: CATEGORIAS | None = None
    precio: Decimal | None = Field(default=None, gt=0, le=100_000_000, decimal_places=2)
    version_esperada: int | None = Field(default=None, description="Control de concurrencia optimista")


def _serializar(fila: dict) -> dict:
    d = dict(fila)
    d["precio"] = float(d["precio"])
    d["actualizado_en"] = d["actualizado_en"].isoformat()
    return d


def _leer_bd(producto_id: int) -> dict | None:
    with pool.connection() as conn:
        fila = conn.execute(f"SELECT {COLUMNAS} FROM productos WHERE id = %s", (producto_id,)).fetchone()
    return _serializar(fila) if fila else None


@app.get("/health")
def health():
    return {"estado": "ok", "servicio": "productos"}


@app.get("/ready")
def ready(response: Response):
    bd = db_lista(pool)
    redis_ok = cache.disponible_ahora()
    if not bd:
        response.status_code = 503
    # Redis no es dependencia crítica: sin él se responde desde la BD.
    return {"listo": bd, "base_datos": bd, "redis": redis_ok}


@app.get("/productos")
def listar(incluir_inactivos: bool = False, categoria: str | None = None):
    sql = f"SELECT {COLUMNAS} FROM productos WHERE (%s OR activo)"
    params: list = [incluir_inactivos]
    if categoria:
        sql += " AND categoria = %s"
        params.append(categoria)
    with pool.connection() as conn:
        return [_serializar(f) for f in conn.execute(sql + " ORDER BY id", params).fetchall()]


@app.get("/productos/{producto_id}")
def detalle(producto_id: int, response: Response):
    """Cache-aside: Redis primero; si falla o no está, BD y se guarda con TTL."""
    inicio = time.perf_counter()
    valor, estado = cache.leer(producto_id)
    if valor is None:
        valor = _leer_bd(producto_id)
        if valor is None:
            raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Producto no encontrado."})
        if estado == "MISS":
            cache.guardar(producto_id, valor)
    ms = (time.perf_counter() - inicio) * 1000
    cache.metricas.registrar({"HIT": "hit", "MISS": "miss"}.get(estado, "sin_cache"), ms)
    response.headers["X-Cache"] = estado
    response.headers["X-Cache-Latencia-ms"] = f"{ms:.3f}"
    if not valor["activo"]:
        raise HTTPException(404, detail={"codigo": "inactivo", "mensaje": "El producto no está disponible."})
    return valor


@app.get("/internos/productos/{producto_id}/cotizacion")
def cotizacion(producto_id: int, _: Usuario = Depends(requiere_rol("servicio", "admin"))):
    """Cotización vigente leída de la FUENTE DE VERDAD (BD), nunca de la caché."""
    valor = _leer_bd(producto_id)
    if valor is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Producto no encontrado."})
    return {"producto_id": valor["id"], "sku": valor["sku"], "nombre": valor["nombre"], "precio": valor["precio"],
            "version": valor["version"], "activo": valor["activo"], "fuente": "base_de_datos"}


@app.post("/productos", status_code=201)
def crear(p: ProductoNuevo, _: Usuario = Depends(requiere_rol("admin"))):
    with pool.connection() as conn:
        try:
            fila = conn.execute(
                f"INSERT INTO productos (sku, nombre, descripcion, categoria, precio) VALUES (%s,%s,%s,%s,%s) "
                f"RETURNING {COLUMNAS}", (p.sku, p.nombre, p.descripcion, p.categoria, p.precio)).fetchone()
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, detail={"codigo": "sku_duplicado", "mensaje": "Ya existe un producto con ese SKU."})
            raise
        conn.execute("INSERT INTO historial_precios (producto_id, version, precio) VALUES (%s, 1, %s)",
                     (fila["id"], p.precio))
    return _serializar(fila)


@app.put("/productos/{producto_id}")
def actualizar(producto_id: int, c: ProductoCambio, _: Usuario = Depends(requiere_rol("admin"))):
    cambios = c.model_dump(exclude_none=True, exclude={"version_esperada"})
    if not cambios:
        raise HTTPException(422, detail={"codigo": "sin_cambios", "mensaje": "No se envió ningún cambio."})
    sets = ", ".join(f"{k} = %s" for k in cambios)
    with pool.connection() as conn:
        with conn.transaction():
            fila = conn.execute(
                f"UPDATE productos SET {sets}, version = version + 1, actualizado_en = now() "
                f"WHERE id = %s AND (%s::int IS NULL OR version = %s) RETURNING {COLUMNAS}",
                (*cambios.values(), producto_id, c.version_esperada, c.version_esperada)).fetchone()
            if fila is None:
                existe = conn.execute("SELECT version FROM productos WHERE id = %s", (producto_id,)).fetchone()
                if existe is None:
                    raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Producto no encontrado."})
                raise HTTPException(409, detail={"codigo": "version_desactualizada",
                                                 "mensaje": f"El producto cambió (versión actual {existe['version']})."})
            conn.execute("INSERT INTO historial_precios (producto_id, version, precio) VALUES (%s, %s, %s)",
                         (producto_id, fila["version"], fila["precio"]))
    cache.invalidar(producto_id)  # después del COMMIT
    return _serializar(fila)


@app.delete("/productos/{producto_id}")
def desactivar(producto_id: int, _: Usuario = Depends(requiere_rol("admin"))):
    with pool.connection() as conn:
        fila = conn.execute(f"UPDATE productos SET activo = FALSE, version = version + 1, actualizado_en = now() "
                            f"WHERE id = %s RETURNING {COLUMNAS}", (producto_id,)).fetchone()
    if fila is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Producto no encontrado."})
    cache.invalidar(producto_id)
    return _serializar(fila)


@app.post("/productos/{producto_id}/reactivar")
def reactivar(producto_id: int, _: Usuario = Depends(requiere_rol("admin"))):
    with pool.connection() as conn:
        fila = conn.execute(f"UPDATE productos SET activo = TRUE, version = version + 1, actualizado_en = now() "
                            f"WHERE id = %s RETURNING {COLUMNAS}", (producto_id,)).fetchone()
    if fila is None:
        raise HTTPException(404, detail={"codigo": "no_encontrado", "mensaje": "Producto no encontrado."})
    cache.invalidar(producto_id)
    return _serializar(fila)


@app.get("/cache/metricas")
def metricas(_: Usuario = Depends(requiere_rol("admin"))):
    return {"ttl_segundos": cache.ttl, "redis_disponible": cache.disponible_ahora(), **cache.metricas.resumen()}


@app.post("/cache/metricas/reiniciar")
def reiniciar_metricas(_: Usuario = Depends(requiere_rol("admin"))):
    cache.metricas.reiniciar()
    return {"ok": True}


@app.get("/cache/productos/{producto_id}")
def estado_clave(producto_id: int, _: Usuario = Depends(requiere_rol("admin"))):
    """Muestra si la clave existe y su TTL restante (para demostrar expiración)."""
    return {"clave": f"productos:v1:{producto_id}", "ttl_restante_s": cache.ttl_restante(producto_id)}


@app.delete("/cache/productos/{producto_id}")
def vaciar_clave(producto_id: int, _: Usuario = Depends(requiere_rol("admin"))):
    """Borra la entrada de caché (demostración de 'Redis vacío'); la siguiente lectura será MISS."""
    cache.invalidar(producto_id)
    return {"clave": f"productos:v1:{producto_id}", "borrada": True}
