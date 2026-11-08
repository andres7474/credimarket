"""Características del modelo de riesgo.

Este módulo lo comparten el entrenamiento (ml/) y el servicio de Préstamos, para que
ambos construyan exactamente las mismas variables. El pipeline exportado lo referencia,
así que el servicio debe tener este paquete instalado con la misma versión.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

VERSION_CARACTERISTICAS = "1.0.0"

# Tasa mensual ficticia: constante en todo el histórico (0,018). No es predictora;
# solo se usa como parámetro fijo para derivar la cuota a partir de monto y plazo.
TASA_MENSUAL = 0.018

OBJETIVO = "incumplimiento_90d"
CLASE_POSITIVA = 1  # 1 = incumplió en los 90 días siguientes

# Columnas que se excluyen y por qué (se documenta en el informe del modelo).
EXCLUIDAS = {
    "solicitud_id": "identificador, solo trazabilidad",
    "cliente_id": "identificador, solo trazabilidad",
    "tasa_mensual_simulada": "constante (0,018) en todo el histórico",
    "cuota_estimada_cop": "redundante: el pipeline la recalcula desde monto, plazo y tasa",
    OBJETIVO: "variable objetivo; nunca es entrada",
}

# Entradas crudas que recibe el pipeline (lo que envía el servicio de Préstamos).
ENTRADAS = [
    "ingresos_mensuales_cop",
    "obligaciones_mensuales_cop",
    "tipo_empleo",
    "antiguedad_laboral_meses",
    "meses_historial_crediticio",
    "creditos_previos",
    "atrasos_previos_30d",
    "porcentaje_pago_puntual",
    "monto_solicitado_cop",
    "plazo_meses",
]

CATEGORIAS_EMPLEO = ["dependiente", "independiente", "temporal"]
PLAZOS_VALIDOS = [6, 12, 18, 24, 36]

DERIVADAS = ["cuota_calculada_cop", "carga_financiera", "monto_sobre_ingreso", "sin_historial_pago"]

NUMERICAS = [c for c in ENTRADAS if c != "tipo_empleo"] + DERIVADAS
CATEGORICAS = ["tipo_empleo"]


def cuota_francesa(monto, plazo, tasa=TASA_MENSUAL):
    monto = np.asarray(monto, dtype=float)
    plazo = np.asarray(plazo, dtype=float)
    return monto * tasa / (1.0 - (1.0 + tasa) ** (-plazo))


class CaracteristicasDerivadas(BaseEstimator, TransformerMixin):
    """Agrega variables derivadas. No aprende nada del entrenamiento (sin fuga)."""

    def __init__(self, tasa: float = TASA_MENSUAL):
        self.tasa = tasa

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        X = pd.DataFrame(X).copy()
        cuota = cuota_francesa(X["monto_solicitado_cop"], X["plazo_meses"], self.tasa)
        ingresos = X["ingresos_mensuales_cop"].astype(float).clip(lower=1.0)
        X["cuota_calculada_cop"] = cuota
        X["carga_financiera"] = (X["obligaciones_mensuales_cop"].astype(float) + cuota) / ingresos
        X["monto_sobre_ingreso"] = X["monto_solicitado_cop"].astype(float) / ingresos
        X["sin_historial_pago"] = X["porcentaje_pago_puntual"].isna().astype(float)
        return X


def construir_preprocesador(escalar: bool = True) -> Pipeline:
    pasos_num = [("imputar", SimpleImputer(strategy="median", add_indicator=True))]
    if escalar:
        pasos_num.append(("escalar", StandardScaler()))
    columnas = ColumnTransformer(
        [
            ("num", Pipeline(pasos_num), NUMERICAS),
            (
                "cat",
                Pipeline(
                    [
                        ("imputar", SimpleImputer(strategy="constant", fill_value="desconocido")),
                        ("codificar", OneHotEncoder(handle_unknown="ignore")),
                    ]
                ),
                CATEGORICAS,
            ),
        ]
    )
    return Pipeline([("derivadas", CaracteristicasDerivadas()), ("columnas", columnas)])


def construir_pipeline(estimador, escalar: bool = True) -> Pipeline:
    return Pipeline([("preprocesar", construir_preprocesador(escalar)), ("modelo", estimador)])


def a_dataframe(registros) -> pd.DataFrame:
    """Convierte una lista de diccionarios en el DataFrame que espera el pipeline."""
    df = pd.DataFrame(list(registros))
    for c in ENTRADAS:
        if c not in df.columns:
            df[c] = np.nan
    df = df[ENTRADAS]
    for c in ENTRADAS:
        if c != "tipo_empleo":
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["tipo_empleo"] = df["tipo_empleo"].astype(object).where(df["tipo_empleo"].notna(), np.nan)
    return df
