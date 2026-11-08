# Informe de Redis: caché del catálogo y desempeño

## 1. Diseño

| Aspecto | Decisión |
|---|---|
| Patrón | **Cache-aside** en el servicio de Productos (`services/productos/app/cache.py`). |
| Qué se guarda | El JSON del producto (`GET /productos/{id}`), incluida su `version` y el indicador `activo`. |
| Clave | `productos:v1:{producto_id}`. El `v1` es la versión del **formato** del valor: si cambia la estructura, se pasa a `v2` y las claves viejas expiran solas. |
| TTL | `CACHE_TTL_SEGUNDOS = 60`. |
| Lectura | 1) `GET` en Redis → **HIT**, se responde. 2) Si no está → **MISS**: se lee PostgreSQL y se guarda con `SET ... EX 60`. 3) Si Redis falla → **OMITIDA**: se responde desde PostgreSQL. El encabezado `X-Cache` indica el caso. |
| Escritura | `PUT`/`DELETE`/`reactivar`: primero se hace el **COMMIT** en PostgreSQL y **después** `DEL` de la clave. No se escribe el valor nuevo en la caché: lo carga la siguiente lectura. |
| Tolerancia a fallos | Tiempos de espera de 100 ms (conexión y operación). Tras un error, Redis se omite durante 5 s (cortocircuito) para no sumar 100 ms a cada petición mientras está caído. |
| Configuración del contenedor | `--maxmemory 64mb --maxmemory-policy allkeys-lru` y sin persistencia (`--save "" --appendonly no`). Es una caché: perderla solo cuesta misses. |
| Qué **no** hace Redis | No guarda préstamos, reservas, claves de idempotencia ni la deduplicación de mensajes. Todo eso es durable en PostgreSQL y sobrevive a borrar Redis (prueba 8 y prueba 5). |
| Precio del préstamo | Préstamos **no** usa la caché: pide `GET /internos/productos/{id}/cotizacion`, que lee PostgreSQL, y guarda `precio_unitario` y `version_cotizacion` en el préstamo (prueba 9). |

### ¿Cuándo puede estar desactualizado un dato de la caché?

1. **Carrera lectura/escritura (ventana pequeña):** una lectura hace MISS y lee el precio viejo de la BD.
   Mientras tanto, otra petición actualiza el precio y borra la clave. Si la primera guarda su valor después
   del borrado, queda el precio viejo en caché **hasta 60 s** (TTL). Es el caso clásico de cache-aside y el TTL
   lo acota.
2. **Fallo al invalidar:** si Redis no responde justo cuando se borra la clave, la entrada vieja puede vivir
   hasta 60 s. Esto queda registrado en la métrica `invalidaciones_fallidas`.
3. **Escrituras fuera de la API:** un cambio hecho directamente en la base no invalida la caché. No está
   permitido; solo Productos escribe en `productos_db`.

En los tres casos el efecto se limita a **mostrar** un precio viejo en el catálogo durante un máximo de 60 s.
El monto del préstamo no se ve afectado, porque siempre se calcula con la fuente de verdad. El frontend lo
advierte junto al formulario.

## 2. Demostración funcional (prueba de aceptación 8)

Resultado de `python scripts/pruebas/aceptacion.py 8` (ver `docs/evidencias/aceptacion_local.txt`):

```
[8] Redis: miss, hit, expiración, invalidación y caída
     ✓ primera consulta MISS, segunda HIT
     ✓ la clave productos:v1:2 tiene TTL 60 s
     ✓ tras el TTL la entrada expiró (MISS)
     ✓ con Redis caído responde desde la BD en 5 ms
     ✓ al volver Redis la caché se recupera
```

Desde el navegador (administrador): en el detalle de un producto aparece la etiqueta `MISS`/`HIT` y el botón
"Consultar otra vez". Al cambiar el precio y volver al detalle se ve `MISS` con el precio nuevo. En el
catálogo, el panel "Caché Redis del catálogo" muestra la tasa de aciertos y los percentiles p50 y p95.
Comandos útiles:

```bash
docker compose exec redis redis-cli KEYS 'productos:v1:*'
docker compose exec redis redis-cli TTL productos:v1:2
docker compose stop redis      # caída: X-Cache: OMITIDA y la respuesta sigue siendo 200
docker compose start redis
```

## 3. Mediciones de carga

Script: `python scripts/bench_cache.py --peticiones 3000 --concurrencia 16 --con-caida`. Genera
`docs/evidencias/bench_cache.json`. Se hicieron 3.000 lecturas aleatorias sobre los 7 productos del catálogo
con 16 hilos concurrentes, en una máquina de 2 vCPU y 8 GB, con un solo proceso uvicorn y PostgreSQL y Redis
locales.

### Vista del cliente HTTP (latencia de extremo a extremo)

| Escenario | Endpoint | p50 | p95 | p99 | Peticiones/s |
|---|---|---|---|---|---|
| A. Caché caliente (100 % HIT) | `GET /productos/{id}` | **9,5 ms** | **18,5 ms** | 26,3 ms | **1.472** |
| D. Redis caído (100 % PostgreSQL) | `GET /productos/{id}` | 16,6 ms | 29,9 ms | 39,6 ms | 890 |
| B. Fuente de verdad (referencia) | `GET /internos/.../cotizacion` | 24,3 ms | 37,0 ms | 43,2 ms | 639 |

A y D comparan **el mismo endpoint** con y sin Redis: la caché reduce la mediana un 43 % y aumenta el
throughput 1,65 veces. B no es directamente comparable, porque además valida un JWT en cada petición. Se
incluye como el costo de la consulta que usa Préstamos.

### Vista del servidor (tiempo dentro de Productos, `GET /cache/metricas`)

| Escenario | Tasa de aciertos | HIT p50 / p95 | MISS p50 / p95 | Sin Redis p50 / p95 |
|---|---|---|---|---|
| A. Caliente | 100 % (3.000 / 3.000) | 0,17 / 2,17 ms | — | — |
| C. Invalidación cada 100 lecturas (simula cambios frecuentes de catálogo) | **90,1 %** (2.704 / 3.000) | 0,34 / 5,13 ms | 4,59 / 13,34 ms | — |
| D. Redis caído | — | — | — | 2,91 / 9,57 ms |

### Análisis

- **Un HIT cuesta unas 25 veces menos que un MISS** dentro del servicio (0,17 ms frente a 4,6 ms de mediana).
  Con un 90 % de aciertos, el tiempo medio por lectura baja aproximadamente de 4,6 ms a
  0,9·0,34 + 0,1·4,6 ≈ 0,77 ms.
- **El MISS es más caro que no tener Redis** (4,6 ms frente a 2,9 ms), porque suma el GET fallido y el SET.
  Por eso la caché solo conviene si la tasa de aciertos es alta. Con un catálogo pequeño y muy leído, y un
  TTL de 60 s, la tasa observada fue de 90 % incluso invalidando cada 100 lecturas.
- **La diferencia de extremo a extremo (9,5 ms frente a 16,6 ms) es menor que la del servidor.** Con 16
  clientes concurrentes en 2 vCPU domina el costo de HTTP y del propio uvicorn. En producción, con una base
  remota y tablas más grandes, el beneficio relativo sería mayor.
- **Comportamiento ante fallos:** con Redis detenido no hubo ni una respuesta con error: 1.000 de 1.000 fueron
  200 `OMITIDA`. Solo la primera petición pagó el tiempo de espera de 100 ms (`errores_redis = 1`); el
  cortocircuito de 5 s evitó repetir ese costo. Al volver Redis, la caché se repobló sola (prueba 8).
- **Desactualización medida:** en la prueba 9, tras un `PUT` del precio, la siguiente lectura fue `MISS` con el
  precio nuevo, y el préstamo creado a continuación usó la versión nueva de la cotización.

## 4. Recursos consumidos

| Recurso | Valor medido |
|---|---|
| Memoria de Redis con el catálogo cargado | 0,9 MB (`used_memory_human`); cada producto ocupa menos de 1 KB |
| Límite configurado | 64 MB con `allkeys-lru`. Alcanza para unos 50.000 productos con holgura |
| Contenedor `redis:8-alpine` en reposo | ~5–10 MB de RAM; CPU despreciable a este volumen |
| Persistencia en disco | ninguna (es caché) |

Para medir en Docker: `docker stats --no-stream credimarket-redis-1 credimarket-productos-1`, y luego volver a
ejecutar `scripts/bench_cache.py`. Los resultados se guardan en `docs/evidencias/bench_cache.json`.
