"""Etapa A del ciclo MLOps: validar, versionar y particionar los datos.

Uso:  python ml/preparar_datos.py
Salidas:
  ml/salidas/datos/manifiesto_datos.json   versión, hash SHA-256 y particiones
  ml/salidas/datos/particiones/*.csv       identificadores (solicitud_id) de cada partición
  ml/reportes/eda.md + figuras             análisis exploratorio breve
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402

from credimarket_ml.features import CATEGORIAS_EMPLEO, OBJETIVO, PLAZOS_VALIDOS  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
DATOS = RAIZ / "data" / "prestamos_entrenamiento.csv"
NUEVAS = RAIZ / "data" / "solicitudes_nuevas.csv"
SALIDA = RAIZ / "ml" / "salidas" / "datos"
REPORTES = RAIZ / "ml" / "reportes"
SEMILLA = 42
VERSION_DATOS = "prestamos_entrenamiento-v1"

# Rangos esperados (diccionario de datos). Valores fuera de rango invalidan el archivo.
RANGOS = {
    "ingresos_mensuales_cop": (0, 100_000_000),
    "obligaciones_mensuales_cop": (0, 100_000_000),
    "antiguedad_laboral_meses": (0, 600),
    "meses_historial_crediticio": (0, 600),
    "creditos_previos": (0, 100),
    "atrasos_previos_30d": (0, 100),
    "porcentaje_pago_puntual": (0, 100),
    "monto_solicitado_cop": (1, 100_000_000),
    "plazo_meses": (1, 120),
}


def sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def validar(df: pd.DataFrame) -> dict:
    problemas = []
    columnas = [
        "solicitud_id", "cliente_id", "ingresos_mensuales_cop", "obligaciones_mensuales_cop",
        "tipo_empleo", "antiguedad_laboral_meses", "meses_historial_crediticio", "creditos_previos",
        "atrasos_previos_30d", "porcentaje_pago_puntual", "monto_solicitado_cop", "plazo_meses",
        "tasa_mensual_simulada", "cuota_estimada_cop", OBJETIVO,
    ]
    faltan = [c for c in columnas if c not in df.columns]
    if faltan:
        problemas.append(f"faltan columnas: {faltan}")
    for c, (lo, hi) in RANGOS.items():
        fuera = df[c].dropna()
        fuera = fuera[(fuera < lo) | (fuera > hi)]
        if len(fuera):
            problemas.append(f"{c}: {len(fuera)} valores fuera de [{lo}, {hi}]")
    cats = set(df["tipo_empleo"].dropna().unique()) - set(CATEGORIAS_EMPLEO)
    if cats:
        problemas.append(f"tipo_empleo con categorías desconocidas: {cats}")
    plazos = set(df["plazo_meses"].unique()) - set(PLAZOS_VALIDOS)
    if plazos:
        problemas.append(f"plazos no previstos: {plazos}")
    if set(df[OBJETIVO].unique()) - {0, 1}:
        problemas.append("objetivo con valores distintos de 0/1")
    resumen = {
        "filas": int(len(df)),
        "columnas": int(df.shape[1]),
        "faltantes": {c: int(v) for c, v in df.isna().sum().items() if v},
        "duplicados_solicitud_id": int(df["solicitud_id"].duplicated().sum()),
        "duplicados_cliente_id": int(df["cliente_id"].duplicated().sum()),
        "filas_duplicadas_sin_ids": int(df.drop(columns=["solicitud_id", "cliente_id"]).duplicated().sum()),
        "tasa_mensual_valores_unicos": sorted(df["tasa_mensual_simulada"].unique().tolist()),
        "proporcion_clase_1": round(float(df[OBJETIVO].mean()), 4),
        "problemas": problemas,
    }
    return resumen


def eda(df: pd.DataFrame, nuevas: pd.DataFrame, resumen: dict) -> None:
    REPORTES.mkdir(parents=True, exist_ok=True)
    fig, ejes = plt.subplots(1, 3, figsize=(14, 3.8))
    df[OBJETIVO].value_counts().sort_index().plot.bar(ax=ejes[0], color=["#3b6ea5", "#d9822b"])
    ejes[0].set_title("Clases (0 = pagó, 1 = incumplió)")
    df.groupby("tipo_empleo", dropna=False)[OBJETIVO].mean().plot.bar(ax=ejes[1], color="#3b6ea5")
    ejes[1].set_title("Tasa de incumplimiento por tipo de empleo")
    df.groupby("atrasos_previos_30d")[OBJETIVO].mean().head(8).plot(ax=ejes[2], marker="o")
    ejes[2].set_title("Tasa de incumplimiento por atrasos previos")
    fig.tight_layout()
    fig.savefig(REPORTES / "eda_resumen.png", dpi=110)
    plt.close(fig)

    carga = (df["obligaciones_mensuales_cop"] + df["cuota_estimada_cop"]) / df["ingresos_mensuales_cop"]
    por_carga = df.groupby(pd.qcut(carga, 5), observed=True)[OBJETIVO].mean()
    por_plazo = df.groupby("plazo_meses")[OBJETIVO].mean()
    sin_hist = df.groupby(df["porcentaje_pago_puntual"].isna())[OBJETIVO].mean()

    lineas = [
        "# Análisis exploratorio (etapa A)",
        "",
        f"- Archivo: `data/prestamos_entrenamiento.csv` — versión `{VERSION_DATOS}`",
        f"- SHA-256: `{sha256(DATOS)}`",
        f"- Filas: {resumen['filas']}, columnas: {resumen['columnas']}",
        f"- Proporción de la clase 1 (incumplió): {resumen['proporcion_clase_1']:.1%}",
        f"- Duplicados por `solicitud_id`: {resumen['duplicados_solicitud_id']}; por `cliente_id`: "
        f"{resumen['duplicados_cliente_id']} (cada cliente aparece una vez); filas idénticas: "
        f"{resumen['filas_duplicadas_sin_ids']}",
        f"- `tasa_mensual_simulada` toma un único valor {resumen['tasa_mensual_valores_unicos']}: se excluye.",
        f"- Problemas de validación: {resumen['problemas'] or 'ninguno'}",
        "",
        "## Faltantes",
        "",
        "| Columna | Faltantes | % |",
        "| --- | --- | --- |",
    ]
    for c, v in resumen["faltantes"].items():
        lineas.append(f"| {c} | {v} | {v / resumen['filas']:.1%} |")
    lineas += [
        "",
        "`porcentaje_pago_puntual` falta cuando no hay créditos previos con historial: se imputa con la "
        "mediana de entrenamiento y se agrega un indicador `sin_historial_pago`.",
        "",
        "## Rangos (numéricas)",
        "",
        df.describe().T[["min", "50%", "max"]].round(2).to_markdown(),
        "",
        "## Relación con el objetivo",
        "",
        "| Quintil de carga financiera ((obligaciones + cuota) / ingresos) | Tasa de incumplimiento |",
        "| --- | --- |",
    ]
    lineas += [f"| {k} | {v:.1%} |" for k, v in por_carga.items()]
    lineas += ["", "| Plazo (meses) | Tasa de incumplimiento |", "| --- | --- |"]
    lineas += [f"| {k} | {v:.1%} |" for k, v in por_plazo.items()]
    lineas += [
        "",
        f"Sin historial de pago: {sin_hist.get(True, float('nan')):.1%} de incumplimiento; "
        f"con historial: {sin_hist.get(False, float('nan')):.1%}.",
        "",
        "## Solicitudes nuevas (solo inferencia)",
        "",
        f"- Filas: {len(nuevas)}; sin variable objetivo, por lo que no permiten calcular métricas.",
        f"- Faltantes: {dict((c, int(v)) for c, v in nuevas.isna().sum().items() if v)}",
        "",
        "![EDA](eda_resumen.png)",
        "",
    ]
    (REPORTES / "eda.md").write_text("\n".join(lineas), encoding="utf-8")


def particionar(df: pd.DataFrame) -> dict:
    ids = df[["solicitud_id", "cliente_id", OBJETIVO]]
    # 60/20/20 estratificado por el objetivo con semilla fija. Cada cliente aparece una
    # sola vez, así que partir por solicitud equivale a partir por cliente.
    entrenamiento, resto = train_test_split(ids, test_size=0.4, stratify=ids[OBJETIVO], random_state=SEMILLA)
    validacion, prueba = train_test_split(resto, test_size=0.5, stratify=resto[OBJETIVO], random_state=SEMILLA)
    carpeta = SALIDA / "particiones"
    carpeta.mkdir(parents=True, exist_ok=True)
    info = {}
    for nombre, parte in [("entrenamiento", entrenamiento), ("validacion", validacion), ("prueba", prueba)]:
        parte[["solicitud_id", "cliente_id"]].sort_values("solicitud_id").to_csv(carpeta / f"{nombre}_ids.csv", index=False)
        info[nombre] = {"filas": int(len(parte)), "proporcion_clase_1": round(float(parte[OBJETIVO].mean()), 4)}
    clientes = [set(p["cliente_id"]) for p in (entrenamiento, validacion, prueba)]
    assert not (clientes[0] & clientes[1] or clientes[0] & clientes[2] or clientes[1] & clientes[2])
    return info


def main() -> None:
    df = pd.read_csv(DATOS)
    nuevas = pd.read_csv(NUEVAS)
    resumen = validar(df)
    if resumen["problemas"]:
        raise SystemExit(f"Datos inválidos: {resumen['problemas']}")
    particiones = particionar(df)
    eda(df, nuevas, resumen)
    manifiesto = {
        "version_datos": VERSION_DATOS,
        "archivo": "data/prestamos_entrenamiento.csv",
        "sha256": sha256(DATOS),
        "semilla": SEMILLA,
        "estrategia_particion": "estratificada por incumplimiento_90d, 60/20/20, por solicitud (1 solicitud por cliente)",
        "particiones": particiones,
        "validacion": resumen,
    }
    SALIDA.mkdir(parents=True, exist_ok=True)
    (SALIDA / "manifiesto_datos.json").write_text(json.dumps(manifiesto, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: manifiesto[k] for k in ("version_datos", "sha256", "particiones")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
