CREATE TABLE IF NOT EXISTS clientes (
    id                          SERIAL PRIMARY KEY,
    cliente_externo_id          TEXT NOT NULL UNIQUE,      -- clave externa única (p. ej. CLI-0012001)
    nombre                      TEXT NOT NULL,
    documento                   TEXT UNIQUE,
    email                       TEXT UNIQUE,
    activo                      BOOLEAN NOT NULL DEFAULT TRUE,
    ingresos_mensuales_cop      BIGINT CHECK (ingresos_mensuales_cop >= 0),
    obligaciones_mensuales_cop  BIGINT CHECK (obligaciones_mensuales_cop >= 0),
    tipo_empleo                 TEXT CHECK (tipo_empleo IN ('dependiente', 'independiente', 'temporal')),
    antiguedad_laboral_meses    NUMERIC(6, 1) CHECK (antiguedad_laboral_meses >= 0),
    meses_historial_crediticio  NUMERIC(6, 1) CHECK (meses_historial_crediticio >= 0),
    creditos_previos            INT CHECK (creditos_previos >= 0),
    atrasos_previos_30d         INT CHECK (atrasos_previos_30d >= 0),
    porcentaje_pago_puntual     NUMERIC(5, 2) CHECK (porcentaje_pago_puntual BETWEEN 0 AND 100),
    origen                      TEXT NOT NULL DEFAULT 'registro',
    creado_en                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usuarios (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    rol             TEXT NOT NULL CHECK (rol IN ('cliente', 'admin')),
    cliente_id      INT UNIQUE REFERENCES clientes(id),
    nombre          TEXT NOT NULL,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS importaciones (
    id              SERIAL PRIMARY KEY,
    archivo         TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    filas           INT NOT NULL,
    insertados      INT NOT NULL,
    actualizados    INT NOT NULL,
    sin_cambios     INT NOT NULL,
    rechazados      INT NOT NULL,
    realizada_por   TEXT NOT NULL,
    realizada_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);
