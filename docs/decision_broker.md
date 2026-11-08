# Decisión de arquitectura: RabbitMQ en lugar de Kafka

**Decisión:** el proyecto usa **RabbitMQ 4.1** con colas *quorum* para los comandos y eventos entre Préstamos e
Inventario. Kafka se descarta para la versión actual. Al final se indica qué cambio de requisitos haría
reconsiderarlo.

## 1. Volumen, consumidores y patrón de comunicación

- **Volumen asumido:** tienda en línea de tamaño medio, con unos cientos a pocos miles de solicitudes de
  financiación al día. Cada solicitud favorable genera de 2 a 4 mensajes (reserva, resultado, entrega o
  liberación, confirmación). En el pico supuesto (campañas de descuento, 10× el promedio) son decenas de
  mensajes por segundo, varios órdenes de magnitud por debajo de lo que soporta un solo nodo de RabbitMQ.
- **Consumidores:** un consumidor lógico por cola: Inventario consume comandos y Préstamos consume eventos.
  Cada servicio puede escalar a varias réplicas que compiten por la misma cola.
- **Patrón:** *comando → evento*. Préstamos **ordena** a Inventario algo concreto (`ReservaSolicitada`,
  `LiberacionSolicitada`, `EntregaSolicitada`) y Inventario **informa** el resultado (`InventarioReservado`,
  `InventarioInsuficiente`, `ReservaLiberada`, `SalidaConfirmada`, `EntregaRechazada`, `ReservaVencida`). Es
  trabajo punto a punto con enrutamiento por tipo, que es justo el modelo de exchanges y colas de RabbitMQ.

## 2. ¿Hace falta conservar y reproducir eventos?

**No en el broker.** La fuente de verdad del negocio está en las bases de datos de cada servicio:

- `prestamos_db.historial_estados` guarda cada transición, incluidos los eventos ignorados (`aplicado = false`).
- Las tablas `outbox` de Préstamos e Inventario conservan todo mensaje publicado, con su `event_id` y su fecha.
- `mensajes_procesados` guarda qué se consumió y con qué resultado.
- `inventario_db.movimientos` es el libro de entradas y salidas de unidades.

Un mensaje se borra de la cola en cuanto se confirma (`ack`). La auditoría y la reproducción se hacen desde
esas tablas: por ejemplo, `GET /prestamos/{id}/mensajes` y `GET /mensajeria/outbox?prestamo_id=`, y los
scripts de prueba republican mensajes tomados de ahí. Si un servicio necesitara reconstruir su estado leyendo
eventos de meses atrás (*event sourcing*), la retención de Kafka sería una ventaja; hoy ese caso no existe.
Los mensajes que no se pueden procesar sí se conservan en la cola `credimarket.dlq` hasta que un operador los
revise.

## 3. Distribución del trabajo y orden de procesamiento

| Elemento | Configuración |
|---|---|
| Exchange `credimarket.comandos` | `direct`, durable. Routing keys `inventario.reservar`, `inventario.liberar`, `inventario.entregar` |
| Exchange `credimarket.eventos` | `topic`, durable. Routing keys `inventario.reservado`, `inventario.insuficiente`, `inventario.liberada`, `inventario.salida_confirmada`, `inventario.entrega_rechazada`, `inventario.vencida` |
| Cola `inventario.comandos` | quorum, durable, ligada a las tres routing keys de comandos |
| Cola `prestamos.eventos` | quorum, durable, ligada a `inventario.#` |
| Exchange `credimarket.dlx` y cola `credimarket.dlq` | `fanout` y quorum. Destino de mensajes inválidos o que agotaron sus reintentos |
| Argumentos de las colas de trabajo | `x-queue-type=quorum`, `x-delivery-limit=3` (`MAX_REINTENTOS`), `x-dead-letter-exchange=credimarket.dlx` |
| Prefetch | `basic_qos(prefetch_count=10)` por consumidor |

- **Distribución:** si hay varias réplicas de Inventario, RabbitMQ reparte los mensajes de la cola entre ellas
  (consumidores competidores).
- **Orden:** una cola entrega en orden FIFO, así que con un consumidor `ReservaSolicitada` siempre se procesa
  antes que la `LiberacionSolicitada` del mismo préstamo. **No dependemos de ese orden**, porque un reintento o
  varias réplicas lo rompen:
  - Si llega una liberación sin reserva, se crea una reserva "lápida" (`LIBERADA`, cantidad 0) que bloquea
    una reserva posterior gracias a `UNIQUE(prestamo_id)`.
  - Los eventos que llegan a Préstamos se validan contra la máquina de estados. Un evento que no corresponde
    al estado actual (por ejemplo, `InventarioReservado` sobre un préstamo `CANCELADA`) se registra sin
    aplicarse.
  - La concurrencia sobre el mismo producto se resuelve en PostgreSQL con un `UPDATE` condicional. El orden de
    llegada de dos solicitudes no puede dejar existencias negativas.

## 4. Errores, duplicados, reintentos y mensajes irrecuperables

| Situación | Tratamiento |
|---|---|
| Caída entre guardar el estado y publicar | **Outbox transaccional.** El mensaje se inserta en `outbox` dentro de la misma transacción que el cambio de negocio. Un hilo relay lo publica después. |
| Pérdida en el broker | Mensajes `delivery_mode=2` (persistentes), colas quorum durables, **publisher confirms** y `mandatory=true`. El outbox solo se marca como publicado si el broker confirmó. |
| Duplicados (republicación del relay o reentrega del broker) | `mensajes_procesados(event_id PRIMARY KEY)` se inserta en la misma transacción que el efecto. Si el `event_id` ya existe, se confirma el mensaje sin efecto. Además, `UNIQUE(prestamo_id)` en reservas evita que un mismo préstamo reserve dos veces, aunque el comando llegue con otro `event_id`. |
| Confirmación al broker | `basic_ack` manual **después del COMMIT**. Si el proceso cae antes del ack, el mensaje se reentrega y la deduplicación lo absorbe. |
| Error transitorio (BD caída, bloqueo) | `basic_nack(requeue=true)` tras una espera creciente (0,5 s, 1 s, 1,5 s). RabbitMQ cuenta las entregas (`x-delivery-count`). |
| Error permanente (préstamo inexistente, falla repetida) | Al superar `MAX_REINTENTOS=3`, el consumidor hace `nack(requeue=false)` y el broker lo envía al DLX. El `x-delivery-limit=3` de la cola es una segunda barrera del lado del broker. |
| Mensaje que no cumple el contrato (JSON inválido, falta un campo del sobre, `schema_version` desconocida, tipo desconocido) | Va directo al DLQ, sin reintentos: reintentarlo nunca lo arreglaría. |
| Sin respuesta de Inventario | Préstamos cancela por tiempo los préstamos en `PENDIENTE_INVENTARIO` después de `PENDIENTE_TIMEOUT_SEGUNDOS=600` y pide liberar la reserva. |

La prueba 17 de `scripts/pruebas/aceptacion.py` publica un mensaje corrupto y un evento de un préstamo
inexistente, y verifica que ambos terminen en `credimarket.dlq`.

## 5. Costo de recursos y complejidad operativa

| | RabbitMQ (elegido) | Kafka |
|---|---|---|
| Contenedores para la demo | 1 (`rabbitmq:4.1-management`), con consola web incluida | 1 broker en modo KRaft, más una UI aparte (Kafdrop o AKHQ) si se quiere inspeccionar |
| Memoria en reposo (orden de magnitud) | ~150 MB | ~500 MB–1 GB (JVM) |
| Conceptos que hay que dominar | exchange, cola, binding, ack, DLX | topic, partición, clave, grupo de consumidores, offset, retención, rebalanceo |
| Reintentos y dead letter | nativos (`x-delivery-limit`, DLX) | se construyen con topics de reintento y DLQ propios y una política de commit de offsets |
| Enrutamiento por tipo de mensaje | nativo (routing keys) | por topic o filtrando en el consumidor |
| Retención y reprocesamiento | no (el mensaje se borra tras el ack) | sí, por tiempo o tamaño, reprocesable moviendo el offset |
| Escalado del throughput | vertical o por colas | horizontal por particiones, con cientos de miles de mensajes por segundo |

Para cuatro servicios y un portátil de estudiante, RabbitMQ consume menos memoria, ofrece reintentos y dead
letter sin código adicional, y su consola permite mostrar durante la sustentación las colas, las confirmaciones
y el DLQ.

## 6. Por qué RabbitMQ y qué nos haría cambiar a Kafka

**Elegimos RabbitMQ** porque el problema es de **coordinación de trabajo**: comandos dirigidos a un servicio y
respuestas, con poco volumen, necesidad de reintentos acotados, dead letter y enrutamiento por tipo, y sin
requisito de reprocesar la historia desde el broker. RabbitMQ da todo eso de forma nativa y con menor costo
operativo. Las garantías que importan, sin pérdidas y sin efectos duplicados, no vienen del broker sino del
outbox, de la deduplicación transaccional y de las restricciones de la base de datos. Con Kafka igual
necesitaríamos ese mismo diseño.

**Cambiaríamos a Kafka si:**

1. Aparecen **varios consumidores independientes del mismo flujo de eventos**, como analítica en tiempo real,
   detección de fraude, notificaciones o un *data lake*, y cada uno debe leer a su propio ritmo.
2. Se exige **retener y reproducir eventos** durante días o meses: auditoría regulatoria desde el log,
   reconstruir un servicio nuevo o reentrenar el modelo con el flujo histórico de solicitudes.
3. El volumen crece a **miles de mensajes por segundo** con necesidad de **orden por clave**. Por ejemplo, la
   partición por `producto_id` serializaría las reservas de cada producto.
4. Se adopta *event sourcing* o CDC, por ejemplo con Debezium, para publicar el outbox directamente desde
   el WAL de PostgreSQL.

En ese caso, la migración afecta solo a `comun/credimarket_comun/mensajeria.py`, la capa de publicación y
consumo. Los contratos (sobre JSON versionado), el outbox y la deduplicación se mantienen.
