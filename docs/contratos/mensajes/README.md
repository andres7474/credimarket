# Contratos de mensajes (schema_version 1)

- Esquema formal (JSON Schema 2020-12): [`mensaje.schema.json`](mensaje.schema.json). Define el sobre común y
  el payload de cada tipo.
- Ejemplos reales tomados de los outbox: [`ejemplos/`](ejemplos/). Todos validan contra el esquema.
- Implementación: `comun/credimarket_comun/mensajeria.py` (`crear_mensaje` y `validar_mensaje`).

## Sobre común

| Campo | Tipo | Uso |
|---|---|---|
| `event_id` | UUID | Identificador único del mensaje y clave de deduplicación (`mensajes_procesados`). También se envía como `message_id` de AMQP. |
| `type` | texto | Tipo del mensaje (tabla siguiente). Decide la routing key. |
| `schema_version` | entero | `1`. Un consumidor rechaza (envía al DLQ) una versión que no conoce. |
| `occurred_at` | fecha ISO-8601 UTC | Momento en que el productor generó el mensaje. |
| `correlation_id` | UUID | Igual para todos los mensajes de una solicitud de préstamo. Permite seguirla en los logs. |
| `prestamo_id` | UUID | Préstamo afectado. |
| `producer` | `prestamos` \| `inventario` | Servicio que publicó el mensaje. |
| `payload` | objeto | Datos propios del tipo. |

**Minimización de datos:** Inventario solo recibe producto, cantidad y reserva. Nunca recibe ingresos,
historial crediticio, la probabilidad del modelo ni otros datos del cliente.

## Tipos

| Tipo | Clase | Productor → consumidor | Exchange / routing key | Payload |
|---|---|---|---|---|
| `ReservaSolicitada` | comando | Préstamos → Inventario | `credimarket.comandos` / `inventario.reservar` | `producto_id`, `cantidad` |
| `LiberacionSolicitada` | comando | Préstamos → Inventario | `credimarket.comandos` / `inventario.liberar` | `producto_id`, `cantidad`, `motivo` (`cancelacion` \| `tiempo_agotado`) |
| `EntregaSolicitada` | comando | Préstamos → Inventario | `credimarket.comandos` / `inventario.entregar` | `reserva_id`, `producto_id`, `cantidad` |
| `InventarioReservado` | evento | Inventario → Préstamos | `credimarket.eventos` / `inventario.reservado` | `reserva_id`, `producto_id`, `cantidad`, `expira_en` |
| `InventarioInsuficiente` | evento | Inventario → Préstamos | `credimarket.eventos` / `inventario.insuficiente` | `producto_id`, `cantidad`, `disponible` |
| `ReservaLiberada` | evento | Inventario → Préstamos | `credimarket.eventos` / `inventario.liberada` | `producto_id`, `cantidad_liberada` (0 si no había reserva activa), `estado_reserva_previo` |
| `SalidaConfirmada` | evento | Inventario → Préstamos | `credimarket.eventos` / `inventario.salida_confirmada` | `reserva_id`, `producto_id`, `cantidad` |
| `EntregaRechazada` | evento | Inventario → Préstamos | `credimarket.eventos` / `inventario.entrega_rechazada` | `motivo` |
| `ReservaVencida` | evento | Inventario → Préstamos | `credimarket.eventos` / `inventario.vencida` | `reserva_id`, `producto_id`, `cantidad` |

## Ejemplo

```json
{
  "event_id": "fcb79c7e-208c-4608-b5b3-cd7251749659",
  "type": "InventarioReservado",
  "schema_version": 1,
  "occurred_at": "2026-11-07T22:36:24.976+00:00",
  "correlation_id": "5d3091ff-9f7c-4744-9f8f-d1f9b7ac29cf",
  "prestamo_id": "958a4059-6cef-4cb9-ae36-0dd913366421",
  "producer": "inventario",
  "payload": {
    "reserva_id": "2fa2b7b2-8415-43df-b877-2be199b12b23",
    "producto_id": 2,
    "cantidad": 1,
    "expira_en": "2026-11-07T23:06:24.974776+00:00"
  }
}
```

## Propiedades AMQP

`content_type=application/json`, `delivery_mode=2` (persistente), `message_id=event_id`, `mandatory=true` y
publicación con *publisher confirms*. Los consumidores confirman manualmente (`basic_ack`) después del COMMIT.

## Evolución del contrato

- **Cambio compatible** (agregar un campo opcional al payload): se mantiene `schema_version: 1`. Los
  consumidores ignoran los campos que no conocen.
- **Cambio incompatible** (renombrar o quitar un campo, cambiar su tipo): se publica con `schema_version: 2`.
  Primero se despliega el consumidor que acepta 1 y 2, y después el productor.
- Un mensaje con una versión o un tipo desconocidos es un error de contrato: va al DLQ sin reintentos.
