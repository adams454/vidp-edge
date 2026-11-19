import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
MODELS = os.environ.get("MODELS_DIR", os.path.join(ROOT, "models"))
ANIMALS = os.environ.get("ANIMALS_DIR", os.path.join(ROOT, "data", "animals"))


def have_models():
    return os.path.exists(os.path.join(MODELS, "whisper-tiny", "model.bin")) and \
        os.path.exists(os.path.join(MODELS, "whisper-base", "model.bin")) and \
        os.path.exists(os.path.join(MODELS, "MobileNetSSD_deploy.caffemodel"))


needs_models = pytest.mark.skipif(not have_models() or not shutil.which("ffmpeg"),
                                  reason="modèles (python -m vidp fetch-models) ou ffmpeg absents")


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    """4 vidéos de test (avec voix et animaux) générées une fois par session."""
    out = tmp_path_factory.mktemp("videos")
    subprocess.check_call([sys.executable, "-m", "vidp", "synth", "--out", str(out), "--animals", ANIMALS,
                           "--count", "5", "--duration", "14"],
                          env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")})
    return str(out)
