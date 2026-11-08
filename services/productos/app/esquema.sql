CREATE TABLE IF NOT EXISTS productos (
    id              SERIAL PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,
    nombre          TEXT NOT NULL,
    descripcion     TEXT NOT NULL DEFAULT '',
    categoria       TEXT NOT NULL CHECK (categoria IN ('computadores', 'celulares', 'tablets', 'accesorios', 'otros')),
    precio          NUMERIC(14, 2) NOT NULL CHECK (precio > 0),
    version         INT NOT NULL DEFAULT 1,
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Historial de precios: permite verificar qué precio tenía cada versión de la cotización.
CREATE TABLE IF NOT EXISTS historial_precios (
    producto_id     INT NOT NULL REFERENCES productos(id),
    version         INT NOT NULL,
    precio          NUMERIC(14, 2) NOT NULL,
    registrado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (producto_id, version)
);
