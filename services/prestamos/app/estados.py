"""Máquina de estados del préstamo.

Estados finales: RECHAZADA, CANCELADA_SIN_STOCK, COMPLETADA, CANCELADA, VENCIDA.

Estados intermedios agregados a los mínimos del enunciado (porque dependen de Inventario):
  ENTREGA_PENDIENTE      se pidió confirmar la salida; mientras tanto no se puede cancelar.
  CANCELACION_PENDIENTE  se pidió liberar la reserva; CANCELADA solo llega tras ReservaLiberada.
VENCIDA: la reserva expiró sin entrega (un préstamo aprobado no conserva una reserva inexistente).

Un evento que no corresponde al estado actual NO cambia el estado: se registra en el
historial con aplicado = FALSE. Así un evento tardío nunca reactiva un préstamo cancelado.
"""
from __future__ import annotations

RECHAZADA = "RECHAZADA"
PENDIENTE_INVENTARIO = "PENDIENTE_INVENTARIO"
APROBADA = "APROBADA"
CANCELADA_SIN_STOCK = "CANCELADA_SIN_STOCK"
ENTREGA_PENDIENTE = "ENTREGA_PENDIENTE"
COMPLETADA = "COMPLETADA"
CANCELACION_PENDIENTE = "CANCELACION_PENDIENTE"
CANCELADA = "CANCELADA"
VENCIDA = "VENCIDA"

FINALES = {RECHAZADA, CANCELADA_SIN_STOCK, COMPLETADA, CANCELADA, VENCIDA}

# (estado actual, disparador) -> estado nuevo
TRANSICIONES = {
    (PENDIENTE_INVENTARIO, "InventarioReservado"): APROBADA,
    (PENDIENTE_INVENTARIO, "InventarioInsuficiente"): CANCELADA_SIN_STOCK,
    (PENDIENTE_INVENTARIO, "cancelar"): CANCELACION_PENDIENTE,
    (PENDIENTE_INVENTARIO, "tiempo_agotado"): CANCELACION_PENDIENTE,
    (PENDIENTE_INVENTARIO, "ReservaVencida"): VENCIDA,
    (APROBADA, "entregar"): ENTREGA_PENDIENTE,
    (APROBADA, "cancelar"): CANCELACION_PENDIENTE,
    (APROBADA, "ReservaVencida"): VENCIDA,
    (ENTREGA_PENDIENTE, "SalidaConfirmada"): COMPLETADA,
    (ENTREGA_PENDIENTE, "EntregaRechazada"): VENCIDA,
    (ENTREGA_PENDIENTE, "ReservaVencida"): VENCIDA,
    (CANCELACION_PENDIENTE, "ReservaLiberada"): CANCELADA,
    (CANCELACION_PENDIENTE, "InventarioInsuficiente"): CANCELADA,
    (CANCELACION_PENDIENTE, "ReservaVencida"): CANCELADA,
}

DESCRIPCION = {
    RECHAZADA: "Rechazada por el modelo de riesgo o por reglas de negocio.",
    PENDIENTE_INVENTARIO: "Riesgo favorable. Esperando la reserva de inventario: aún no está aprobada.",
    APROBADA: "Aprobada: el inventario confirmó la reserva.",
    CANCELADA_SIN_STOCK: "Cancelada: no había unidades disponibles.",
    ENTREGA_PENDIENTE: "Entrega en curso: esperando que Inventario confirme la salida.",
    COMPLETADA: "Entregada.",
    CANCELACION_PENDIENTE: "Cancelando: esperando que Inventario libere la reserva.",
    CANCELADA: "Cancelada; la reserva fue liberada.",
    VENCIDA: "La reserva venció antes de la entrega.",
}


def siguiente(estado: str, disparador: str) -> str | None:
    return TRANSICIONES.get((estado, disparador))
