#!/usr/bin/env python3
"""Essai complet SANS Docker ni AWS (poste de développement, Linux ou Windows).

Lance en processus locaux : Cloud (API + worker « Video Production »), téléverseur, les 4 étages Edge
et le générateur de charge. « Coupure WAN » = arrêt brutal de l'API Cloud puis redémarrage.
Produit des dossiers de résultats analysables par analyze.py (sans tc, sans énergie mesurée).

    python scripts/dev_smoke.py --out /tmp/smoke --duration 60 --rate 6 --modes hybrid --conditions nominal blackout
Prérequis : ffmpeg/ffprobe dans le PATH, modèles dans MODELS_DIR (python -m vidp fetch-models),
            vidéos de test (python -m vidp synth).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from vidp.common import node_token  # noqa: E402
from vidp.config import STAGES  # noqa: E402


def wait_http(url, timeout=30):
    import urllib.request
    t = time.time()
    while time.time() - t < timeout:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def run(mode, cond, a, port):
    run_id = f"{mode}-{cond}-r0"
    base = Path(a.out) / "work" / run_id
    shutil.rmtree(base, ignore_errors=True)
    for d in ("spool", "cloud", "inbox"):
        (base / d).mkdir(parents=True)
    res = Path(a.out) / "results"
    rd = res / run_id
    shutil.rmtree(rd, ignore_errors=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CLOUD_MASTER_SECRET": "dev-secret",
           "DATA_DIR": str(base / "cloud"), "STORAGE": "local", "NODE_ID": "edge-01",
           "NODE_TOKEN": node_token("dev-secret", "edge-01"), "CLOUD_URL": f"http://127.0.0.1:{port}",
           "SPOOL_DIR": str(base / "spool"), "RESULTS_DIR": str(res), "RUN_ID": run_id, "MODE": mode,
           "INBOX_DIR": str(base / "inbox"), "LIBRARY_DIR": a.videos, "FEED_RATE": str(a.rate),
           "DURATION_S": str(a.duration), "WAN_IFACE": "lo", "CONNECT_TIMEOUT": "1", "READ_TIMEOUT": "5",
           "MODELS_DIR": a.models, "PYTHONIOENCODING": "utf-8"}
    logs = base / "logs"
    logs.mkdir()
    procs = {}

    def sp(name, args, **extra):
        f = open(logs / f"{name}.log", "w", encoding="utf-8")
        procs[name] = subprocess.Popen([sys.executable, *args], env={**env, **extra}, stdout=f, stderr=subprocess.STDOUT)

    def api():
        sp("api", ["-m", "uvicorn", "--factory", "vidp.cloud_app:all", "--port", str(port), "--log-level", "warning"])
        wait_http(f"http://127.0.0.1:{port}/healthz")

    api()
    sp("worker", ["-m", "vidp", "cloud-worker"], METRICS_PORT=str(port + 300))
    sp("uploader", ["-m", "vidp", "uploader"], METRICS_PORT=str(port + 100))
    for i, s in enumerate(STAGES):
        sp(s, ["-m", "vidp", "stage", s], METRICS_PORT=str(port + 110 + i))
    time.sleep(3)
    sp("feeder", ["-m", "vidp", "feeder"])
    t0, tl = time.time(), []
    if cond == "blackout":
        a1, a2 = a.duration * 0.25, a.duration * 0.6
        time.sleep(a1)
        procs["api"].kill(); procs["api"].wait()
        tl.append({"t": time.time(), "action": "down"})
        print(f"  coupure du Cloud ({a2 - a1:.0f} s)", flush=True)
        time.sleep(a2 - a1)
        api()
        tl.append({"t": time.time(), "action": "up"})
    procs["feeder"].wait()
    sys.path.insert(0, str(ROOT / "src"))
    from vidp.outbox import Outbox
    from vidp.storage import SqliteMeta
    from vidp.jobs import JobQueue
    ob, jq = Outbox(str(base / "spool")), JobQueue(str(base / "spool" / "jobs.db"))
    meta = SqliteMeta(str(base / "cloud" / "videos.db"))
    n_in = sum(1 for _ in open(rd / "feeder.jsonl", encoding="utf-8"))
    deadline = time.time() + a.drain_timeout
    while time.time() < deadline:
        vs = meta.list(run_id, 5000)
        done = sum(1 for v in vs if v.get("state") == "done")
        if done >= n_in and ob.backlog()[0] == 0:
            break
        time.sleep(1)
    time.sleep(2)
    (rd / "cloud_videos.json").write_text(json.dumps(meta.list(run_id, 5000), ensure_ascii=False), encoding="utf-8")
    (rd / "timeline.jsonl").write_text("".join(json.dumps(x) + "\n" for x in tl), encoding="utf-8")
    (rd / "run_meta.json").write_text(json.dumps({"run_id": run_id, "mode": mode, "condition": cond, "rep": 0,
                                                  "duration_s": a.duration, "rate": a.rate, "t_start": t0,
                                                  "t_end": time.time(), "jobs": jq.counts()}), encoding="utf-8")
    for p in procs.values():
        p.kill()
    for p in procs.values():
        p.wait()
    print(f"  {run_id} : {done}/{n_in} vidéos publiées, file Edge = {ob.backlog()[0]}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    tmp = os.environ.get("TEMP", "/tmp")
    ap.add_argument("--out", default=os.path.join(tmp, "vidp_smoke"))
    ap.add_argument("--videos", default=str(ROOT / "data" / "videos"))
    ap.add_argument("--models", default=os.environ.get("MODELS_DIR", str(ROOT / "models")))
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--rate", type=float, default=6, help="vidéos par minute")
    ap.add_argument("--drain-timeout", type=int, default=300)
    ap.add_argument("--modes", nargs="+", default=["cloud_only", "edge_first", "hybrid"])
    ap.add_argument("--conditions", nargs="+", default=["nominal", "blackout"])
    a = ap.parse_args()
    if not list(Path(a.videos).glob("*.mp4")):
        sys.exit(f"aucune vidéo dans {a.videos} : python -m vidp synth --out {a.videos}")
    port = 18700
    for cond in a.conditions:
        for mode in a.modes:
            print("run", mode, cond, flush=True)
            run(mode, cond, a, port)
            port += 1
    print(f"Résultats : {a.out}/results  ->  python scripts/analyze.py --results {a.out}/results "
          f"--videos {a.videos} --out {a.out}/analysis")


if __name__ == "__main__":
    main()
