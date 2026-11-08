CREATE TABLE IF NOT EXISTS existencias (
    producto_id     INT PRIMARY KEY,
    disponible      INT NOT NULL DEFAULT 0 CHECK (disponible >= 0),   -- nunca negativo
    reservado       INT NOT NULL DEFAULT 0 CHECK (reservado >= 0),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservas (
    id              UUID PRIMARY KEY,
    prestamo_id     UUID NOT NULL UNIQUE,     -- a lo sumo una reserva por préstamo
    producto_id     INT NOT NULL,
    cantidad        INT NOT NULL CHECK (cantidad >= 0),
    estado          TEXT NOT NULL CHECK (estado IN ('RESERVADA', 'RECHAZADA', 'LIBERADA', 'CONFIRMADA', 'VENCIDA')),
    motivo          TEXT,
    expira_en       TIMESTAMPTZ,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reservas_por_vencer ON reservas (expira_en) WHERE estado = 'RESERVADA';

CREATE TABLE IF NOT EXISTS movimientos (
    id              BIGSERIAL PRIMARY KEY,
    producto_id     INT NOT NULL,
    tipo            TEXT NOT NULL CHECK (tipo IN ('ENTRADA', 'RESERVA', 'LIBERACION', 'SALIDA', 'VENCIMIENTO')),
    cantidad        INT NOT NULL,
    prestamo_id     UUID,
    referencia      TEXT,
    disponible_despues INT NOT NULL,
    reservado_despues  INT NOT NULL,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS movimientos_producto ON movimientos (producto_id, id DESC);
