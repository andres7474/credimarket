#!/bin/sh
# Crea una base de datos lógica y un usuario restringido por servicio.
# Cada usuario solo tiene permisos sobre SU base: un servicio no puede leer tablas de otro.
set -eu

crear() {
  base="$1"; usuario="$2"; clave="$3"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE ${usuario} LOGIN PASSWORD '${clave}';
CREATE DATABASE ${base} OWNER ${usuario};
REVOKE ALL ON DATABASE ${base} FROM PUBLIC;
SQL
}

crear productos_db  productos_app  "$PRODUCTOS_DB_PASSWORD"
crear clientes_db   clientes_app   "$CLIENTES_DB_PASSWORD"
crear prestamos_db  prestamos_app  "$PRESTAMOS_DB_PASSWORD"
crear inventario_db inventario_app "$INVENTARIO_DB_PASSWORD"
