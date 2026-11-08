# Ciclo MLOps del modelo de riesgo (etapas A–F)

Este documento es el índice del ciclo. Los informes detallados están en `ml/reportes/` y el historial completo
se consulta en MLflow (`http://localhost:5000`, experimento **credimarket-riesgo**).

| Etapa | Qué se hizo | Evidencia |
|---|---|---|
| A. Validar y versionar los datos | Validación de tipos, rangos, faltantes, duplicados y clases; EDA; SHA-256; partición 60/20/20 estratificada con semilla 42 | `ml/preparar_datos.py`, `ml/reportes/eda.md`, `ml/salidas/datos/manifiesto_datos.json`, `ml/salidas/datos/particiones/*_ids.csv` |
| B. Entrenar y comparar | Baseline más 4 algoritmos y 7 corridas candidatas | `ml/entrenar.py`, `ml/salidas/entrenamiento/comparacion_corridas.csv` |
| C. Registrar en MLflow | 8 runs con parámetros, hash de datos y de código, commit, dependencias, duración, métricas y artefactos | `mlops/mlflow/` (backend SQLite y artefactos) |
| D. Seleccionar, exportar y promover | Regla fijada de antemano, umbral por costo, exportación con manifiesto y promoción explícita | `ml/reportes/seleccion_modelo.md`, `ml/exportar.py`, `ml/promover.py`, `models/` |
| E. Servir | Préstamos carga la versión promovida al iniciar; seis endpoints | `services/prestamos/app/modelo.py`, `main.py` |
| F. Monitorear y operar | Predicciones registradas, drift (PSI), segunda versión, promoción y rollback | `GET /modelo/monitoreo`, `scripts/simular_drift.py`, `models/historial_promociones.jsonl` |

## A. Datos

- Dataset `prestamos_entrenamiento-v1`: SHA-256 `c6cdc437…a2446ecd`, 12.000 filas y 15 columnas. Un 30,6 % es
  clase 1 (`incumplimiento_90d = 1`).
- No hay duplicados por solicitud, por cliente ni filas idénticas. Cada cliente aparece una sola vez, así que
  partir por solicitud equivale a partir por cliente.
- Faltantes: `porcentaje_pago_puntual` 20,6 % (falta cuando no hay historial de créditos); `tipo_empleo`,
  `antiguedad_laboral_meses` y `meses_historial_crediticio` menos del 1 % cada una.
- **Variables excluidas y por qué:**
  - `solicitud_id` y `cliente_id`: son identificadores y solo sirven para trazabilidad.
  - `tasa_mensual_simulada`: es constante (0,018), no aporta información.
  - `cuota_estimada_cop`: es redundante; es una función determinista de monto, plazo y tasa, y el pipeline la
    recalcula.
  - `incumplimiento_90d`: es la etiqueta.
- **Pipeline sin fuga:** todo lo que se aprende (imputación con mediana más indicador de faltante, escalado y
  one-hot con `handle_unknown="ignore"`) va dentro del `Pipeline` de scikit-learn y se ajusta solo con
  entrenamiento.
- El transformador `CaracteristicasDerivadas` (`ml/credimarket_ml/features.py`) calcula cuota, carga
  financiera, monto sobre ingreso y el indicador `sin_historial_pago`. Es el mismo código que ejecuta
  Préstamos, instalado como paquete.

## B–C. Experimentos

| Corrida | Algoritmo | Hiperparámetros (justificación) | AP validación |
|---|---|---|---|
| baseline-prior | Dummy (prior) | Referencia trivial | 0,306 |
| logreg-C1 | Regresión logística | Regularización estándar | 0,530 |
| logreg-C0.1-balanced | Regresión logística | Más regularización y pesos balanceados por el desbalance 70/30 | 0,530 |
| arbol-d4 | Árbol de decisión | Árbol poco profundo: interpretable y con poco sobreajuste | 0,476 |
| arbol-d8 | Árbol de decisión | Más profundo para ver el sobreajuste | 0,459 |
| rf-d8 | Random forest | Profundidad limitada | 0,519 |
| rf-d14 | Random forest | Más capacidad (sobreajusta: AP de entrenamiento 0,92) | 0,514 |
| hgb-lr0.05 | Gradient boosting | Árboles poco profundos (profundidad 3), tasa de aprendizaje 0,05, regularización L2 | 0,519 |

Cada run registra el algoritmo, los hiperparámetros, la semilla, la estrategia de partición, el hash de datos,
la lista de características, el hash del código y el commit de git, `pip freeze`, la duración, las métricas
de entrenamiento, validación y CV (con prefijos diferenciados), el pipeline, la matriz de confusión, la curva
de calibración y el reporte. El modelo seleccionado registra además las métricas `prueba_*`.

## D. Selección, umbral y exportación

- **Regla fijada antes de comparar:** máxima AP de la clase 1 en validación; con diferencia menor a 0,005
  desempata ROC-AUC y después Brier. Ganó **`logreg-C0.1-balanced`**: empató en AP con `logreg-C1` y ganó por
  ROC-AUC.
- **Umbral 0,45**, ajustado en validación con costos ficticios explícitos: aprobar a quien incumplirá
  (FN) = 3 y rechazar a quien habría pagado (FP) = 1. El umbral se congeló antes de mirar la prueba.
- **Prueba, usada una sola vez:** AP 0,526 · ROC-AUC 0,692 · recall de la clase 1 0,671 · precision 0,421 ·
  Brier 0,218.
- **Versiones exportadas** (cada una con `manifiesto.json`, `pipeline.joblib`, ejemplos de entrada y salida y
  predicciones de referencia):
  - `v1`: `rf-d8`, umbral 0,25, el mejor de otro algoritmo.
  - `v2`: `logreg-C0.1-balanced`, umbral 0,45.
- **Promoción explícita:** `python ml/promover.py --version v2 --motivo "..."` verifica el SHA-256, escribe
  `models/activo.json` y agrega una línea a `models/historial_promociones.jsonl`. La API carga **solo** lo que
  dice `activo.json`, nunca "el último archivo".

## E. Servicio

Préstamos carga el pipeline al iniciar y comprueba que el hash coincida con el manifiesto y que las variables
sean las esperadas. Expone `POST /prestamos`, `GET /prestamos/{id}`, `POST /riesgo/predict`,
`GET /modelo/info`, `GET /health` y `GET /ready`. `/ready` responde 503 "no disponible" si el modelo no cargó.
La entrada se valida con Pydantic: tipos, categorías, rangos, plazos válidos y "pago puntual vacío si no hay
créditos previos". Los campos extra se rechazan. **Equivalencia (prueba 10):** 25 solicitudes de
`solicitudes_nuevas.csv` dan por API la misma probabilidad que `predicciones_referencia.csv`, con una
diferencia máxima de 4,6·10⁻⁷.

## F. Monitoreo, drift, promoción y rollback

- Cada predicción (de préstamos y de `/riesgo/predict`) se guarda en `predicciones` con su versión y sus
  entradas.
- `GET /modelo/monitoreo` muestra el volumen, la probabilidad media y la tasa desfavorable por versión, y el
  **PSI** de cada variable frente a la distribución de entrenamiento guardada en el manifiesto
  (`referencia_drift`, deciles). El administrador lo ve en la pestaña Modelo del frontend.
- **Drift simulado** (`python scripts/simular_drift.py`, resultados en `docs/evidencias/drift_*.json`):

  | Lote | % desfavorable | Variables con PSI > 0,25 |
  |---|---|---|
  | 200 solicitudes nuevas sin cambios | 51,0 % | ninguna (máximo 0,11 en antigüedad laboral) |
  | Las mismas con choque (ingresos −35 %, obligaciones +60 %, monto +80 %, +1 atraso) | 97,5 % | atrasos (5,4), monto (0,59), ingresos (0,51), obligaciones (0,34) |

  **Acción documentada:** con PSI > 0,25 en variables clave se revisan los datos y se reentrena (etapas A–D)
  con datos recientes. La versión nueva solo entra con una promoción explícita.
- **Rollback (prueba 11):** `python ml/promover.py --version v1 --motivo "rollback"` y después
  `POST /modelo/recargar`, o el botón "Recargar versión promovida" del frontend. La misma entrada pasa de
  0,585 (v2) a 0,290 (v1). Cada préstamo guarda su `model_version` y `model_run_id`.

## Reentrenar desde cero

```bash
docker compose up -d mlflow
docker compose --profile entrenamiento run --rm entrenamiento python ml/preparar_datos.py
docker compose --profile entrenamiento run --rm entrenamiento python ml/entrenar.py
docker compose --profile entrenamiento run --rm entrenamiento python ml/exportar.py --version v3 --rol seleccionado
python ml/promover.py --version v3 --motivo "reentrenamiento"     # luego: Recargar en el frontend
```
