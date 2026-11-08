"""Promoción explícita (y rollback) de la versión activa del modelo.

Uso:
  python ml/promover.py --version v2 --motivo "gana en AP de validación"
  python ml/promover.py --version v1 --motivo "rollback: drift detectado"   # rollback = promover la anterior

La API de Préstamos carga SOLO la versión indicada en models/activo.json. Exportar una nueva
versión no la activa. Cada cambio queda en models/historial_promociones.jsonl.
Después de promover: reiniciar Préstamos o llamar POST /modelo/recargar (rol administrador).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
MODELOS = Path(os.environ.get("MODELOS_DIR", RAIZ / "models"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--version", required=True)
    p.add_argument("--motivo", required=True)
    a = p.parse_args()

    carpeta = MODELOS / a.version
    manifiesto = json.loads((carpeta / "manifiesto.json").read_text(encoding="utf-8"))
    real = hashlib.sha256((carpeta / manifiesto["artefacto"]["archivo"]).read_bytes()).hexdigest()
    if real != manifiesto["artefacto"]["sha256"]:
        raise SystemExit("El hash del artefacto no coincide con el manifiesto: no se promueve.")

    ruta_activo = MODELOS / "activo.json"
    anterior = json.loads(ruta_activo.read_text(encoding="utf-8"))["version"] if ruta_activo.exists() else None
    registro = {
        "version": a.version,
        "run_id": manifiesto["run_id"],
        "sha256_artefacto": real,
        "anterior": anterior,
        "motivo": a.motivo,
        "promovido_por": getpass.getuser(),
        "promovido_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ruta_activo.write_text(json.dumps(registro, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(MODELOS / "historial_promociones.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    print(f"Versión activa: {anterior} -> {a.version}. Reinicie Préstamos o llame POST /modelo/recargar.")


if __name__ == "__main__":
    main()
