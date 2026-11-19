#!/usr/bin/env python3
"""Orchestre les scénarios obligatoires sur le nœud Edge (Docker) et collecte les données brutes.

Modes      : cloud_only (baseline) | edge_first | hybrid (politique adaptative)
Conditions : nominal | bw10 | bw5 | lat100_loss1 | blackout120 (coupure WAN de 2 minutes à t = 60 s)
Charge     : le générateur dépose FEED_RATE vidéos/min (Poisson, graine fixe) pendant --duration s.
Exemples   : python3 scripts/run_scenarios.py --preset quick
             python3 scripts/run_scenarios.py --modes hybrid --conditions blackout120 --reps 1 --duration 300
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDGE = ROOT / "edge"
WANCTL = str(ROOT / "scripts" / "wanctl.sh")
OUTAGE_AT, OUTAGE_LEN = 60, 120
PIPE = ["downscale", "langident", "subtitle", "animal", "uploader"]
PORTS = {"downscale": 9111, "langident": 9112, "subtitle": 9113, "animal": 9114, "uploader": 9101, "energy": 9102}

CONDITIONS = {  # [(décalage s après le début, action wanctl)]
    "nominal": [],
    "bw10": [(0, ["set", "--rate", "10"])],
    "bw5": [(0, ["set", "--rate", "5"])],
    "lat100_loss1": [(0, ["set", "--delay", "100", "--loss", "1"])],
    "blackout120": [(OUTAGE_AT, ["down"]), (OUTAGE_AT + OUTAGE_LEN, ["up"])],
}
PRESETS = {
    "demo": dict(modes=["hybrid"], conditions=["nominal", "blackout120"], reps=1, duration=240),
    "quick": dict(modes=["cloud_only", "edge_first", "hybrid"], conditions=list(CONDITIONS), reps=1, duration=300),
    "full": dict(modes=["cloud_only", "edge_first", "hybrid"], conditions=list(CONDITIONS), reps=3, duration=300),
}


def sh(*a, **k):
    return subprocess.run(list(map(str, a)), check=k.pop("check", True), **k)


def compose(*a):
    return sh("docker", "compose", "-f", EDGE / "docker-compose.yml", "--project-directory", EDGE, *a)


def scrape(port):
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3).read().decode()
    except Exception:
        return ""


def gauge(text, name):
    return sum(float(m.group(1)) for m in re.finditer(rf"^{name}(?:\{{[^}}]*\}})?\s+([0-9.e+-]+)", text, re.M))


def wan(*args):
    sh(WANCTL, *args, stdout=subprocess.DEVNULL)


def clean_state():
    """Vide le tampon et le magasin local entre deux exécutions (fichiers appartenant à l'uid 10001)."""
    for d in (EDGE / "spool", ROOT / "data" / "inbox"):
        sh("docker", "run", "--rm", "-v", f"{d}:/s", "alpine:3.20", "sh", "-c", "rm -rf /s/* /s/.[!.]* 2>/dev/null; true")


def snapshot(rd, tag):
    for name, port in PORTS.items():
        (rd / f"metrics_{tag}_{name}.txt").write_text(scrape(port))


def measure_idle(seconds):
    compose("stop", *PIPE)
    a = gauge(scrape(9102), "host_energy_joules_total")
    t = time.time()
    time.sleep(seconds)
    w = (gauge(scrape(9102), "host_energy_joules_total") - a) / (time.time() - t)
    (EDGE / "results").mkdir(exist_ok=True)
    (EDGE / "results" / "idle.json").write_text(json.dumps({"idle_power_w": w, "seconds": seconds}))
    print(f"puissance au repos : {w:.2f} W")


def cloud_videos(cloud_public, run_id):
    return json.loads(urllib.request.urlopen(f"{cloud_public}/api/videos?run_id={run_id}&limit=2000",
                                             timeout=10).read())


def one_run(mode, cond, rep, duration, rate, cloud_public, drain_timeout):
    run_id = f"{mode}-{cond}-r{rep}"
    rd = EDGE / "results" / run_id
    print(f"\n=== {run_id} ({duration}s, {rate} vidéos/min)")
    compose("--profile", "load", "stop", "feeder", *PIPE)
    wan("reset")
    clean_state()
    (EDGE / ".env.run").write_text(f"MODE={mode}\nRUN_ID={run_id}\nDURATION_S={duration}\nFEED_RATE={rate}\n")
    rd.mkdir(parents=True, exist_ok=True)
    compose("up", "-d", "--force-recreate", *PIPE)
    time.sleep(8)                               # chargement des modèles
    snapshot(rd, "start")
    compose("--profile", "load", "up", "-d", "--force-recreate", "feeder")
    t0 = time.time()
    timeline = []
    for off, act in CONDITIONS[cond]:
        time.sleep(max(0, t0 + off - time.time()))
        wan(*act)
        timeline.append({"t": time.time(), "offset": off, "action": act[0], "args": act[1:]})
        print(f"  t+{off}s : wanctl {' '.join(act)}")
    while not (rd / "feeder.done").exists():
        time.sleep(2)
    t_fed = time.time()
    n_in = sum(1 for _ in open(rd / "feeder.jsonl"))
    # On laisse la condition réseau en place pendant le drainage (c'est elle qu'on mesure),
    # avec un plafond de temps ; la coupure, elle, est déjà terminée.
    deadline = time.time() + drain_timeout
    while time.time() < deadline:
        try:
            done = sum(1 for v in cloud_videos(cloud_public, run_id) if v.get("state") == "done")
        except Exception:
            done = -1
        if done >= n_in and gauge(scrape(9101), "uploader_queue_items") == 0:
            break
        time.sleep(3)
    wan("reset")
    time.sleep(3)
    snapshot(rd, "end")
    (rd / "timeline.jsonl").write_text("".join(json.dumps(x) + "\n" for x in timeline))
    try:
        (rd / "cloud_videos.json").write_text(json.dumps(cloud_videos(cloud_public, run_id)))
    except Exception as e:
        print("  !! impossible de lire les résultats du Cloud :", e)
    (rd / "run_meta.json").write_text(json.dumps({
        "run_id": run_id, "mode": mode, "condition": cond, "rep": rep, "duration_s": duration, "rate": rate,
        "t_start": t0, "t_fed": t_fed, "t_end": time.time(), "videos_in": n_in}))
    print(f"  {n_in} vidéos déposées, drainage {time.time() - t_fed:.0f} s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=PRESETS)
    ap.add_argument("--modes", nargs="+")
    ap.add_argument("--conditions", nargs="+", choices=CONDITIONS)
    ap.add_argument("--reps", type=int)
    ap.add_argument("--duration", type=int)
    ap.add_argument("--rate", type=float, default=4, help="vidéos par minute")
    ap.add_argument("--idle", type=int, default=60, help="secondes de mesure de la puissance au repos (0 = ignorer)")
    ap.add_argument("--drain-timeout", type=int, default=600)
    ap.add_argument("--cloud-public", default=os.environ.get("CLOUD_PUBLIC_URL", ""),
                    help="ex. http://IP_CLOUD (par défaut : lu dans edge/.env)")
    a = ap.parse_args()
    cfg = dict(PRESETS[a.preset or "quick"])
    for k in ("modes", "conditions", "reps", "duration"):
        if getattr(a, k):
            cfg[k] = getattr(a, k)
    if not a.cloud_public:
        m = re.search(r"^CLOUD_PUBLIC_URL=(\S+)", (EDGE / ".env").read_text(), re.M)
        if not m:
            sys.exit("CLOUD_PUBLIC_URL introuvable (edge/.env ou --cloud-public)")
        a.cloud_public = m.group(1)
    if "blackout120" in cfg["conditions"] and cfg["duration"] < OUTAGE_AT + OUTAGE_LEN + 60:
        sys.exit("durée trop courte pour la coupure de 2 min (>= 240 s)")
    if a.idle:
        measure_idle(a.idle)
    for rep in range(cfg["reps"]):
        for cond in cfg["conditions"]:
            for mode in cfg["modes"]:
                one_run(mode, cond, rep, cfg["duration"], a.rate, a.cloud_public, a.drain_timeout)
    wan("reset")
    compose("up", "-d", *PIPE)
    print("\nTerminé. Lancer : python3 scripts/analyze.py")


if __name__ == "__main__":
    main()
