import hashlib
import hmac
import json
import os
import shutil
import time


def video_id(node: str, run_id: str, name: str, size: int, mtime: float) -> str:
    """Identifiant stable d'une vidéo arrivée sur l'Edge : un renvoi produit le même id (déduplication)."""
    raw = f"{node}|{run_id}|{name}|{size}|{round(mtime, 3)}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def node_token(master: str, node_id: str) -> str:
    return hmac.new(master.encode(), node_id.encode(), hashlib.sha256).hexdigest()


def check_token(master: str, node_id: str, token: str) -> bool:
    if not master or not token:
        return False
    return hmac.compare_digest(node_token(master, node_id), token)


def ffmpeg_bin() -> str:
    p = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg introuvable (installer ffmpeg ou définir FFMPEG)")
    return p


def ffprobe_bin() -> str:
    p = os.environ.get("FFPROBE") or shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe introuvable")
    return p


def append_jsonl(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    for i in range(20):          # Windows : remplacement refusé si un lecteur tient le fichier
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.01 * (i + 1))
    os.replace(tmp, path)


def now() -> float:
    return time.time()
