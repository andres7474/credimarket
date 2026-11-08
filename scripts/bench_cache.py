"""Medición de la caché Redis del catálogo (informe docs/informe_redis.md).

Escenarios (cliente HTTP concurrente contra Productos):
  A. Catálogo caliente   GET /productos/{id} con la caché ya poblada (mayoría HIT).
  B. Fuente de verdad    GET /internos/productos/{id}/cotizacion (siempre lee PostgreSQL; misma fila).
  C. Caché fría + TTL    se invalida antes de cada ronda (mezcla MISS/HIT, como tras cambios de precio).
Se reportan p50/p95/p99 vistos por el cliente y, del lado del servidor, la tasa de aciertos y
p50/p95 por tipo (hit / miss / sin_cache) que publica GET /cache/metricas.

  D. (--con-caida) Redis detenido: todas las lecturas van a PostgreSQL (OMITIDA).

Uso: python scripts/bench_cache.py [--peticiones 3000] [--concurrencia 16] [--con-caida]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent / "pruebas"))
RAIZ = Path(__file__).resolve().parents[1]


def percentiles(valores):
    v = sorted(valores)
    q = lambda p: v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]
    return {"n": len(v), "p50_ms": round(q(50), 2), "p95_ms": round(q(95), 2), "p99_ms": round(q(99), 2),
            "media_ms": round(statistics.mean(v), 2)}


def correr(sesion_factory, urls, concurrencia, headers=None):
    tiempos, estados = [], {}

    def trabajo(lote):
        s = sesion_factory()
        locales = []
        for u in lote:
            t = time.perf_counter()
            r = s.get(u, headers=headers, timeout=5)
            locales.append(((time.perf_counter() - t) * 1000, r.status_code, r.headers.get("X-Cache", "-")))
        return locales

    lotes = [urls[i::concurrencia] for i in range(concurrencia)]
    inicio = time.perf_counter()
    with cf.ThreadPoolExecutor(concurrencia) as ex:
        for res in ex.map(trabajo, lotes):
            for ms, codigo, cache in res:
                tiempos.append(ms)
                estados[f"{codigo}/{cache}"] = estados.get(f"{codigo}/{cache}", 0) + 1
    duracion = time.perf_counter() - inicio
    return {**percentiles(tiempos), "rps": round(len(urls) / duracion, 1), "respuestas": estados}


def main():
    import aceptacion as a  # reutiliza la lectura de .env y el login
    ap = argparse.ArgumentParser()
    ap.add_argument("--peticiones", type=int, default=3000)
    ap.add_argument("--concurrencia", type=int, default=16)
    ap.add_argument("--con-caida", action="store_true", help="incluye el escenario D deteniendo Redis")
    args = ap.parse_args()
    base = a.URL["productos"]
    ids = [p["id"] for p in requests.get(f"{base}/productos", timeout=5).json()]
    random.seed(7)
    secuencia = [random.choice(ids) for _ in range(args.peticiones)]
    sesion = requests.Session
    reporte = {"fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "peticiones": args.peticiones,
               "concurrencia": args.concurrencia, "productos": len(ids)}

    # A. caliente
    for i in ids:
        requests.get(f"{base}/productos/{i}", timeout=5)
    requests.post(f"{base}/cache/metricas/reiniciar", headers=a.ADMIN, timeout=5)
    reporte["A_cache_caliente"] = correr(sesion, [f"{base}/productos/{i}" for i in secuencia], args.concurrencia)
    reporte["A_servidor"] = requests.get(f"{base}/cache/metricas", headers=a.ADMIN, timeout=5).json()

    # B. siempre base de datos (token de administrador; mismo tamaño de respuesta aproximado)
    reporte["B_fuente_de_verdad"] = correr(sesion, [f"{base}/internos/productos/{i}/cotizacion" for i in secuencia],
                                           args.concurrencia, headers=a.ADMIN)

    # C. fría: invalidación cada 100 peticiones (simula actualizaciones frecuentes de catálogo)
    requests.post(f"{base}/cache/metricas/reiniciar", headers=a.ADMIN, timeout=5)
    tiempos = []
    for inicio in range(0, args.peticiones, 100):
        for i in ids:
            requests.delete(f"{base}/cache/productos/{i}", headers=a.ADMIN, timeout=5)
        r = correr(sesion, [f"{base}/productos/{i}" for i in secuencia[inicio:inicio + 100]], args.concurrencia)
        tiempos.append(r)
    reporte["C_servidor"] = requests.get(f"{base}/cache/metricas", headers=a.ADMIN, timeout=5).json()

    # D. Redis caído (opcional): las lecturas siguen respondiendo desde PostgreSQL con timeout acotado
    if args.con_caida:
        a.sh(a.DETENER, "redis")
        try:
            requests.post(f"{base}/cache/metricas/reiniciar", headers=a.ADMIN, timeout=5)
            reporte["D_redis_caido"] = correr(sesion, [f"{base}/productos/{i}" for i in secuencia[:1000]],
                                              args.concurrencia)
            reporte["D_servidor"] = requests.get(f"{base}/cache/metricas", headers=a.ADMIN, timeout=5).json()
        finally:
            a.sh(a.INICIAR, "redis")
            time.sleep(6)

    # Recursos de Redis
    try:
        salida = subprocess.run(["docker", "compose", "exec", "-T", "redis", "redis-cli", "INFO", "memory"],
                                cwd=RAIZ, capture_output=True, text=True, timeout=10).stdout
        if not salida:
            salida = subprocess.run(["redis-cli", "INFO", "memory"], capture_output=True, text=True, timeout=5).stdout
        reporte["redis_memoria"] = {k: v for k, v in (l.split(":", 1) for l in salida.splitlines() if ":" in l)
                                    if k in ("used_memory_human", "used_memory_peak_human", "maxmemory_policy")}
    except Exception as e:  # noqa: BLE001
        reporte["redis_memoria"] = f"no disponible: {e}"

    destino = RAIZ / "docs" / "evidencias" / "bench_cache.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(reporte, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(reporte, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {destino.relative_to(RAIZ)}")


if __name__ == "__main__":
    main()
