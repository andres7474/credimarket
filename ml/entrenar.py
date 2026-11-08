"""Etapas B y C: entrenar, comparar y registrar en MLflow; selección y umbral.

Uso:  MLFLOW_TRACKING_URI=http://localhost:5000 python ml/entrenar.py

Reglas fijadas ANTES de comparar (no se cambian después de ver resultados):
  * Métrica de selección: average precision (AP) de la clase 1 en VALIDACIÓN.
  * Desempate: si dos candidatos difieren en menos de 0,005 de AP, gana el de mayor
    ROC-AUC en validación; si persiste, el de menor Brier score.
  * Umbral: se elige sobre VALIDACIÓN minimizando un costo ficticio y explícito:
        aprobar a quien incumplirá (falso negativo) = 3 unidades
        rechazar a quien habría pagado (falso positivo) = 1 unidad
    Luego se congela y solo entonces se evalúa en PRUEBA.
  * La PRUEBA se usa una sola vez, para los modelos ya congelados.

Los costos son ficticios y no son una recomendación financiera.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import mlflow  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sklearn  # noqa: E402
from sklearn.calibration import calibration_curve  # noqa: E402
from sklearn.dummy import DummyClassifier  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    ConfusionMatrixDisplay,
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_score  # noqa: E402
from sklearn.tree import DecisionTreeClassifier  # noqa: E402

from credimarket_ml.features import (  # noqa: E402
    CLASE_POSITIVA,
    DERIVADAS,
    ENTRADAS,
    EXCLUIDAS,
    OBJETIVO,
    VERSION_CARACTERISTICAS,
    construir_pipeline,
)

RAIZ = Path(__file__).resolve().parents[1]
DATOS = RAIZ / "data" / "prestamos_entrenamiento.csv"
MANIFIESTO_DATOS = RAIZ / "ml" / "salidas" / "datos" / "manifiesto_datos.json"
PARTICIONES = RAIZ / "ml" / "salidas" / "datos" / "particiones"
SALIDA = RAIZ / "ml" / "salidas" / "entrenamiento"
REPORTES = RAIZ / "ml" / "reportes"
EXPERIMENTO = "credimarket-riesgo"
SEMILLA = 42
COSTO_FN = 3.0  # aprobar a quien incumplirá
COSTO_FP = 1.0  # rechazar a quien habría pagado
TOLERANCIA_EMPATE = 0.005

CANDIDATOS = [
    # (nombre de la corrida, algoritmo, estimador, escalar, justificación)
    ("baseline-prior", "dummy", DummyClassifier(strategy="prior"), False,
     "Baseline trivial: predice la proporción de la clase 1 para todos."),
    ("logreg-C1", "logistic_regression", LogisticRegression(C=1.0, max_iter=2000), True,
     "Modelo lineal de referencia con regularización estándar."),
    ("logreg-C0.1-balanced", "logistic_regression",
     LogisticRegression(C=0.1, class_weight="balanced", max_iter=2000), True,
     "Más regularización y pesos balanceados por el desbalance 70/30."),
    ("arbol-d4", "decision_tree", DecisionTreeClassifier(max_depth=4, min_samples_leaf=50, random_state=SEMILLA), False,
     "Árbol poco profundo: interpretable, poca varianza."),
    ("arbol-d8", "decision_tree", DecisionTreeClassifier(max_depth=8, min_samples_leaf=20, random_state=SEMILLA), False,
     "Árbol más profundo para capturar interacciones; hojas mínimas contra sobreajuste."),
    ("rf-d8", "random_forest",
     RandomForestClassifier(n_estimators=300, max_depth=8, min_samples_leaf=10, n_jobs=-1, random_state=SEMILLA), False,
     "Bosque con profundidad acotada: reduce la varianza de un árbol."),
    ("rf-d14", "random_forest",
     RandomForestClassifier(n_estimators=300, max_depth=14, min_samples_leaf=5, max_features="sqrt", n_jobs=-1,
                            random_state=SEMILLA), False,
     "Bosque más profundo para comparar sesgo/varianza frente a rf-d8."),
    ("hgb-lr0.05", "hist_gradient_boosting",
     HistGradientBoostingClassifier(learning_rate=0.05, max_iter=300, max_depth=3, l2_regularization=1.0,
                                    random_state=SEMILLA), False,
     "Boosting con árboles poco profundos y tasa de aprendizaje baja."),
]


def hash_codigo() -> str:
    h = hashlib.sha256()
    for ruta in sorted((RAIZ / "ml").rglob("*.py")):
        if "salidas" in ruta.parts:
            continue
        h.update(ruta.read_bytes())
    return h.hexdigest()[:16]


def commit_git() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=RAIZ, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "sin-git"


def cargar_particiones():
    df = pd.read_csv(DATOS).set_index("solicitud_id")
    partes = {}
    for nombre in ("entrenamiento", "validacion", "prueba"):
        ids = pd.read_csv(PARTICIONES / f"{nombre}_ids.csv")["solicitud_id"]
        sub = df.loc[ids]
        partes[nombre] = (sub[ENTRADAS], sub[OBJETIVO].to_numpy())
    return partes


def metricas(y, prob, umbral=0.5) -> dict:
    pred = (prob >= umbral).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "precision_1": precision_score(y, pred, zero_division=0),
        "recall_1": recall_score(y, pred, zero_division=0),
        "f1_1": f1_score(y, pred, zero_division=0),
        "roc_auc": roc_auc_score(y, prob),
        "average_precision": average_precision_score(y, prob),
        "brier": brier_score_loss(y, prob),
        "tasa_rechazo": float(pred.mean()),
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
        "costo_por_solicitud": float((COSTO_FN * fn + COSTO_FP * fp) / len(y)),
    }


def figura_confusion(y, prob, umbral, titulo, ruta):
    fig, ax = plt.subplots(figsize=(4, 3.6))
    ConfusionMatrixDisplay(confusion_matrix(y, (prob >= umbral).astype(int), labels=[0, 1]),
                           display_labels=["pagó (0)", "incumplió (1)"]).plot(ax=ax, colorbar=False)
    ax.set_title(titulo, fontsize=9)
    fig.tight_layout()
    fig.savefig(ruta, dpi=110)
    plt.close(fig)


def figura_calibracion(y, prob, ruta):
    fig, ax = plt.subplots(figsize=(4, 3.6))
    frac, media = calibration_curve(y, prob, n_bins=10, strategy="quantile")
    ax.plot([0, 1], [0, 1], "--", color="gray")
    ax.plot(media, frac, marker="o")
    ax.set_xlabel("probabilidad media predicha")
    ax.set_ylabel("fracción observada de clase 1")
    ax.set_title("Calibración (validación)", fontsize=9)
    fig.tight_layout()
    fig.savefig(ruta, dpi=110)
    plt.close(fig)


def curva_costo(y, prob):
    umbrales = np.round(np.arange(0.05, 0.951, 0.01), 2)
    costos = []
    for u in umbrales:
        pred = (prob >= u).astype(int)
        fp = int(((pred == 1) & (y == 0)).sum())
        fn = int(((pred == 0) & (y == 1)).sum())
        costos.append((COSTO_FN * fn + COSTO_FP * fp) / len(y))
    return umbrales, np.array(costos)


def elegir(resultados):
    candidatos = [r for r in resultados if r["algoritmo"] != "dummy"]
    mejor_ap = max(r["val"]["average_precision"] for r in candidatos)
    empatados = [r for r in candidatos if mejor_ap - r["val"]["average_precision"] < TOLERANCIA_EMPATE]
    empatados.sort(key=lambda r: (-r["val"]["roc_auc"], r["val"]["brier"]))
    return empatados[0], empatados


def main() -> None:
    manifiesto_datos = json.loads(MANIFIESTO_DATOS.read_text(encoding="utf-8"))
    partes = cargar_particiones()
    X_tr, y_tr = partes["entrenamiento"]
    X_va, y_va = partes["validacion"]
    X_te, y_te = partes["prueba"]
    SALIDA.mkdir(parents=True, exist_ok=True)
    REPORTES.mkdir(parents=True, exist_ok=True)

    mlflow.set_experiment(EXPERIMENTO)
    etiquetas_comunes = {
        "version_datos": manifiesto_datos["version_datos"],
        "sha256_datos": manifiesto_datos["sha256"],
        "version_codigo": hash_codigo(),
        "commit_git": commit_git(),
        "version_caracteristicas": VERSION_CARACTERISTICAS,
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
    }
    requisitos = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEMILLA)
    resultados = []

    for nombre, algoritmo, estimador, escalar, porque in CANDIDATOS:
        with mlflow.start_run(run_name=nombre) as run:
            inicio = time.time()
            pipeline = construir_pipeline(estimador, escalar=escalar)
            cv_ap = cross_val_score(pipeline, X_tr, y_tr, cv=cv, scoring="average_precision", n_jobs=1)
            pipeline.fit(X_tr, y_tr)
            prob_tr = pipeline.predict_proba(X_tr)[:, 1]
            prob_va = pipeline.predict_proba(X_va)[:, 1]
            m_tr, m_va = metricas(y_tr, prob_tr), metricas(y_va, prob_va)
            duracion = time.time() - inicio

            mlflow.set_tags({**etiquetas_comunes, "algoritmo": algoritmo, "justificacion": porque,
                             "rol": "baseline" if algoritmo == "dummy" else "candidato"})
            mlflow.log_params({f"modelo__{k}": v for k, v in estimador.get_params().items()
                               if isinstance(v, (int, float, str, bool, type(None)))})
            mlflow.log_params({
                "algoritmo": algoritmo,
                "semilla": SEMILLA,
                "estrategia_particion": manifiesto_datos["estrategia_particion"],
                "caracteristicas_entrada": ",".join(ENTRADAS),
                "caracteristicas_derivadas": ",".join(DERIVADAS),
                "excluidas": ",".join(EXCLUIDAS),
                "prep_numericas": "imputar mediana + indicador de faltante" + (" + escalar" if escalar else ""),
                "prep_categoricas": "imputar 'desconocido' + one-hot (ignora categorías nuevas)",
                "umbral_evaluacion": 0.5,
                "costo_fn": COSTO_FN,
                "costo_fp": COSTO_FP,
            })
            mlflow.log_metrics({f"entrenamiento_{k}": float(v) for k, v in m_tr.items()})
            mlflow.log_metrics({f"validacion_{k}": float(v) for k, v in m_va.items()})
            mlflow.log_metrics({"cv_ap_media": float(cv_ap.mean()), "cv_ap_desv": float(cv_ap.std()),
                                "duracion_segundos": duracion})

            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                joblib.dump(pipeline, tmp / "pipeline.joblib", compress=3)
                figura_confusion(y_va, prob_va, 0.5, f"{nombre} · validación · umbral 0,5", tmp / "matriz_confusion_validacion.png")
                figura_calibracion(y_va, prob_va, tmp / "calibracion_validacion.png")
                (tmp / "requirements.txt").write_text(requisitos)
                informe = {"corrida": nombre, "algoritmo": algoritmo, "justificacion": porque,
                           "entrenamiento": m_tr, "validacion": m_va,
                           "cv_ap": [float(x) for x in cv_ap], "duracion_segundos": duracion}
                (tmp / "reporte_evaluacion.json").write_text(json.dumps(informe, indent=2, ensure_ascii=False))
                mlflow.log_artifacts(str(tmp))

            resultados.append({"nombre": nombre, "algoritmo": algoritmo, "run_id": run.info.run_id,
                               "pipeline": pipeline, "prob_va": prob_va, "val": m_va, "cv_ap": float(cv_ap.mean()),
                               "tr": m_tr, "escalar": escalar})
            print(f"{nombre:22s} val_AP={m_va['average_precision']:.4f} val_AUC={m_va['roc_auc']:.4f} "
                  f"brier={m_va['brier']:.4f} cvAP={cv_ap.mean():.4f}")

    seleccionado, empatados = elegir(resultados)
    # Versión anterior (v1) para demostrar promoción y rollback: el mejor candidato de un
    # algoritmo distinto al seleccionado, con la misma regla de AP en validación.
    alternativo = max((r for r in resultados if r["algoritmo"] not in ("dummy", seleccionado["algoritmo"])),
                      key=lambda r: r["val"]["average_precision"])

    resumen_versiones = {}
    for etiqueta, r in (("seleccionado", seleccionado), ("alternativo", alternativo)):
        umbrales, costos = curva_costo(y_va, r["prob_va"])
        umbral = float(umbrales[int(np.argmin(costos))])
        prob_te = r["pipeline"].predict_proba(X_te)[:, 1]
        m_va_u = metricas(y_va, r["prob_va"], umbral)
        m_te = metricas(y_te, prob_te, umbral)
        with mlflow.start_run(run_id=r["run_id"]):
            mlflow.log_param("umbral_seleccionado", umbral)
            mlflow.log_metrics({f"validacion_umbral_{k}": float(v) for k, v in m_va_u.items()})
            mlflow.log_metrics({f"prueba_{k}": float(v) for k, v in m_te.items()})
            mlflow.set_tag("rol", "seleccionado" if etiqueta == "seleccionado" else "version-anterior")
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                fig, ax = plt.subplots(figsize=(5, 3.4))
                ax.plot(umbrales, costos)
                ax.axvline(umbral, ls="--", color="#d9822b")
                ax.set_xlabel("umbral sobre P(incumplimiento)")
                ax.set_ylabel("costo ficticio por solicitud")
                ax.set_title(f"Curva de costo (validación) · FN={COSTO_FN:g}, FP={COSTO_FP:g}", fontsize=9)
                fig.tight_layout()
                fig.savefig(tmp / "curva_costo_validacion.png", dpi=110)
                fig.savefig(REPORTES / f"curva_costo_{etiqueta}.png", dpi=110)
                plt.close(fig)
                figura_confusion(y_te, prob_te, umbral, f"{r['nombre']} · PRUEBA · umbral {umbral}",
                                 tmp / "matriz_confusion_prueba.png")
                figura_confusion(y_te, prob_te, umbral, f"{r['nombre']} · PRUEBA · umbral {umbral}",
                                 REPORTES / f"matriz_confusion_prueba_{etiqueta}.png")
                mlflow.log_artifacts(str(tmp))
        resumen_versiones[etiqueta] = {"corrida": r["nombre"], "run_id": r["run_id"], "algoritmo": r["algoritmo"],
                                       "umbral": umbral, "validacion": m_va_u, "prueba": m_te,
                                       "validacion_umbral_0_5": r["val"]}

    tabla = pd.DataFrame([{
        "corrida": r["nombre"], "algoritmo": r["algoritmo"], "run_id": r["run_id"],
        "val_AP": r["val"]["average_precision"], "val_ROC_AUC": r["val"]["roc_auc"],
        "val_brier": r["val"]["brier"], "val_precision_1@0.5": r["val"]["precision_1"],
        "val_recall_1@0.5": r["val"]["recall_1"], "val_f1_1@0.5": r["val"]["f1_1"],
        "cv_AP_entrenamiento": r["cv_ap"], "entr_AP": r["tr"]["average_precision"],
    } for r in resultados]).sort_values("val_AP", ascending=False)
    tabla.to_csv(SALIDA / "comparacion_corridas.csv", index=False)
    seleccion = {
        "experimento": EXPERIMENTO,
        "regla": "max AP en validación; desempate (<0,005) por ROC-AUC y luego Brier",
        "empatados": [r["nombre"] for r in empatados],
        "costos": {"falso_negativo": COSTO_FN, "falso_positivo": COSTO_FP},
        "versiones": resumen_versiones,
    }
    (SALIDA / "seleccion.json").write_text(json.dumps(seleccion, indent=2, ensure_ascii=False), encoding="utf-8")
    escribir_informe(tabla, seleccion)
    print(json.dumps({k: {"corrida": v["corrida"], "umbral": v["umbral"],
                          "prueba_AP": round(v["prueba"]["average_precision"], 4)}
                      for k, v in resumen_versiones.items()}, indent=2))


def escribir_informe(tabla: pd.DataFrame, seleccion: dict) -> None:
    s = seleccion["versiones"]["seleccionado"]
    l = seleccion["versiones"]["alternativo"]
    f = lambda x: f"{x:.3f}"  # noqa: E731
    lineas = [
        "# Informe de selección del modelo (etapas B a D)",
        "",
        "## Reglas fijadas antes de comparar",
        "",
        "- Métrica de selección: **average precision (AP) de la clase 1 en validación**. Se prefiere a "
        "accuracy porque la clase 1 es el 30 % y lo que importa es ordenar bien el riesgo.",
        f"- Desempate: diferencia de AP menor a {TOLERANCIA_EMPATE} → mayor ROC-AUC → menor Brier.",
        f"- Umbral: mínimo costo ficticio en validación con FN = {COSTO_FN:g} (aprobar a quien incumplirá) "
        f"y FP = {COSTO_FP:g} (rechazar a quien habría pagado). Costos explícitos y no son una recomendación financiera.",
        "- La partición de prueba se usó una sola vez, con los modelos y umbrales ya congelados.",
        "",
        "## Comparación (validación, umbral 0,5 salvo AP, ROC-AUC y Brier)",
        "",
        tabla.drop(columns=["run_id"]).round(4).to_markdown(index=False),
        "",
        f"Empatados dentro de la tolerancia: {', '.join(seleccion['empatados'])}. El desempate se decidió por "
        "ROC-AUC, tal como se fijó, aunque la diferencia es mínima; el modelo con pesos balanceados está peor "
        "calibrado (Brier más alto) porque desplaza las probabilidades hacia la clase 1. Como la decisión usa un "
        "umbral ajustado en validación sobre ese mismo modelo, el desplazamiento queda compensado en el umbral. "
        "Si el negocio necesitara probabilidades calibradas, la regla debería cambiarse antes de una nueva comparación.",
        "",
        f"## Modelo seleccionado: `{s['corrida']}` (versión v2)",
        "",
        f"- run_id: `{s['run_id']}`",
        f"- Umbral congelado: **{s['umbral']}** sobre P(incumplimiento). Si P ≥ umbral, la decisión de riesgo es "
        "DESFAVORABLE. La decisión de riesgo no es la aprobación del préstamo: luego se aplican reglas de negocio y "
        "se reserva inventario.",
        "",
        "| Métrica | Validación (umbral) | Prueba (umbral) |",
        "| --- | --- | --- |",
    ]
    for k in ("average_precision", "roc_auc", "brier", "precision_1", "recall_1", "f1_1", "tasa_rechazo",
              "costo_por_solicitud"):
        lineas.append(f"| {k} | {f(s['validacion'][k])} | {f(s['prueba'][k])} |")
    lineas += [
        "",
        f"Matriz de confusión en prueba: TN={s['prueba']['tn']}, FP={s['prueba']['fp']}, "
        f"FN={s['prueba']['fn']}, TP={s['prueba']['tp']}.",
        "",
        "![Curva de costo](curva_costo_seleccionado.png) ![Matriz de confusión](matriz_confusion_prueba_seleccionado.png)",
        "",
        f"## Versión anterior para promoción y rollback: `{l['corrida']}` (versión v1)",
        "",
        f"- run_id: `{l['run_id']}`, umbral {l['umbral']}. Prueba: AP {f(l['prueba']['average_precision'])}, "
        f"ROC-AUC {f(l['prueba']['roc_auc'])}, recall {f(l['prueba']['recall_1'])}.",
        "- Es el mejor candidato de un algoritmo distinto al seleccionado. Se exporta como v1 para demostrar la "
        "promoción v1 → v2 y el rollback con dos modelos que producen probabilidades distintas.",
        "",
        "## Calibración",
        "",
        "Se reporta el Brier score y la curva de calibración de cada corrida (artefacto `calibracion_validacion.png` "
        "en MLflow). La salida se interpreta como un puntaje ordenable con un umbral; no se afirma que represente el "
        "riesgo real de personas: los datos son sintéticos.",
        "",
    ]
    (REPORTES / "seleccion_modelo.md").write_text("\n".join(lineas), encoding="utf-8")


if __name__ == "__main__":
    main()
