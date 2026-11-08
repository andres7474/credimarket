"""Acceso a PostgreSQL. Cada servicio usa SU base de datos y SU usuario."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("db")


def dsn_desde_entorno() -> str:
    return (f"host={os.environ.get('DB_HOST', 'localhost')} port={os.environ.get('DB_PORT', '5432')} "
            f"dbname={os.environ['DB_NAME']} user={os.environ['DB_USER']} password={os.environ['DB_PASSWORD']} "
            "connect_timeout=3")


def crear_pool(dsn: str | None = None, min_size: int = 2, max_size: int = 10) -> ConnectionPool:
    pool = ConnectionPool(dsn or dsn_desde_entorno(), min_size=min_size, max_size=max_size,
                          kwargs={"row_factory": dict_row}, open=False, timeout=5)
    for intento in range(60):
        try:
            pool.open(wait=True, timeout=5)
            return pool
        except Exception as e:  # la base de datos puede tardar en arrancar
            log.warning("Base de datos no disponible (%s). Reintento %s", e, intento + 1)
            time.sleep(2)
    raise RuntimeError("No fue posible conectar a la base de datos")


def aplicar_esquema(pool: ConnectionPool, ruta_sql: Path) -> None:
    with pool.connection() as conn:
        conn.execute(ruta_sql.read_text(encoding="utf-8"))


def db_lista(pool: ConnectionPool) -> bool:
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
