# Guía de demostración

## Cuentas de prueba

Las contraseñas **no** están en el repositorio. `scripts/init-env` las genera y las imprime, y quedan en `.env`.

| Usuario | Rol | Contraseña | Perfil para la demo |
|---|---|---|---|
| `admin@credimarket.co` | administrador | `ADMIN_PASSWORD` de `.env` | Catálogo, clientes, inventario, entregas y modelo |
| `ana@demo.credimarket.co` | cliente | `DEMO_CLIENTE_PASSWORD` | Ingresos altos, sin atrasos: **riesgo favorable** |
| `bruno@demo.credimarket.co` | cliente | `DEMO_CLIENTE_PASSWORD` | Ingresos bajos, empleo temporal, atrasos: **riesgo desfavorable** |
| `carla@demo.credimarket.co` | cliente | `DEMO_CLIENTE_PASSWORD` | Riesgo favorable (para concurrencia y préstamos ajenos) |

También se puede crear un cliente nuevo con **Registrarse** en la pantalla de acceso.

Catálogo inicial: Portátil Andino 14 (20 u.), Celular Cóndor X (30), Tableta Llanos 10 (10), Portátil Nevado
Pro 16 (5), Audífonos Caribe (40) y **Portátil Última Unidad (1 unidad)**.

## Direcciones

| Qué | URL |
|---|---|
| Frontend | http://localhost:8080 |
| MLflow (experimentos y runs) | http://localhost:5000 → experimento `credimarket-riesgo` |
| RabbitMQ (colas, DLQ) | http://localhost:15672 (usuario `RABBITMQ_USER` y contraseña `RABBITMQ_PASSWORD` de `.env`) |
| Documentación de las APIs | http://localhost:8001/docs · :8002/docs · :8003/docs · :8004/docs |

## Recorrido en el navegador (unos 15 minutos)

Cubre lo que la sección 10 exige demostrar desde el frontend: préstamo aprobado, rechazo por riesgo, falta de
inventario, cancelación, entrega, cambio de precio, importación de clientes y versión del modelo.

1. **Préstamo aprobado (casos 1 y 15).** Entrar como **Ana** → Catálogo → *Portátil Andino 14* → cantidad 1,
   plazo 12 → *Enviar solicitud*.
   - La pantalla muestra **"Pendiente de inventario"** con el aviso de que el riesgo favorable aún no es una
     aprobación. En 1–2 s cambia sola a **"Aprobada"** (polling cada 2 s).
   - Se ven la probabilidad frente al umbral, la versión del modelo, la cotización usada (precio y versión),
     la reserva y su vencimiento, y el historial.
   - El botón queda deshabilitado mientras se envía. Si la red falla, el reintento usa la **misma**
     Idempotency-Key.
2. **Rechazo por riesgo (caso 2).** *Salir* → **Bruno** → solicitar cualquier producto. Queda **"Rechazada"** con
   el motivo, la probabilidad y el modelo, sin reserva (Inventario → Reservas no muestra nada para ese préstamo).
3. **Falta de inventario (caso 3).** **Ana** solicita *Portátil Última Unidad* → **Aprobada** (era la última).
   **Carla** solicita el mismo producto → **"Sin existencias"** (`CANCELADA_SIN_STOCK`). En Inventario el
   disponible es 0, nunca negativo.
4. **Cancelación (caso 7).** **Ana** → Mis préstamos → el préstamo de *Última Unidad* → *Cancelar solicitud*.
   Pasa por **"Cancelando"** y termina en **"Cancelada"**. La unidad vuelve a disponible.
5. **Entrega.** **Administrador** → Inventario → pestaña *Entregas y cancelaciones* → *Entregar* el portátil de
   Ana. Pasa a "Entrega en curso" y luego a **"Completada"**. En *Movimientos* aparece la `SALIDA`.
6. **Cambio de precio e invalidación de caché (casos 8 y 9).** **Administrador** → Catálogo → *Tableta Llanos
   10* → el detalle muestra la etiqueta `MISS` o `HIT`; *Consultar otra vez* muestra `HIT`.
   - *Editar / cambiar precio* → por ejemplo 1.500.000 → *Guardar*. El detalle vuelve a `MISS` con el precio
     nuevo y la versión aumenta.
   - Como **Carla**, solicitar la tableta: en la cotización del préstamo aparece el precio nuevo y su versión.
   - En el catálogo del administrador, el panel *Caché Redis* muestra hits, misses, tasa de aciertos y p50/p95.
7. **Importación de clientes (caso 12).** **Administrador** → Clientes → importar `data/solicitudes_nuevas.csv`
   → 200 insertados. Importarlo **otra vez** → 0 insertados y 200 sin cambios. Importar
   `data/clientes_con_errores.csv` → filas rechazadas con el error de cada una.
8. **Modelo.** **Administrador** → Modelo: versión activa, run ID, umbral, hashes, métricas de prueba, predicciones
   por versión y drift (PSI). *Abrir MLflow* lleva a la comparación de runs.
9. **Préstamos ajenos (caso 13).** Como **Carla**, abrir la URL de un préstamo de Ana
   (`#/prestamo/<id>`). El backend responde 403 y la interfaz muestra el error.
10. **Errores (caso 16).**
    - Cantidad 9 en el formulario: el error aparece junto al campo.
    - **Sesión vencida:** esperar 30 min, o poner `SESION_MINUTOS=1` en `.env` y reiniciar `clientes`. La
      siguiente acción lleva al login con el mensaje "Su sesión venció".
    - **API caída:** `docker compose stop prestamos` → al enviar una solicitud aparece "No se pudo conectar
      con el servicio de Préstamos…" y nada se simula. Con `docker compose start prestamos` se reintenta con la
      misma clave.

## Pruebas por script (fallos, concurrencia, caché y reentregas)

Desde la raíz, con el sistema arriba (`pip install -r scripts/requirements.txt`):

```bash
python scripts/pruebas/aceptacion.py            # todas (unos 3 minutos; detiene y arranca contenedores)
python scripts/pruebas/aceptacion.py 4 5 6      # concurrencia, idempotencia, caídas
python scripts/pruebas/aceptacion.py --rapido 8 # Redis sin esperar los 60 s del TTL
python scripts/bench_cache.py --con-caida       # p50/p95 y tasa de aciertos
python scripts/simular_drift.py                 # drift para el monitoreo
```

Después de cada script, el resultado se puede **observar en la interfaz**: préstamos y estados, inventario, la
pestaña Mensajería y el modelo. En la consola de RabbitMQ, la cola `credimarket.dlq` contiene los mensajes de
la prueba 17.

| # | Caso | Cómo se demuestra |
|---|---|---|
| 1 | Riesgo favorable con existencias | Recorrido paso 1 y 5 · script 1 |
| 2 | Solicitud rechazada | Paso 2 · script 2 |
| 3 | Sin existencias | Paso 3 · script 3 |
| 4 | Última unidad, dos solicitudes simultáneas | Script 4: dos hilos solicitan el producto 6; uno APROBADA y otro CANCELADA_SIN_STOCK |
| 5 | Petición y evento repetidos | Script 5: 5 POST concurrentes con la misma clave dan 1 préstamo; otro cuerpo da 409; se republican el comando y el evento (mismo y distinto `event_id`) y queda 1 reserva y 1 efecto |
| 6 | Caída del consumidor o del broker | Script 6: `stop inventario`, solicitud en 202, `start` y APROBADA; `stop rabbitmq`, solicitud en 202 (outbox), `start` y APROBADA, sin duplicados |
| 7 | Cancelación y evento tardío | Paso 4 · script 7 publica un `InventarioReservado` tardío: el préstamo sigue CANCELADA y el historial lo muestra como "evento ignorado" |
| 8 | Redis vacío, expirado o caído | Paso 6 · script 8 |
| 9 | Precio modificado | Paso 6 · script 9 |
| 10 | Modelo exportado vs. API | Script 10 (diferencia máxima 4,6·10⁻⁷) |
| 11 | Promoción y rollback | `python ml/promover.py --version v1 --motivo "rollback demo"` → Modelo → *Recargar versión promovida* → v1; repetir con v2. Script 11 |
| 12 | CSV dos veces | Paso 7 · script 12 |
| 13 | Préstamos ajenos | Paso 9 · script 13 |
| 14 | Flujo desde el frontend | Pasos 1–8 (todas las operaciones son reales y persisten en las bases) |
| 15 | Pendiente y respuesta asíncrona | Paso 1: "Pendiente de inventario" cambia solo a "Aprobada" |
| 16 | Formulario inválido, sesión vencida, API caída | Paso 10 · script 16 |
| extra 17 | Dead letter | Script 17 + consola de RabbitMQ |
| extra 18 | Vencimiento de la reserva | `RESERVA_TTL_SEGUNDOS=60` en `.env` → `docker compose up -d inventario` → script 18 (o aprobar y esperar en la interfaz: termina en "Vencida") |

## Reiniciar los datos de la demo

```bash
docker compose down -v      # borra las bases y las colas (los experimentos de MLflow se conservan)
docker compose up -d
```
