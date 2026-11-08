#!/bin/sh
# Genera config.js con las URL públicas de las APIs (variables de entorno del contenedor).
# No contiene secretos: el navegador solo recibe direcciones de APIs.
set -eu
cat > /usr/share/nginx/html/config.js <<CFG
window.CREDIMARKET_CONFIG = {
  productos: "${PRODUCTOS_API:-http://localhost:8001}",
  clientes: "${CLIENTES_API:-http://localhost:8002}",
  prestamos: "${PRESTAMOS_API:-http://localhost:8003}",
  inventario: "${INVENTARIO_API:-http://localhost:8004}",
  mlflow: "${MLFLOW_URL:-http://localhost:5000}",
  rabbitmq: "${RABBITMQ_UI_URL:-http://localhost:15672}"
};
CFG
