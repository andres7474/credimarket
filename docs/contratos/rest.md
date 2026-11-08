# Contratos REST

La especificación completa (esquemas de entrada y salida) está en OpenAPI 3.1: [`openapi/`](openapi/). Con el
sistema en ejecución también se puede ver en la documentación interactiva de cada servicio:
`http://localhost:8001/docs` (Productos), `:8002/docs` (Clientes), `:8003/docs` (Préstamos) y `:8004/docs` (Inventario).

## Convenciones

- **Autenticación:** `Authorization: Bearer <JWT>`. El token lo emite Clientes en `/auth/login` o `/auth/registro`.
  Roles: `cliente`, `admin` y `servicio`. El rol `servicio` corresponde a llamadas internas con un token de
  5 minutos.
- **Errores:** siempre con la forma `{"detail": {"codigo": "...", "mensaje": "...", "errores": [{"campo", "mensaje"}]?}}`.

  | HTTP | código típico | significado |
  |---|---|---|
  | 401 | `no_autenticado`, `token_invalido`, `sesion_vencida` | falta el token, el token es inválido o está vencido |
  | 403 | `prohibido` | el rol no alcanza o el recurso es de otro cliente |
  | 404 | `no_encontrado`, `inactivo` | el recurso no existe o está desactivado |
  | 409 | `idempotencia_conflicto`, `estado_no_permite`, `version_desactualizada`, `sku_duplicado`, `email_registrado` | conflicto de negocio |
  | 422 | `entrada_invalida` (+ `errores` por campo), `producto_inexistente`, `producto_inactivo` | datos inválidos |
  | 503 | `modelo_no_disponible`, `dependencia_no_disponible` | falta una dependencia; se puede reintentar |
- **CORS:** los orígenes permitidos se configuran con `CORS_ORIGINS`. Se exponen `X-Cache` y `X-Cache-Latencia-ms`.

## Productos (:8001)

| Método y ruta | Rol | Respuesta | Notas |
|---|---|---|---|
| `GET /productos[?incluir_inactivos&categoria]` | público | 200 lista | Lee la base de datos. |
| `GET /productos/{id}` | público | 200, 404 | **Cache-aside** con `productos:v1:{id}`. Encabezados `X-Cache: HIT \| MISS \| OMITIDA` (OMITIDA significa que Redis no está disponible). |
| `POST /productos` | admin | 201, 409 (SKU repetido), 422 | `{sku, nombre, descripcion, categoria, precio}` |
| `PUT /productos/{id}` | admin | 200, 409 (`version_esperada` distinta), 422 | Cambio parcial. Si cambia el precio, se incrementa `version` y se escribe `historial_precios`. La caché se invalida **después** del COMMIT. |
| `DELETE /productos/{id}` | admin | 200 | Desactiva el producto (no lo borra) e invalida la caché. |
| `POST /productos/{id}/reactivar` | admin | 200 | |
| `GET /internos/productos/{id}/cotizacion` | servicio, admin | 200 `{id, precio, version, activo}` | **Fuente de verdad** (PostgreSQL) para el monto del préstamo. |
| `GET /cache/metricas`, `POST /cache/metricas/reiniciar` | admin | 200 | hits, misses, tasa de aciertos, p50/p95 por tipo |
| `GET /cache/productos/{id}`, `DELETE /cache/productos/{id}` | admin | 200 | TTL restante; vaciar la clave para la demostración |
| `GET /health`, `GET /ready` | público | 200 / 503 | `ready` revisa la BD; Redis se informa pero no es crítico |

## Clientes (:8002)

| Método y ruta | Rol | Respuesta | Notas |
|---|---|---|---|
| `POST /auth/login` | público | 200 `{access_token, rol, cliente_id, nombre, expira_en}`, 401, 403 (inactivo) | |
| `POST /auth/registro` | público | 201 (token), 409, 422 | Datos personales, financieros y laborales |
| `GET /clientes/me`, `PUT /clientes/me` | cliente | 200, 422 | Edita solo nombre y datos financieros y laborales |
| `GET /clientes[?buscar&limite&desde]` | admin | 200 `{total, clientes}` | |
| `GET /clientes/{id}` | admin o el propio cliente | 200, 403 | |
| `PUT /clientes/{id}` | admin | 200, 422 | |
| `PATCH /clientes/{id}/activo?activo=` | admin | 200 | |
| `POST /clientes/importar` (multipart `archivo`) | admin | 200 `{filas, insertados, actualizados, sin_cambios, rechazados, errores[{fila, cliente_id, errores}]}` | Política en `services/clientes/app/importacion.py` |
| `GET /clientes-importaciones` | admin | 200 | Historial de importaciones con el hash del archivo |
| `GET /internos/clientes/{id}` | servicio | 200 | Lo usa Préstamos |

## Préstamos (:8003)

| Método y ruta | Rol | Respuesta | Notas |
|---|---|---|---|
| `POST /prestamos` + `Idempotency-Key` | cliente | **202** `PENDIENTE_INVENTARIO` · **201** `RECHAZADA` · reintento con la misma clave y el mismo cuerpo: devuelve la **misma** solicitud con el mismo código · **409** si es la misma clave con otro cuerpo · 422 · 503 | Cuerpo `{producto_id, cantidad (1–5), plazo_meses ∈ {6,12,18,24,36}}`. El `cliente_id` sale del token. |
| `GET /prestamos[?estado&limite]` | cliente (los suyos), admin (todos) | 200 | |
| `GET /prestamos/{id}` | dueño o admin | 200, 403, 404 | Estado, motivo, cotización usada (`precio_unitario`, `version_cotizacion`), `probabilidad`, `decision_riesgo`, `umbral`, `model_version` e `historial` |
| `POST /prestamos/{id}/cancelar` | dueño o admin | 200 `CANCELACION_PENDIENTE`, 409 | Solo desde `PENDIENTE_INVENTARIO` o `APROBADA` |
| `POST /prestamos/{id}/entregar` | admin | 200 `ENTREGA_PENDIENTE`, 409 | Solo desde `APROBADA` |
| `GET /prestamos/{id}/mensajes` | admin | 200 `{enviados, recibidos}` | Trazabilidad del outbox y del inbox |
| `POST /riesgo/predict` | admin, servicio | 200 `{probabilidad_incumplimiento, decision_riesgo, umbral, model_version, run_id}`, 422 | **No** crea préstamos ni reserva |
| `GET /modelo/info` | público | 200, 503 | Versión, run ID, variables, umbral, métricas, hash y promoción |
| `POST /modelo/recargar` | admin | 200 `{anterior, activa, info}` | Relee `models/activo.json` (promoción o rollback) |
| `GET /modelo/monitoreo[?ventana]` | admin | 200 | Predicciones por versión, drift (PSI) y estado de la mensajería |
| `GET /health` | público | 200 | El proceso responde |
| `GET /ready` | público | 200 / **503 "no disponible"** | Revisa el modelo cargado, la BD y la conexión al broker |

## Inventario (:8004)

| Método y ruta | Rol | Respuesta | Notas |
|---|---|---|---|
| `GET /existencias`, `GET /existencias/{producto_id}` | público | 200 `{producto_id, disponible, reservado}` | |
| `POST /existencias/{producto_id}/entradas` | admin | 201, 422 (producto inexistente) | Verifica el producto con la API de Productos |
| `GET /reservas[?estado&limite]` | admin | 200 | |
| `GET /movimientos[?producto_id&limite]` | admin | 200 | Libro de ENTRADA, RESERVA, LIBERACION, SALIDA y VENCIMIENTO |
| `GET /mensajeria/estado`, `GET /mensajeria/outbox?prestamo_id=` | admin | 200 | |
| `GET /health`, `GET /ready` | público | 200 / 503 | |

La reserva, la liberación y la entrega **no** tienen endpoint REST en Inventario: solo se ejecutan por mensajes
(ver [contratos de mensajes](mensajes/README.md)).
