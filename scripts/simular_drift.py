"""Simulación de drift para el monitoreo del modelo (etapa F del ciclo MLOps).

1. Envía las 200 solicitudes de data/solicitudes_nuevas.csv tal cual a POST /riesgo/predict
   -> la distribución se parece a la de entrenamiento (PSI bajo).
2. Envía las mismas solicitudes con un choque simulado (ingresos -35 %, obligaciones +60 %,
   monto +80 %, más atrasos) -> GET /modelo/monitoreo debe marcar drift moderado/alto.
Guarda ambos reportes en docs/evidencias/drift_*.json.

Uso: python scripts/simular_drift.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent / "pruebas"))
import aceptacion as a  # noqa: E402  (reutiliza .env y el login)

RAIZ = Path(__file__).resolve().parents[1]
CAMPOS = ["ingresos_mensuales_cop", "obligaciones_mensuales_cop", "tipo_empleo", "antiguedad_laboral_meses",
          "meses_historial_crediticio", "creditos_previos", "atrasos_previos_30d", "porcentaje_pago_puntual",
          "monto_solicitado_cop", "plazo_meses"]
ENTEROS = {"ingresos_mensuales_cop", "obligaciones_mensuales_cop", "creditos_previos", "atrasos_previos_30d",
           "plazo_meses", "monto_solicitado_cop"}


def entrada(fila: dict) -> dict:
    e = {}
    for c in CAMPOS:
        v = fila[c]
        e[c] = None if v == "" else v if c == "tipo_empleo" else (int(float(v)) if c in ENTEROS else float(v))
    return e


def choque(e: dict) -> dict:
    e = dict(e)
    e["ingresos_mensuales_cop"] = int(e["ingresos_mensuales_cop"] * 0.65)
    e["obligaciones_mensuales_cop"] = int(e["obligaciones_mensuales_cop"] * 1.6)
    e["monto_solicitado_cop"] = min(int(e["monto_solicitado_cop"] * 1.8), 15_000_000)
    e["atrasos_previos_30d"] = e["atrasos_previos_30d"] + 1
    return e


def lote(entradas: list[dict], nombre: str) -> dict:
    desfavorables = 0
    for e in entradas:
        r = requests.post(f"{a.URL['prestamos']}/riesgo/predict", headers=a.ADMIN, json=e, timeout=10)
        r.raise_for_status()
        desfavorables += r.json()["decision_riesgo"] == "DESFAVORABLE"
    m = requests.get(f"{a.URL['prestamos']}/modelo/monitoreo?ventana={len(entradas)}", headers=a.ADMIN, timeout=10).json()
    resumen = {"lote": nombre, "solicitudes": len(entradas), "tasa_desfavorable_lote": round(desfavorables / len(entradas), 3),
               "drift": m["drift"]}
    destino = RAIZ / "docs" / "evidencias" / f"drift_{nombre}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n== {nombre}: {len(entradas)} solicitudes, {resumen['tasa_desfavorable_lote']:.1%} desfavorables")
    for variable, d in sorted(m["drift"]["variables"].items(), key=lambda kv: -kv[1]["psi"]):
        print(f"   {variable:32s} PSI={d['psi']:.3f}  {d['alerta']}")
    return resumen


def main():
    filas = list(csv.DictReader(open(RAIZ / "data" / "solicitudes_nuevas.csv", encoding="utf-8")))
    base = [entrada(f) for f in filas]
    lote(base, "sin_cambios")
    lote([choque(e) for e in base], "con_choque")
    print("\nCriterio: PSI < 0,1 estable; 0,1-0,25 moderado; > 0,25 alto (revisar o reentrenar).")


if __name__ == "__main__":
    main()
