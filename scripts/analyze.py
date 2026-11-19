#!/usr/bin/env python3
"""Analyse des exécutions : percentiles + IC bootstrap, volume WAN, résilience, ressources, énergie, qualité.

Usage : python3 scripts/analyze.py [--results edge/results] [--videos data/videos] [--out results]
Entrées par exécution (dossier <results>/<run_id>/), toutes brutes et jointes au dépôt :
  run_meta.json, feeder.jsonl, videos.jsonl, acks.jsonl, uploader_backlog.csv, res_<étage>.csv,
  metrics_{start,end}_*.txt, cloud_videos.json, timeline.jsonl
Définitions : docs/MESURES.md. Tout est recalculé depuis les données brutes (reproductible).
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import statistics as st
from collections import Counter, defaultdict

import numpy as np

T95 = {1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}
RNG = np.random.default_rng(12345)
MODES = ("cloud_only", "edge_first", "hybrid")
STAGES = ("downscale", "langident", "subtitle", "animal")


# ------------------------------------------------------------------ utilitaires
def jl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


def jf(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def rcsv(path):
    return list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def boot_ci(a, q, n=1000):
    """IC 95 % bootstrap d'un percentile."""
    a = np.asarray(a, float)
    if len(a) < 5:
        return float("nan"), float("nan")
    idx = RNG.integers(0, len(a), (n, len(a)))
    v = np.percentile(a[idx], q, axis=1)
    return float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))


def t_ci(vals):
    v = [x for x in vals if x == x]
    if len(v) < 2:
        return (v[0] if v else float("nan")), float("nan")
    m, s = st.mean(v), st.stdev(v)
    return m, T95.get(len(v) - 1, 1.96) * s / math.sqrt(len(v))


def parse_prom(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r'^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([^\s]+)', line)
        if m:
            try:
                out[(m.group(1), frozenset(re.findall(r'(\w+)="([^"]*)"', m.group(2) or "")))] = float(m.group(3))
            except ValueError:
                pass
    return out


def prom_delta(run_dir, endpoint, name):
    a = parse_prom(os.path.join(run_dir, f"metrics_start_{endpoint}.txt"))
    b = parse_prom(os.path.join(run_dir, f"metrics_end_{endpoint}.txt"))
    d = {k: v - a.get(k, 0.0) for k, v in b.items() if k[0] == name}
    return sum(d.values()) if d else float("nan")


def energy_source(run_dir):
    for (n, lab), _ in parse_prom(os.path.join(run_dir, "metrics_end_energy.txt")).items():
        if n == "host_energy_joules_total":
            return dict(lab).get("source", "?")
    return "n/a"


# ------------------------------------------------------------------ qualité vs vérité terrain
def words(s):
    return re.findall(r"\w+", (s or "").lower())


def wer(ref, hyp):
    r, h = words(ref), words(hyp)
    if not r:
        return float("nan")
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / len(r)


def quality(v, gt, tol=1.0):
    """-> dict : langue correcte, (VP, détections, vérités) animaux, WER, résultat correct."""
    det = v.get("animals") or []
    hits, found = 0, set()
    for d in det:
        ok = False
        for i, g in enumerate(gt.get("animals", [])):
            if d["label"] == g["label"] and d["t0"] - tol <= g["t1"] and d["t1"] + tol >= g["t0"]:
                ok = True
                found.add(i)
        hits += ok
    lang_ok = (v.get("lang") or None) == (gt.get("lang") or None)
    labels_ok = {d["label"] for d in det} == {g["label"] for g in gt.get("animals", [])}
    hyp = " ".join(s["text"] for s in v.get("subtitles") or [])
    return {"lang_ok": lang_ok, "tp": hits, "n_det": len(det), "gt_found": len(found),
            "n_gt": len(gt.get("animals", [])), "wer": wer(gt.get("text"), hyp) if gt.get("text") else float("nan"),
            "correct": lang_ok and labels_ok}


# ------------------------------------------------------------------ une exécution
def analyze_run(run_dir, gts):
    meta = jf(os.path.join(run_dir, "run_meta.json"), {})
    mode, cond = meta.get("mode"), meta.get("condition", "?")
    T = float(meta.get("duration_s") or 0)
    fed = jl(os.path.join(run_dir, "feeder.jsonl"))
    lib_of = {f["name"]: f["library"] for f in fed}
    cloud = jf(os.path.join(run_dir, "cloud_videos.json"), []) or []
    acks = jl(os.path.join(run_dir, "acks.jsonl"))
    r = {"run_id": os.path.basename(run_dir), "mode": mode, "condition": cond, "rep": meta.get("rep", 0),
         "duration_s": T, "videos_in": len(fed)}

    # --- latence : arrivée sur l'Edge (t0) -> résultat publié (visible sur la page)
    done = [v for v in cloud if v.get("state") == "done" and v.get("published_ts")]
    lat = [v["published_ts"] - v["t0"] for v in done]
    r["n_latency"] = len(lat)
    for q in (50, 95, 99):
        r[f"p{q}"] = pct(lat, q)
        r[f"p{q}_lo"], r[f"p{q}_hi"] = boot_ci(lat, q)
    r["_lat"] = lat
    r["offload_pct"] = 100 * sum(v.get("processed_at") == "cloud" for v in done) / len(done) if done else float("nan")

    # --- résilience : pertes, doublons
    names = Counter(v.get("name") for v in cloud)
    r["published"] = len(done)
    r["lost"] = len({f["name"] for f in fed} - {v.get("name") for v in done})
    r["duplicates"] = sum(n - 1 for n in names.values() if n > 1)
    r["dup_acks"] = sum(1 for a in acks if a.get("duplicate"))      # renvois absorbés par l'idempotence

    # --- qualité
    qs = [quality(v, gts[lib_of[v["name"]]]) for v in done if lib_of.get(v.get("name")) in gts]
    if qs:
        r["lang_acc"] = sum(q["lang_ok"] for q in qs) / len(qs)
        nd, ng = sum(q["n_det"] for q in qs), sum(q["n_gt"] for q in qs)
        r["precision"] = sum(q["tp"] for q in qs) / nd if nd else float("nan")
        r["recall"] = sum(q["gt_found"] for q in qs) / ng if ng else float("nan")
        w = [q["wer"] for q in qs if q["wer"] == q["wer"]]
        r["wer"] = float(np.mean(w)) if w else float("nan")
        r["useful"] = sum(q["correct"] for q in qs)
    else:
        r.update(lang_acc=float("nan"), precision=float("nan"), recall=float("nan"), wer=float("nan"),
                 useful=len(done))

    # --- réseau
    r["src_MB"] = sum(f.get("bytes", 0) for f in fed) / 1e6
    r["wan_tx_MB"] = prom_delta(run_dir, "uploader", "wan_if_tx_bytes_total") / 1e6
    r["app_MB"] = prom_delta(run_dir, "uploader", "uploader_app_bytes_sent_total") / 1e6
    if r["wan_tx_MB"] != r["wan_tx_MB"] or r["wan_tx_MB"] <= 0:       # dev sans compteur noyau : octets applicatifs
        r["wan_tx_MB"] = sum(a.get("bytes", 0) for a in acks) / 1e6
    r["avoided_vs_src_pct"] = 100 * (1 - r["wan_tx_MB"] / r["src_MB"]) if r["src_MB"] else float("nan")
    r["retries"] = prom_delta(run_dir, "uploader", "uploader_retries_total")

    # --- backlog / reprise
    bl = rcsv(os.path.join(run_dir, "uploader_backlog.csv"))
    tl = jl(os.path.join(run_dir, "timeline.jsonl"))
    r["backlog_max_items"] = max([int(float(x["items"])) for x in bl], default=0)
    r["backlog_max_MB"] = max([float(x["bytes"]) for x in bl], default=0) / 1e6
    t_down = next((x["t"] for x in tl if x.get("action") == "down"), None)
    t_up = next((x["t"] for x in tl if x.get("action") == "up"), None)
    r["recovery_s"] = float("nan")
    if t_up and bl:
        base = [int(float(x["items"])) for x in bl if float(x["t"]) < (t_down or t_up)]
        thr = (np.median(base) if base else 0) + 2
        after = [(float(x["t"]), int(float(x["items"]))) for x in bl if float(x["t"]) >= t_up]
        for i, (t, n) in enumerate(after):
            if n <= thr and all(m <= thr for _, m in after[i:i + 5]):
                r["recovery_s"] = t - t_up
                break
    r["_backlog"] = [(float(x["t"]), int(float(x["items"]))) for x in bl]
    r["_t_down"], r["_t_up"] = t_down, t_up

    # --- ressources (somme des 4 conteneurs d'étage)
    cpu_tot, wss_tot = 0.0, 0.0
    for s in STAGES:
        rs = rcsv(os.path.join(run_dir, f"res_{s}.csv"))
        c = [float(x["cpu_pct"]) for x in rs]
        w = [float(x["wss"]) for x in rs]
        r[f"cpu_{s}"] = float(np.mean(c)) if c else float("nan")
        cpu_tot += r[f"cpu_{s}"] if c else 0
        wss_tot += max(w) if w else 0
    r["cpu_mean"] = cpu_tot or float("nan")
    r["wss_max_MB"] = wss_tot / 1e6 or float("nan")

    # --- énergie
    e_tot = prom_delta(run_dir, "energy", "host_energy_joules_total")
    idle = jf(os.path.join(os.path.dirname(run_dir), "idle.json"), {}) or {}
    dur = float(meta.get("t_end", 0) - meta.get("t_start", 0)) or T
    r["energy_source"] = energy_source(run_dir)
    r["energy_J"] = e_tot
    r["energy_net_J"] = e_tot - idle.get("idle_power_w", 0) * dur if e_tot == e_tot else float("nan")
    r["J_per_useful"] = r["energy_net_J"] / r["useful"] if r["useful"] and r["energy_net_J"] == r["energy_net_J"] \
        else float("nan")
    return r


# ------------------------------------------------------------------ agrégation + sorties
NUM = ["p50", "p95", "p99", "lost", "duplicates", "published", "offload_pct", "lang_acc", "precision", "recall",
       "wer", "useful", "wan_tx_MB", "src_MB", "avoided_vs_src_pct", "backlog_max_items", "backlog_max_MB",
       "recovery_s", "cpu_mean", "wss_max_MB", "energy_net_J", "J_per_useful"]


def aggregate(runs):
    groups = defaultdict(list)
    for r in runs:
        groups[(r["mode"], r["condition"])].append(r)
    agg = []
    for (mode, cond), rs in sorted(groups.items()):
        row = {"mode": mode, "condition": cond, "reps": len(rs)}
        pooled = [x for r in rs for x in r["_lat"]]
        row["n_videos"] = len(pooled)
        for q in (50, 95, 99):
            row[f"p{q}_pooled"] = pct(pooled, q)
            row[f"p{q}_lo"], row[f"p{q}_hi"] = boot_ci(pooled, q)
        for k in NUM:
            row[k], row[k + "_ci"] = t_ci([r.get(k, float("nan")) for r in rs])
        row["energy_source"] = rs[0]["energy_source"]
        agg.append(row)
    ref = next((a["wan_tx_MB"] for a in agg if a["mode"] == "cloud_only" and a["condition"] == "nominal"), None)
    for a in agg:
        a["avoided_vs_cloud_pct"] = (1 - a["wan_tx_MB"] / ref) * 100 if ref else float("nan")
    return agg


def write_csv(path, rows):
    if not rows:
        return
    keys = [k for k in rows[0] if not k.startswith("_")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k in keys})


def fmt(x, nd=2):
    return "—" if x is None or (isinstance(x, float) and x != x) else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def write_md(path, agg):
    L = ["# Résultats agrégés (généré par analyze.py)\n",
         "## Latence et résilience\n",
         "| Mode | Condition | n | p50 (s) | p95 (s) [IC95] | p99 (s) [IC95] | Perdues | Doublons | Backlog max | Reprise (s) | % déporté |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in agg:
        L.append(f"| {a['mode']} | {a['condition']} | {a['n_videos']} | {fmt(a['p50_pooled'])} | "
                 f"{fmt(a['p95_pooled'])} [{fmt(a['p95_lo'])}–{fmt(a['p95_hi'])}] | "
                 f"{fmt(a['p99_pooled'])} [{fmt(a['p99_lo'])}–{fmt(a['p99_hi'])}] | {fmt(a['lost'], 1)} | "
                 f"{fmt(a['duplicates'], 1)} | {fmt(a['backlog_max_items'], 0)} | {fmt(a['recovery_s'], 1)} | "
                 f"{fmt(a['offload_pct'], 0)} |")
    L += ["\n## Réseau, ressources, énergie, qualité\n",
          "| Mode | Condition | WAN (Mo) | Évité vs source (%) | Évité vs cloud_only (%) | CPU (% cœur) | WSS (Mo) | J nets | J / résultat utile | Langue | Précision | Rappel | WER |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in agg:
        L.append(f"| {a['mode']} | {a['condition']} | {fmt(a['wan_tx_MB'], 1)} ± {fmt(a['wan_tx_MB_ci'], 1)} | "
                 f"{fmt(a['avoided_vs_src_pct'], 1)} | {fmt(a['avoided_vs_cloud_pct'], 1)} | {fmt(a['cpu_mean'], 0)} | "
                 f"{fmt(a['wss_max_MB'], 0)} | {fmt(a['energy_net_J'], 0)} | {fmt(a['J_per_useful'], 1)} ({a['energy_source']}) | "
                 f"{fmt(a['lang_acc'])} | {fmt(a['precision'])} | {fmt(a['recall'])} | {fmt(a['wer'])} |")
    L.append("\nIC : bootstrap 95 % sur les vidéos poolées (percentiles) ; t de Student sur les répétitions (moyennes ±). "
             "Énergie `model` = estimation (pas de RAPL) : indicative seulement.")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


def figures(runs, agg, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    col = {"cloud_only": "#c0392b", "edge_first": "#2f6fdb", "hybrid": "#1f9d55"}
    os.makedirs(out, exist_ok=True)
    conds = sorted({r["condition"] for r in runs})
    for c in conds:
        fig, ax = plt.subplots(figsize=(6, 4))
        for m in MODES:
            v = sorted(x for r in runs if r["mode"] == m and r["condition"] == c for x in r["_lat"])
            if v:
                ax.plot(v, np.arange(1, len(v) + 1) / len(v), label=m, color=col[m])
        ax.set_xscale("log"); ax.set_xlabel("latence arrivée → résultat publié (s)"); ax.set_ylabel("CDF")
        ax.set_title(f"Condition : {c}"); ax.grid(alpha=.3); ax.legend()
        fig.tight_layout(); fig.savefig(os.path.join(out, f"latency_cdf_{c}.png"), dpi=140); plt.close(fig)
    for key, lab, fn in (("p99_pooled", "p99 (s)", "p99_by_condition.png"), ("wan_tx_MB", "Octets WAN émis (Mo)", "wan_bytes.png"),
                         ("J_per_useful", "Énergie nette par résultat utile (J)", "energy_per_result.png")):
        fig, ax = plt.subplots(figsize=(7, 4))
        w = 0.27
        for i, m in enumerate(MODES):
            vals = [next((a[key] for a in agg if a["mode"] == m and a["condition"] == c), float("nan")) for c in conds]
            ax.bar(np.arange(len(conds)) + (i - 1) * w, vals, w, label=m, color=col[m])
        ax.set_xticks(range(len(conds))); ax.set_xticklabels(conds, rotation=20); ax.set_ylabel(lab)
        ax.legend(); ax.grid(axis="y", alpha=.3); fig.tight_layout()
        fig.savefig(os.path.join(out, fn), dpi=140); plt.close(fig)
    bo = [r for r in runs if r["_t_down"] and r["_backlog"]]
    if bo:
        fig, ax = plt.subplots(figsize=(7, 4))
        for r in bo:
            t0 = r["_backlog"][0][0]
            ax.plot([t - t0 for t, _ in r["_backlog"]], [n for _, n in r["_backlog"]], color=col.get(r["mode"]),
                    alpha=.8, label=r["mode"])
            ax.axvspan(r["_t_down"] - t0, r["_t_up"] - t0, color="grey", alpha=.06)
        h, l_ = ax.get_legend_handles_labels()
        ax.legend(dict(zip(l_, h)).values(), dict(zip(l_, h)).keys())
        ax.set_xlabel("temps (s)"); ax.set_ylabel("éléments dans le tampon Edge"); ax.set_title("Backlog pendant la coupure WAN")
        ax.grid(alpha=.3); fig.tight_layout(); fig.savefig(os.path.join(out, "blackout_backlog.png"), dpi=140); plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="edge/results")
    ap.add_argument("--videos", default="data/videos")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    gts = {os.path.basename(p)[:-8] + ".mp4": jf(p) for p in glob.glob(os.path.join(a.videos, "*.gt.json"))}
    dirs = sorted(d for d in glob.glob(os.path.join(a.results, "*"))
                  if os.path.isdir(d) and os.path.exists(os.path.join(d, "run_meta.json")))
    if not dirs:
        raise SystemExit(f"aucune exécution dans {a.results}")
    runs = [analyze_run(d, gts) for d in dirs]
    os.makedirs(a.out, exist_ok=True)
    agg = aggregate(runs)
    write_csv(os.path.join(a.out, "summary_runs.csv"), runs)
    write_csv(os.path.join(a.out, "summary.csv"), agg)
    write_md(os.path.join(a.out, "summary.md"), agg)
    figures(runs, agg, os.path.join(a.out, "figures"))
    print(open(os.path.join(a.out, "summary.md"), encoding="utf-8").read())


if __name__ == "__main__":
    main()
