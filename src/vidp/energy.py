"""Exportateur d'énergie : RAPL si disponible, sinon modèle linéaire CPU (étiqueté).

host_energy_joules_total{source="rapl"|"model"}   énergie cumulée de l'hôte (paquet CPU)
host_power_watts{source=...}                      puissance instantanée
Le modèle : P = P_idle + (P_max - P_idle) * charge_cpu  (valeurs configurables, à calibrer
avec un wattmètre si possible). RAPL est préféré : c'est une mesure matérielle.
"""
import glob
import os
import time

import psutil
from prometheus_client import Counter, Gauge, start_http_server

E = Counter("host_energy_joules_total", "Énergie cumulée de l'hôte", ["source"])
P = Gauge("host_power_watts", "Puissance instantanée estimée/mesurée", ["source"])


def rapl_paths():
    base = os.environ.get("RAPL_DIR", "/sys/class/powercap")
    return sorted(glob.glob(os.path.join(base, "intel-rapl:[0-9]", "energy_uj")))


def main():
    port = int(os.environ.get("METRICS_PORT", 9102))
    p_idle = float(os.environ.get("P_IDLE_W", 6.0))
    p_max = float(os.environ.get("P_MAX_W", 28.0))
    start_http_server(port)
    paths = rapl_paths()
    last = {p: int(open(p).read()) for p in paths}
    maxr = {}
    for p in paths:
        try:
            maxr[p] = int(open(os.path.join(os.path.dirname(p), "max_energy_range_uj")).read())
        except Exception:
            maxr[p] = 2 ** 32
    t_last = time.time()
    psutil.cpu_percent(None)
    src = "rapl" if paths else "model"
    print(f"energy-exporter: source={src} ({len(paths)} domaine(s) RAPL)", flush=True)
    while True:
        time.sleep(1.0)
        now = time.time()
        dt = now - t_last
        t_last = now
        if paths:
            dj = 0.0
            for p in paths:
                cur = int(open(p).read())
                d = cur - last[p]
                if d < 0:
                    d += maxr[p]
                dj += d / 1e6
                last[p] = cur
            E.labels("rapl").inc(dj)
            P.labels("rapl").set(dj / dt)
        else:
            load = psutil.cpu_percent(None) / 100.0
            w = p_idle + (p_max - p_idle) * load
            E.labels("model").inc(w * dt)
            P.labels("model").set(w)


if __name__ == "__main__":
    main()
