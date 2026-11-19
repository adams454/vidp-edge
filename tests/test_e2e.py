"""Bout en bout sans Docker : Edge (4 étages + tampon + téléverseur) -> Cloud, avec COUPURE du Cloud.
Vérifie pour chaque mode : chaque vidéo arrivée est publiée exactement une fois, le tampon est vidé,
et la coupure a réellement perturbé l'envoi (sinon le test ne prouverait rien)."""
import argparse
import json
import os
import sys

import pytest
from conftest import MODELS, ROOT, needs_models

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import dev_smoke  # noqa: E402

pytestmark = needs_models


@pytest.mark.parametrize("mode", ["hybrid", "edge_first", "cloud_only"])
def test_blackout_no_loss_no_dup(tmp_path, videos, mode):
    a = argparse.Namespace(out=str(tmp_path), videos=videos, models=MODELS, duration=40, rate=9,
                           drain_timeout=240)
    dev_smoke.run(mode, "blackout", a, 18800 + ["hybrid", "edge_first", "cloud_only"].index(mode))
    rd = tmp_path / "results" / f"{mode}-blackout-r0"
    fed = [json.loads(x) for x in open(rd / "feeder.jsonl", encoding="utf-8")]
    cloud = json.load(open(rd / "cloud_videos.json", encoding="utf-8"))
    acks = [json.loads(x) for x in open(rd / "acks.jsonl", encoding="utf-8")]
    names = sorted(v["name"] for v in cloud)
    assert names == sorted(f["name"] for f in fed), "perte ou doublon"
    assert all(v["state"] == "done" for v in cloud), "résultats non publiés"
    assert max(x["attempts"] for x in acks) > 1, "la coupure n'a rien perturbé : test non probant"
    where = {v["processed_at"] for v in cloud}
    if mode == "cloud_only":
        assert where == {"cloud"}
    if mode == "edge_first":
        assert where == {"edge"}
