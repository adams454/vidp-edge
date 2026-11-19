"""Exécution d'UN étage sur une vidéo, dans un répertoire de travail — commun à l'Edge et au Cloud.

Répertoire de travail d'une vidéo : <work>/<video_id>/
    down.mp4        sortie de Downscale (entrée des étages suivants)
    meta JSON       accumulé dans la file de travaux (data)
    out/            vignette + extraits produits à la fin
"""
import os
import time

from . import stages
from .config import STAGES, ModelCfg

NEXT = {s: (STAGES[i + 1] if i + 1 < len(STAGES) else "done") for i, s in enumerate(STAGES)}


def wdir(work_root, vid):
    d = os.path.join(work_root, vid)
    os.makedirs(os.path.join(d, "out"), exist_ok=True)
    return d


def run_stage(stage: str, job: dict, work_root: str, cfg: ModelCfg):
    """Retourne (données à fusionner, secondes CPU-mur passées)."""
    d = wdir(work_root, job["video_id"])
    down = os.path.join(d, "down.mp4")
    data = job["data"]
    t = time.perf_counter()
    if stage == "downscale":
        res = stages.downscale(job["src"], down, cfg.downscale_height)
        res["src_bytes"] = os.path.getsize(job["src"])
    elif stage == "langident":
        res = stages.langident(stages.load_audio(down) if data.get("has_audio") else None, cfg)
    elif stage == "subtitle":
        r = stages.subtitle(stages.load_audio(down) if data.get("lang") else None, data.get("lang"), cfg)
        with open(os.path.join(d, "out", "subs.vtt"), "w", encoding="utf-8") as f:
            f.write(r["vtt"])
        res = {"subtitles": r["subtitles"]}
    elif stage == "animal":
        r = stages.animal_detect(down, cfg)
        best = r.pop("_best")
        stages.thumbnail(down, os.path.join(d, "out", "thumb.jpg"), best)
        res = r
    else:
        raise ValueError(stage)
    return res, time.perf_counter() - t


def build_outputs(job: dict, work_root: str, quality: str):
    """Extraits utiles d'une vidéo traitée : [(kind, chemin, taille)] + liste des extraits pour les métadonnées."""
    d = wdir(work_root, job["video_id"])
    data = job["data"]
    files = []
    th = os.path.join(d, "out", "thumb.jpg")
    if os.path.exists(th):
        files.append(("thumb", th, os.path.getsize(th)))
    vtt = os.path.join(d, "out", "subs.vtt")
    if data.get("subtitles") and os.path.exists(vtt):
        files.append(("subs", vtt, os.path.getsize(vtt)))
    clips = []
    if quality in stages.EXTRACT_Q and data.get("animals"):
        for i, (a, b) in enumerate(stages.merge_windows(data["animals"], data.get("duration", 0))):
            p = os.path.join(d, "out", f"clip{i}.mp4")
            n = stages.cut_clip(os.path.join(d, "down.mp4"), a, b, p, quality)
            files.append((f"clip{i}", p, n))
            clips.append({"t0": round(a, 2), "t1": round(b, 2), "quality": quality})
    return files, clips


def result_doc(job: dict, node: str, mode: str, where: str, clips, quality) -> dict:
    """Métadonnées finales (ce que la page publique affiche)."""
    data = job["data"]
    return {"video_id": job["video_id"], "run_id": job["run_id"], "node": node, "mode": mode, "name": job["name"],
            "t0": job["t0"], "processed_at": where, "duration": data.get("duration"),
            "width": data.get("width"), "height": data.get("height"), "src_bytes": data.get("src_bytes"),
            "lang": data.get("lang"), "lang_prob": data.get("lang_prob"), "speech_s": data.get("speech_s"),
            "subtitles": data.get("subtitles", []), "animals": data.get("animals", []),
            "clips": clips, "extract_quality": quality, "timings": job.get("timings", {})}
