"""Les 4 étages réels (ffmpeg, Whisper tiny, MobileNet-SSD) sur des vidéos de test à vérité terrain."""
import glob
import json
import os

from conftest import MODELS, needs_models

from vidp import pipeline
from vidp.config import ModelCfg

pytestmark = needs_models


def run_all(path, tmp_path):
    cfg = ModelCfg(models_dir=MODELS)
    job = {"video_id": "t", "src": path, "data": {}, "timings": {}, "run_id": "r", "name": "x", "t0": 0}
    for st in ("downscale", "langident", "subtitle", "animal"):
        res, dt = pipeline.run_stage(st, job, str(tmp_path), cfg)
        job["data"].update(res)
        job["timings"][st] = dt
    return job


def test_pipeline_matches_ground_truth(videos, tmp_path):
    ok_lang = ok_animal = n_lang = n_animal = 0
    for p in sorted(glob.glob(os.path.join(videos, "*.mp4"))):
        gt = json.load(open(p[:-4] + ".gt.json", encoding="utf-8"))
        job = run_all(p, tmp_path / os.path.basename(p))
        d = job["data"]
        assert d["height"] == 360 and d["downscaled_bytes"] < d["src_bytes"]       # Downscale réduit vraiment
        if gt["lang"]:
            n_lang += 1
            ok_lang += d["lang"] == gt["lang"]
            assert d["subtitles"], "voix présente mais aucun sous-titre"
        else:
            assert d["lang"] is None
        labels = {a["label"] for a in d["animals"]}
        for g in gt["animals"]:
            n_animal += 1
            ok_animal += g["label"] in labels
        if not gt["animals"]:
            assert not labels, f"faux positif sur {p} : {labels}"
        files, clips = pipeline.build_outputs(job, str(tmp_path / os.path.basename(p)), "full")
        kinds = {k for k, _, _ in files}
        assert "thumb" in kinds and (bool(clips) == bool(d["animals"]))
    assert n_lang and ok_lang >= n_lang - 1, f"langue : {ok_lang}/{n_lang}"
    assert n_animal and ok_animal >= n_animal - 1, f"animaux : {ok_animal}/{n_animal}"
