"""Carga y uso del modelo de riesgo activo.

Se carga UNA vez al iniciar (o con POST /modelo/recargar, explícito) la versión indicada en
MODELOS_DIR/activo.json. Nunca se entrena por petición ni se toma "el último archivo".
Antes de usarlo se verifica que el hash del artefacto coincida con el manifiesto.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
from pydantic import BaseModel, Field, model_validator

from credimarket_ml.features import ENTRADAS, PLAZOS_VALIDOS, a_dataframe

log = logging.getLogger("modelo")
MODELOS_DIR = Path(os.environ.get("MODELOS_DIR", "/models"))


class EntradaRiesgo(BaseModel):
    """Características de entrada (sin identificadores ni etiqueta)."""

    ingresos_mensuales_cop: float = Field(gt=0, le=100_000_000)
    obligaciones_mensuales_cop: float = Field(ge=0, le=100_000_000)
    tipo_empleo: Literal["dependiente", "independiente", "temporal"] | None = None
    antiguedad_laboral_meses: float | None = Field(default=None, ge=0, le=600)
    meses_historial_crediticio: float | None = Field(default=None, ge=0, le=600)
    creditos_previos: int = Field(ge=0, le=100)
    atrasos_previos_30d: int = Field(ge=0, le=100)
    porcentaje_pago_puntual: float | None = Field(default=None, ge=0, le=100)
    monto_solicitado_cop: float = Field(gt=0, le=100_000_000)
    plazo_meses: int

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _reglas(self):
        if self.plazo_meses not in PLAZOS_VALIDOS:
            raise ValueError(f"plazo_meses debe ser uno de {PLAZOS_VALIDOS}")
        if self.creditos_previos == 0 and self.porcentaje_pago_puntual is not None:
            raise ValueError("porcentaje_pago_puntual debe estar vacío si no hay créditos previos")
        return self


class ModeloActivo:
    def __init__(self):
        self.lock = threading.Lock()
        self.pipeline = None
        self.manifiesto: dict | None = None
        self.activo: dict | None = None
        self.error: str | None = None

    @property
    def listo(self) -> bool:
        return self.pipeline is not None

    def cargar(self) -> None:
        try:
            activo = json.loads((MODELOS_DIR / "activo.json").read_text(encoding="utf-8"))
            carpeta = MODELOS_DIR / activo["version"]
            manifiesto = json.loads((carpeta / "manifiesto.json").read_text(encoding="utf-8"))
            artefacto = carpeta / manifiesto["artefacto"]["archivo"]
            real = hashlib.sha256(artefacto.read_bytes()).hexdigest()
            if real != manifiesto["artefacto"]["sha256"]:
                raise RuntimeError("el hash del artefacto no coincide con el manifiesto")
            if manifiesto["variables_entrada"] != ENTRADAS:
                raise RuntimeError("las variables del manifiesto no coinciden con las del servicio")
            pipeline = joblib.load(artefacto)
            with self.lock:
                self.pipeline, self.manifiesto, self.activo, self.error = pipeline, manifiesto, activo, None
            log.info("Modelo %s cargado (run %s, umbral %s)", manifiesto["model_version"], manifiesto["run_id"],
                     manifiesto["umbral"])
        except Exception as e:
            self.error = repr(e)
            log.error("No se pudo cargar el modelo: %r", e)
            raise

    def predecir(self, entrada: EntradaRiesgo) -> dict:
        with self.lock:
            pipeline, m = self.pipeline, self.manifiesto
        if pipeline is None:
            raise RuntimeError("modelo no disponible")
        prob = float(pipeline.predict_proba(a_dataframe([entrada.model_dump()]))[:, 1][0])
        umbral = float(m["umbral"])
        return {
            "probabilidad_incumplimiento": round(prob, 6),
            "decision_riesgo": "DESFAVORABLE" if prob >= umbral else "FAVORABLE",
            "umbral": umbral,
            "model_version": m["model_version"],
            "run_id": m["run_id"],
            "nota": "La decisión de riesgo no es la aprobación final del préstamo.",
        }

    def info(self) -> dict:
        m = self.manifiesto or {}
        return {
            "listo": self.listo,
            "error": self.error,
            "model_version": m.get("model_version"),
            "run_id": m.get("run_id"),
            "algoritmo": m.get("algoritmo"),
            "umbral": m.get("umbral"),
            "clase_positiva": m.get("clase_positiva"),
            "variables_entrada": m.get("variables_entrada"),
            "dataset": m.get("dataset"),
            "metricas_prueba": {k: m["metricas"]["prueba"][k] for k in ("average_precision", "roc_auc", "recall_1",
                                                                         "precision_1", "brier")} if m else None,
            "sha256_artefacto": (m.get("artefacto") or {}).get("sha256"),
            "promocion": self.activo,
            "mlflow_url": os.environ.get("MLFLOW_PUBLIC_URL", "http://localhost:5000"),
        }


def psi(referencia: dict, valores: list[float]) -> float | None:
    """Population Stability Index entre la distribución de entrenamiento y los valores recientes."""
    valores = [v for v in valores if v is not None]
    if len(valores) < 30:
        return None
    cortes = referencia["cortes"]
    conteo = np.histogram(np.clip(valores, cortes[0], cortes[-1]), bins=cortes)[0]
    actual = np.clip(conteo / conteo.sum(), 1e-4, None)
    esperado = np.clip(np.array(referencia["proporciones"]), 1e-4, None)
    return round(float(np.sum((actual - esperado) * np.log(actual / esperado))), 4)
