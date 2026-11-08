"""Autenticación y autorización compartidas (JWT HS256 firmado con JWT_SECRET).

Roles:
  cliente   solo accede a sus propios datos; su cliente_id sale del token, nunca del cuerpo.
  admin     operaciones administrativas.
  servicio  llamadas internas entre microservicios (Préstamos -> Clientes / Productos, etc.).
El secreto se lee del entorno (.env generado localmente, nunca versionado).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request

EMISOR = "credimarket"
ALGORITMO = "HS256"


def _secreto() -> str:
    s = os.environ.get("JWT_SECRET", "")
    if len(s) < 32:
        raise RuntimeError("JWT_SECRET no está configurado (mínimo 32 caracteres). Ejecute scripts/init-env.")
    return s


@dataclass
class Usuario:
    sub: str
    rol: str
    cliente_id: int | None
    nombre: str | None = None

    @property
    def es_admin(self) -> bool:
        return self.rol == "admin"


def emitir_token(sub: str, rol: str, minutos: int = 30, cliente_id: int | None = None, nombre: str | None = None) -> dict:
    ahora = int(time.time())
    expira = ahora + minutos * 60
    claims = {"iss": EMISOR, "sub": str(sub), "rol": rol, "iat": ahora, "exp": expira}
    if cliente_id is not None:
        claims["cliente_id"] = cliente_id
    if nombre:
        claims["nombre"] = nombre
    return {"access_token": jwt.encode(claims, _secreto(), algorithm=ALGORITMO), "token_type": "bearer",
            "expira_en": expira, "rol": rol, "cliente_id": cliente_id, "nombre": nombre}


def token_servicio(nombre_servicio: str) -> str:
    """Token de corta duración para llamadas internas."""
    return emitir_token(f"servicio:{nombre_servicio}", "servicio", minutos=5)["access_token"]


def _decodificar(token: str) -> Usuario:
    try:
        c = jwt.decode(token, _secreto(), algorithms=[ALGORITMO], issuer=EMISOR,
                       options={"require": ["exp", "iat", "sub", "rol"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, detail={"codigo": "sesion_vencida", "mensaje": "La sesión venció. Inicie sesión de nuevo."})
    except jwt.InvalidTokenError:
        raise HTTPException(401, detail={"codigo": "token_invalido", "mensaje": "Token inválido."})
    return Usuario(sub=c["sub"], rol=c["rol"], cliente_id=c.get("cliente_id"), nombre=c.get("nombre"))


def usuario_actual(request: Request) -> Usuario:
    cabecera = request.headers.get("Authorization", "")
    if not cabecera.lower().startswith("bearer "):
        raise HTTPException(401, detail={"codigo": "no_autenticado", "mensaje": "Debe iniciar sesión."})
    return _decodificar(cabecera.split(" ", 1)[1].strip())


def requiere_rol(*roles: str):
    def dependencia(usuario: Usuario = Depends(usuario_actual)) -> Usuario:
        if usuario.rol not in roles:
            raise HTTPException(403, detail={"codigo": "prohibido", "mensaje": "No tiene permiso para esta operación."})
        return usuario
    return dependencia
