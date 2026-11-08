CREATE TABLE IF NOT EXISTS prestamos (
    id                  UUID PRIMARY KEY,
    cliente_id          INT NOT NULL,
    producto_id         INT NOT NULL,
    cantidad            INT NOT NULL CHECK (cantidad > 0),
    plazo_meses         INT NOT NULL,
    precio_unitario     NUMERIC(14, 2) NOT NULL,
    version_cotizacion  INT NOT NULL,
    monto               NUMERIC(16, 2) NOT NULL,
    estado              TEXT NOT NULL,
    motivo              TEXT,
    probabilidad        NUMERIC(8, 6),
    decision_riesgo     TEXT,
    umbral              NUMERIC(6, 4),
    model_version       TEXT,
    model_run_id        TEXT,
    reserva_id          UUID,
    reserva_expira_en   TIMESTAMPTZ,
    correlation_id      UUID NOT NULL,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prestamos_cliente ON prestamos (cliente_id, creado_en DESC);
CREATE INDEX IF NOT EXISTS prestamos_estado ON prestamos (estado, actualizado_en);

-- Idempotencia HTTP: la restricción durable impide duplicados concurrentes.
CREATE TABLE IF NOT EXISTS idempotencia (
    cliente_id      INT NOT NULL,
    clave           TEXT NOT NULL,
    hash_solicitud  TEXT NOT NULL,
    prestamo_id     UUID NOT NULL REFERENCES prestamos(id),
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cliente_id, clave)
);

CREATE TABLE IF NOT EXISTS historial_estados (
    id              BIGSERIAL PRIMARY KEY,
    prestamo_id     UUID NOT NULL REFERENCES prestamos(id),
    estado_anterior TEXT,
    estado_nuevo    TEXT NOT NULL,
    motivo          TEXT,
    evento_id       UUID,
    aplicado        BOOLEAN NOT NULL DEFAULT TRUE,   -- FALSE = evento tardío registrado pero ignorado
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS historial_prestamo ON historial_estados (prestamo_id, id);

-- Monitoreo del modelo: cada inferencia con su versión.
CREATE TABLE IF NOT EXISTS predicciones (
    id              BIGSERIAL PRIMARY KEY,
    prestamo_id     UUID,
    origen          TEXT NOT NULL,          -- prestamo | predict
    model_version   TEXT NOT NULL,
    entradas        JSONB NOT NULL,
    probabilidad    NUMERIC(8, 6) NOT NULL,
    decision        TEXT NOT NULL,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);
