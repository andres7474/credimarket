#!/usr/bin/env sh
# Genera .env a partir de .env.example reemplazando cada <generado> por un secreto aleatorio.
# No sobrescribe un .env existente (use --forzar para regenerarlo; implica borrar los volúmenes).
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ] && [ "${1:-}" != "--forzar" ]; then
  echo ".env ya existe; no se modifica. Use --forzar para regenerarlo (luego: docker compose down -v)."
  exit 0
fi
aleatorio() { python3 -c "import secrets; print(secrets.token_hex($1))" 2>/dev/null || openssl rand -hex "$1"; }
: > .env
while IFS= read -r linea || [ -n "$linea" ]; do
  case "$linea" in
    JWT_SECRET=\<generado\>) echo "JWT_SECRET=$(aleatorio 32)" >> .env ;;
    *_PASSWORD=\<generado\>)
      clave="${linea%%=*}"
      case "$clave" in
        ADMIN_PASSWORD|DEMO_CLIENTE_PASSWORD) echo "$clave=Cm-$(aleatorio 6)" >> .env ;;
        *) echo "$clave=$(aleatorio 16)" >> .env ;;
      esac ;;
    *) echo "$linea" >> .env ;;
  esac
done < .env.example
chmod 600 .env
echo "Se creó .env con secretos aleatorios. Cuentas de la demo:"
grep -E '^(ADMIN_EMAIL|ADMIN_PASSWORD|DEMO_CLIENTE_PASSWORD)=' .env
