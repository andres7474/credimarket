"""Etapa D: exportar el pipeline de una corrida de MLflow como versión identificable.

Uso:
  MLFLOW_TRACKING_URI=http://localhost:5000 python ml/exportar.py --version v2 --rol seleccionado
  MLFLOW_TRACKING_URI=http://localhost:5000 python ml/exportar.py --version v1 --rol alternativo

Crea models/<version>/ con:
  pipeline.joblib            preprocesamiento + estimador
  manifiesto.json            versión, run_id, datos, entradas, umbral, métricas, dependencias, hash
  ejemplo_entrada.json / ejemplo_salida.json
  predicciones_referencia.csv  salida del pipeline para data/solicitudes_nuevas.csv (prueba de equivalencia)
Exportar NO activa la versión: eso lo hace ml/promover.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version as version_paquete
from pathlib import Path

import joblib
import mlflow
import numpy as np
import pandas as pd

from credimarket_ml.features import CLASE_POSITIVA, ENTRADAS, EXCLUIDAS, a_dataframe

RAIZ = Path(__file__).resolve().parents[1]
MODELOS = RAIZ / "models"
SELECCION = RAIZ / "ml" / "salidas" / "entrenamiento" / "seleccion.json"
MANIFIESTO_DATOS = RAIZ / "ml" / "salidas" / "datos" / "manifiesto_datos.json"
NUEVAS = RAIZ / "data" / "solicitudes_nuevas.csv"
DATOS = RAIZ / "data" / "prestamos_entrenamiento.csv"
PARTICIONES = RAIZ / "ml" / "salidas" / "datos" / "particiones"


def referencia_drift() -> dict:
    """Deciles de cada variable numérica en ENTRENAMIENTO: base para calcular PSI en producción."""
    df = pd.read_csv(DATOS).set_index("solicitud_id")
    entrenamiento = df.loc[pd.read_csv(PARTICIONES / "entrenamiento_ids.csv")["solicitud_id"]]
    ref = {}
    for c in ENTRADAS:
        if c == "tipo_empleo":
            continue
        x = entrenamiento[c].dropna().to_numpy(dtype=float)
        cortes = np.unique(np.quantile(x, np.linspace(0, 1, 11)))
        if len(cortes) < 3:
            continue
        conteo = np.histogram(x, bins=cortes)[0]
        ref[c] = {"cortes": [float(v) for v in cortes], "proporciones": [float(v) for v in conteo / conteo.sum()]}
    return ref


def sha256(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--version", required=True, help="identificador de la versión, p. ej. v2")
    p.add_argument("--rol", required=True, choices=["seleccionado", "alternativo"])
    a = p.parse_args()

    seleccion = json.loads(SELECCION.read_text(encoding="utf-8"))["versiones"][a.rol]
    datos = json.loads(MANIFIESTO_DATOS.read_text(encoding="utf-8"))
    destino = MODELOS / a.version
    if destino.exists():
        raise SystemExit(f"{destino} ya existe: una versión exportada no se sobrescribe.")

    with tempfile.TemporaryDirectory() as tmp:
        ruta = mlflow.artifacts.download_artifacts(run_id=seleccion["run_id"], artifact_path="pipeline.joblib",
                                                   dst_path=tmp)
        destino.mkdir(parents=True)
        shutil.copy(ruta, destino / "pipeline.joblib")

    pipeline = joblib.load(destino / "pipeline.joblib")
    umbral = seleccion["umbral"]

    nuevas = pd.read_csv(NUEVAS)
    prob = pipeline.predict_proba(a_dataframe(nuevas[ENTRADAS].to_dict("records")))[:, 1]
    referencia = pd.DataFrame({"solicitud_id": nuevas["solicitud_id"], "probabilidad_incumplimiento": prob,
                               "decision_riesgo": ["DESFAVORABLE" if x >= umbral else "FAVORABLE" for x in prob]})
    referencia.to_csv(destino / "predicciones_referencia.csv", index=False)

    ejemplo = {k: (None if pd.isna(v) else v) for k, v in nuevas.iloc[0][ENTRADAS].to_dict().items()}
    (destino / "ejemplo_entrada.json").write_text(json.dumps(ejemplo, indent=2, ensure_ascii=False, default=float))
    (destino / "ejemplo_salida.json").write_text(json.dumps({
        "probabilidad_incumplimiento": round(float(prob[0]), 6),
        "decision_riesgo": referencia.loc[0, "decision_riesgo"],
        "umbral": umbral,
        "model_version": a.version,
    }, indent=2, ensure_ascii=False))

    metricas = {
        "validacion": {k: v for k, v in seleccion["validacion"].items()},
        "prueba": {k: v for k, v in seleccion["prueba"].items()},
    }
    manifiesto = {
        "model_version": a.version,
        "run_id": seleccion["run_id"],
        "corrida": seleccion["corrida"],
        "algoritmo": seleccion["algoritmo"],
        "experimento_mlflow": "credimarket-riesgo",
        "exportado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": {"version": datos["version_datos"], "sha256": datos["sha256"]},
        "variables_entrada": ENTRADAS,
        "variables_excluidas": EXCLUIDAS,
        "objetivo": "incumplimiento_90d",
        "clase_positiva": CLASE_POSITIVA,
        "umbral": umbral,
        "regla_decision": "DESFAVORABLE si probabilidad_incumplimiento >= umbral",
        "metricas": metricas,
        "dependencias": {p: version_paquete(p) for p in ("scikit-learn", "numpy", "pandas", "joblib", "credimarket-ml")},
        "artefacto": {"archivo": "pipeline.joblib", "sha256": sha256(destino / "pipeline.joblib")},
        "referencia_drift": referencia_drift(),
    }
    (destino / "manifiesto.json").write_text(json.dumps(manifiesto, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Exportada {a.version} desde {seleccion['corrida']} ({seleccion['run_id']}), umbral {umbral}")


if __name__ == "__main__":
    main()
