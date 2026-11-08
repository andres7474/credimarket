"""Utilidades comunes de FastAPI: CORS configurable, logs y formato de errores."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


def configurar_logs() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("pika").setLevel(logging.WARNING)


def configurar_app(app: FastAPI) -> None:
    origenes = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:8080").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origenes, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
                       expose_headers=["X-Cache", "X-Cache-Latencia-ms", "Location"])

    @app.exception_handler(RequestValidationError)
    async def _validacion(request: Request, exc: RequestValidationError):
        errores = [{"campo": ".".join(str(p) for p in e["loc"] if p != "body"), "mensaje": e["msg"]}
                   for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": {"codigo": "entrada_invalida",
                                                                  "mensaje": "Datos inválidos.", "errores": errores}})
