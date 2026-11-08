# Instrucciones de reproducción (desde un clon limpio)

## Requisitos

- **Docker Desktop** (Windows/macOS) o Docker Engine con el plugin Compose v2 (Linux). Hacen falta unos 4 GB
  de RAM libres para Docker.
- **Git**.
- **Python 3.12 o superior**, solo para los scripts de prueba y de promoción del modelo. Los servicios corren en
  contenedores.
- Puertos libres: 8080, 8001–8004, 5000, 5432, 5672, 15672 y 6379.

## 1. Clonar y generar la configuración

```bash
git clone <url-del-repositorio> credimarket
cd credimarket
```

Linux o macOS:

```bash
./scripts/init-env.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\init-env.ps1
```

El script crea `.env` (ignorado por git) a partir de `.env.example`, con secretos aleatorios: JWT,
contraseñas de las 4 bases, de RabbitMQ y de las cuentas de demostración. Al final imprime las cuentas.
**No se edita `.env.example` con secretos reales.**

## 2. Levantar todo

```bash
docker compose up -d --build
docker compose ps          # esperar a que postgres y rabbitmq estén "healthy" (≈ 30–60 s la primera vez)
```

La primera construcción descarga las imágenes base e instala las dependencias fijadas (unos 5–10 minutos).
Al arrancar, cada servicio crea sus tablas y sus datos de demostración: catálogo, existencias, administrador
y tres clientes. Préstamos carga el modelo promovido en `models/activo.json` (v2).

| Servicio | URL |
|---|---|
| Frontend | http://localhost:8080 |
| Productos / Clientes / Préstamos / Inventario | http://localhost:8001 … 8004 (`/docs` para la API) |
| MLflow | http://localhost:5000 |
| RabbitMQ (consola) | http://localhost:15672 |

Comprobación rápida: `curl http://localhost:8003/ready` debe responder `"listo": true`, con `modelo`,
`base_datos`, `broker_publicacion` y `broker_consumo` en `true`.

## 3. Pruebas de aceptación y mediciones

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r scripts/requirements.txt
python scripts/pruebas/aceptacion.py
python scripts/bench_cache.py --con-caida
python scripts/simular_drift.py
```

Los scripts leen URLs y credenciales de `.env` y usan `docker compose stop/start <servicio>` para simular
caídas. Para cambiar esos comandos se usan las variables `CMD_DETENER` y `CMD_INICIAR`.

## 4. Experimentos de ML (MLflow)

- El historial de MLflow está **versionado en el repositorio** en `mlops/mlflow/`: `mlflow.db` (SQLite) y
  `artifacts/`. El contenedor `mlflow` lo monta desde ahí, así que los 8 runs se ven apenas se levanta el
  sistema y se conservan entre reinicios y entre máquinas.
- Para **conservar experimentos nuevos**, después de entrenar se hace commit de `mlops/mlflow/`, `models/` y
  `ml/reportes/`.
- Reentrenar dentro de Docker, sin instalar nada en el equipo:

```bash
docker compose --profile entrenamiento build entrenamiento
docker compose --profile entrenamiento run --rm entrenamiento python ml/preparar_datos.py
docker compose --profile entrenamiento run --rm entrenamiento python ml/entrenar.py
docker compose --profile entrenamiento run --rm entrenamiento python ml/exportar.py --version v3 --rol seleccionado
python ml/promover.py --version v3 --motivo "reentrenamiento con datos nuevos"
curl -X POST -H "Authorization: Bearer <token admin>" http://localhost:8003/modelo/recargar   # o el botón en el frontend
```

`exportar.py` no sobrescribe una versión existente. `promover.py` solo usa la librería estándar de Python.

## 5. Operación

| Acción | Comando |
|---|---|
| Ver logs de un servicio | `docker compose logs -f prestamos` |
| Reiniciar un servicio | `docker compose restart inventario` |
| Apagar conservando los datos | `docker compose down` |
| Borrar los datos (bases y colas) | `docker compose down -v` |
| Regenerar los secretos | `./scripts/init-env.sh --forzar` y luego `docker compose down -v` (las contraseñas de PostgreSQL se fijan al crear el volumen) |
| Probar el vencimiento de reservas | `RESERVA_TTL_SEGUNDOS=60` en `.env` → `docker compose up -d inventario` |

## 6. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | Hay un PostgreSQL local. Detenerlo, o cambiar el puerto publicado en `docker-compose.yml` (`"127.0.0.1:5433:5432"`). |
| `password authentication failed for user "productos_app"` | Se regeneró `.env` con un volumen viejo. Ejecutar `docker compose down -v` y volver a levantar. |
| `exec /docker-entrypoint-initdb.d/01-crear-bases.sh: no such file or directory` en Windows | Git convirtió los saltos de línea a CRLF. `.gitattributes` lo evita en un clon nuevo; si ya pasó, ejecutar `git rm --cached -r . && git reset --hard`. |
| `/ready` de Préstamos dice `modelo: false` | Revisar `docker compose logs prestamos`. `models/activo.json` debe apuntar a una versión cuyo `pipeline.joblib` tenga el hash del manifiesto. |
| El frontend dice "No se pudo conectar con el servicio…" | Ese contenedor está detenido o reiniciándose (`docker compose ps`). Si se cambió el puerto o el host, ajustar `*_PUBLIC_URL` y `CORS_ORIGINS` en `.env` y ejecutar `docker compose up -d`. |
| MLflow responde "Invalid Host header" | Abrir MLflow con `http://localhost:5000`, no con la IP de la máquina, o agregar el host a `--allowed-hosts` en `docker-compose.yml`. |

## 7. Cómo se probó este repositorio

Los cuatro servicios, el frontend y todos los scripts se ejecutaron en Linux con PostgreSQL 16, RabbitMQ 3.12 y
Redis instalados de forma nativa, usando la misma configuración que inyecta `docker-compose.yml`. Las 15 pruebas
automáticas y el caso 18 pasaron (`docs/evidencias/aceptacion_local.txt`). `docker compose config` valida el
archivo de Compose. Las imágenes de Docker se construyen en el primer `docker compose up --build` en el equipo
del autor. Si algo falla en ese paso, la sección 6 cubre los problemas más probables.
