"""Générateur de charge : dépose des vidéos dans le magasin local de l'Edge (arrivées de Poisson, graine fixe).

    python -m vidp feeder --library data/videos --inbox data/inbox --rate 4 --duration 300

Chaque dépôt est atomique (copie sous un nom temporaire puis renommage) : l'étage Downscale ne voit
jamais un fichier à moitié écrit. Le journal feeder.jsonl relie chaque dépôt à sa vidéo de bibliothèque
(donc à sa vérité terrain).
"""
import argparse
import glob
import os
import random
import shutil
import time

from .common import append_jsonl


def main(argv=None):
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="vidp feeder")
    ap.add_argument("--library", default=env("LIBRARY_DIR", "/data/videos"))
    ap.add_argument("--inbox", default=env("INBOX_DIR", "/data/inbox"))
    ap.add_argument("--rate", type=float, default=float(env("FEED_RATE", 4)), help="vidéos par minute")
    ap.add_argument("--duration", type=float, default=float(env("DURATION_S", 300)), help="0 = sans fin")
    ap.add_argument("--seed", type=int, default=int(env("FEED_SEED", 1)))
    ap.add_argument("--run-id", default=env("RUN_ID", "dev"))
    ap.add_argument("--results", default=env("RESULTS_DIR", "/results"))
    a = ap.parse_args(argv)
    lib = sorted(glob.glob(os.path.join(a.library, "*.mp4")))
    if not lib:
        raise SystemExit(f"bibliothèque vide : {a.library} (python -m vidp synth)")
    os.makedirs(a.inbox, exist_ok=True)
    rng = random.Random(a.seed)
    log = os.path.join(a.results, a.run_id, "feeder.jsonl")
    t_start, seq = time.time(), 0
    next_t = t_start + 2.0
    while not a.duration or next_t < t_start + a.duration:
        time.sleep(max(0.0, next_t - time.time()))
        src = lib[seq % len(lib)] if seq < len(lib) else rng.choice(lib)
        name = f"{a.run_id}_{seq:04d}_{os.path.basename(src)}"
        tmp = os.path.join(a.inbox, "." + name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, os.path.join(a.inbox, name))
        append_jsonl(log, {"t": time.time(), "seq": seq, "name": name, "library": os.path.basename(src),
                           "bytes": os.path.getsize(src)})
        seq += 1
        next_t += rng.expovariate(a.rate / 60.0)
    with open(os.path.join(a.results, a.run_id, "feeder.done"), "w") as f:
        f.write(str(time.time()))
    print(f"feeder terminé : {seq} vidéos déposées", flush=True)
    if env("FEEDER_STAY", "0") == "1":     # conteneur : rester vivant (pas de redémarrage en boucle)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
