import os
import threading
import time

import psutil
from prometheus_client import Counter, Gauge, Histogram, start_http_server  # noqa: F401


def cgroup_working_set():
    """Working set du conteneur (cgroup v2 : memory.current - inactive_file), sinon RSS."""
    try:
        cur = int(open("/sys/fs/cgroup/memory.current").read())
        inactive = 0
        for line in open("/sys/fs/cgroup/memory.stat"):
            if line.startswith("inactive_file "):
                inactive = int(line.split()[1])
        return cur - inactive
    except Exception:
        return psutil.Process().memory_info().rss


def iface_bytes(iface: str):
    try:
        for line in open("/proc/net/dev"):
            if line.strip().startswith(iface + ":"):
                parts = line.split(":")[1].split()
                return int(parts[0]), int(parts[8])  # rx, tx
    except Exception:
        pass
    return 0, 0


class ResourceSampler(threading.Thread):
    """Échantillonne CPU / RSS / working set chaque seconde -> CSV (données brutes)."""

    def __init__(self, csv_path, gauge_cpu=None, interval=1.0):
        super().__init__(daemon=True)
        self.csv = csv_path
        self.g = gauge_cpu
        self.interval = interval
        self.proc = psutil.Process()
        self.cpu = 0.0
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        if not os.path.exists(csv_path):
            with open(csv_path, "w") as f:
                f.write("t,cpu_pct,rss,wss\n")

    def _cpu_total(self):
        t = self.proc.cpu_times()  # inclut les enfants terminés (ffmpeg)
        return t.user + t.system + getattr(t, "children_user", 0) + getattr(t, "children_system", 0)

    def run(self):
        last_c, last_t = self._cpu_total(), time.time()
        while True:
            time.sleep(self.interval)
            c, t = self._cpu_total(), time.time()
            cpu = 100.0 * (c - last_c) / max(t - last_t, 1e-6)
            last_c, last_t = c, t
            self.cpu = 0.5 * self.cpu + 0.5 * cpu
            if self.g:
                self.g.set(self.cpu)
            with open(self.csv, "a") as f:
                f.write(f"{t:.3f},{cpu:.1f},{self.proc.memory_info().rss},{cgroup_working_set()}\n")
