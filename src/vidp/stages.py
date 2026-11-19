"""Les quatre étages du pipeline VidP (énoncé) — fonctions pures, utilisées par l'Edge ET par le Cloud.

    1. Downscale     : réduction de résolution (ffmpeg, H.264 + audio AAC mono)
    2. LangIdent     : langue parlée (Whisper « base », CTranslate2 int8, détection de voix VAD)
    3. Subtitle      : sous-titres WebVTT (Whisper « tiny », plus sobre ; langue imposée par l'étage 2)
    4. Animal detect : animaux (MobileNet-SSD VOC via OpenCV DNN : oiseau, chat, vache, chien, cheval, mouton)

Puis `make_outputs` produit les « extraits utiles » : vignette annotée + courts extraits autour des animaux.
"""
import json
import os
import subprocess
import threading

import cv2
import numpy as np

from .common import ffmpeg_bin, ffprobe_bin
from .config import ModelCfg

VOC = ["background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car", "cat", "chair", "cow",
       "diningtable", "dog", "horse", "motorbike", "person", "pottedplant", "sheep", "sofa", "train", "tvmonitor"]
ANIMALS = {"bird", "cat", "cow", "dog", "horse", "sheep"}
LABEL_FR = {"bird": "oiseau", "cat": "chat", "cow": "vache", "dog": "chien", "horse": "cheval", "sheep": "mouton"}

# qualité des extraits : (hauteur, crf, débit audio)
EXTRACT_Q = {"full": (360, 28, "48k"), "low": (240, 35, "24k")}
ORDER = ["full", "low", "thumb"]


def _run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} a échoué : {p.stderr.decode(errors='replace')[-400:]}")
    return p.stdout


def probe(path: str) -> dict:
    out = json.loads(_run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format",
                           "-show_streams", path]))
    v = next((s for s in out.get("streams", []) if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in out.get("streams", []))
    return {"duration": float(out.get("format", {}).get("duration") or 0), "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0), "has_audio": has_audio, "bytes": os.path.getsize(path)}


# ---------------------------------------------------------------- 1. Downscale
def downscale(src: str, dst: str, height: int = 360) -> dict:
    tmp = dst + ".part.mp4"
    _run([ffmpeg_bin(), "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
          "-vf", f"scale=-2:'min({height},ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", tmp])
    os.replace(tmp, dst)
    info = probe(dst)
    return {"downscaled_bytes": info["bytes"], "width": info["width"], "height": info["height"],
            "duration": info["duration"], "has_audio": info["has_audio"]}


# ---------------------------------------------------------------- 2-3. Whisper (langue + sous-titres)
_whisper = {}
_wlock = threading.Lock()


def whisper(cfg: ModelCfg, name: str | None = None):
    """Modèle Whisper chargé une fois par processus (chaque étage est un conteneur : un seul modèle chacun)."""
    name = name or cfg.whisper
    with _wlock:
        if name not in _whisper:
            from faster_whisper import WhisperModel
            local = os.path.join(cfg.models_dir, f"whisper-{name}")
            _whisper[name] = WhisperModel(local if os.path.isdir(local) else name, device="cpu",
                                          compute_type="int8", cpu_threads=cfg.whisper_threads,
                                          download_root=cfg.models_dir)
        return _whisper[name]


def load_audio(path: str):
    """Audio 16 kHz mono float32, ou None si la vidéo n'a pas de piste son."""
    try:
        raw = _run([ffmpeg_bin(), "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                    "-f", "s16le", "-"])
    except RuntimeError:
        return None
    if not raw:
        return None
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


VAD = {"min_silence_duration_ms": 500}


def langident(audio, cfg: ModelCfg) -> dict:
    if audio is None or len(audio) < 16000 * 0.5:
        return {"lang": None, "lang_prob": 0.0, "speech_s": 0.0}
    _, info = whisper(cfg, cfg.langid_model).transcribe(audio, beam_size=1, vad_filter=True, vad_parameters=VAD)
    speech = float(getattr(info, "duration_after_vad", info.duration) or 0)
    if speech < 0.5:
        return {"lang": None, "lang_prob": 0.0, "speech_s": round(speech, 2)}
    return {"lang": info.language, "lang_prob": round(float(info.language_probability), 3),
            "speech_s": round(speech, 2)}


def _ts(t):
    h, r = divmod(max(t, 0.0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def subtitle(audio, lang, cfg: ModelCfg) -> dict:
    if audio is None or not lang:
        return {"subtitles": [], "vtt": "WEBVTT\n"}
    segs, _ = whisper(cfg).transcribe(audio, language=lang, beam_size=1, vad_filter=True, vad_parameters=VAD,
                                      condition_on_previous_text=False)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs
           if s.text.strip()]
    vtt = "WEBVTT\n\n" + "\n".join(f"{_ts(s['start'])} --> {_ts(s['end'])}\n{s['text']}\n" for s in out)
    return {"subtitles": out, "vtt": vtt}


# ---------------------------------------------------------------- 4. Animal detect
_ssd = None


def ssd(cfg: ModelCfg):
    global _ssd
    if _ssd is None:
        _ssd = cv2.dnn.readNetFromCaffe(os.path.join(cfg.models_dir, "MobileNetSSD_deploy.prototxt"),
                                        os.path.join(cfg.models_dir, "MobileNetSSD_deploy.caffemodel"))
    return _ssd


def _tiles(w, h):
    """Tuiles carrées couvrant l'image (le réseau attend 300×300 : on évite d'écraser une image 16/9)."""
    s = min(w, h)
    n = max(1, int(np.ceil(max(w, h) / s - 0.15)))
    if w >= h:
        xs = [0] if n == 1 else [round(i * (w - s) / (n - 1)) for i in range(n)]
        return [(x, 0, s) for x in xs]
    ys = [0] if n == 1 else [round(i * (h - s) / (n - 1)) for i in range(n)]
    return [(0, y, s) for y in ys]


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def detect_frame(frame, cfg: ModelCfg):
    h, w = frame.shape[:2]
    net = ssd(cfg)
    cand = []
    for x, y, s in _tiles(w, h):
        tile = frame[y:y + s, x:x + s]
        net.setInput(cv2.dnn.blobFromImage(cv2.resize(tile, (300, 300)), 0.007843, (300, 300), 127.5))
        det = net.forward()
        for i in range(det.shape[2]):
            conf = float(det[0, 0, i, 2])
            k = int(det[0, 0, i, 1])
            label = VOC[k] if k < len(VOC) else "?"
            if conf >= cfg.animal_conf and label in ANIMALS:
                bx = (det[0, 0, i, 3:7] * s).astype(int).tolist()
                cand.append({"label": label, "conf": round(conf, 3),
                             "box": [bx[0] + x, bx[1] + y, bx[2] + x, bx[3] + y]})
    # suppression des doublons TOUTES classes confondues : un même animal ne compte qu'une fois
    out = []
    for c in sorted(cand, key=lambda d: -d["conf"]):
        if all(_iou(c["box"], o["box"]) < 0.4 for o in out):
            out.append(c)
    return out


def animal_detect(path: str, cfg: ModelCfg, gap_s: float = 1.6, min_hits: int = 2) -> dict:
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(fps / cfg.animal_fps))
    n, analysed = 0, 0
    tracks = {}                    # label -> liste d'intervalles [t0, t1, conf_max, nb_détections]
    best = None                    # (conf, t, frame, dets)
    while True:
        if n % step:
            if not cap.grab():
                break
            n += 1
            continue
        ok, frame = cap.read()
        if not ok:
            break
        t = n / fps
        n += 1
        analysed += 1
        dets = detect_frame(frame, cfg)
        for d in dets:
            iv = tracks.setdefault(d["label"], [])
            if iv and t - iv[-1][1] <= gap_s:
                iv[-1][1] = t
                iv[-1][2] = max(iv[-1][2], d["conf"])
                iv[-1][3] += 1
            else:
                iv.append([t, t, d["conf"], 1])
        if dets and (best is None or max(d["conf"] for d in dets) > best[0]):
            best = (max(d["conf"] for d in dets), t, frame.copy(), dets)
    cap.release()
    ivs = [(lab, a, b, c, n) for lab, lst in tracks.items() for a, b, c, n in lst
           if n >= min_hits]                         # une détection isolée n'est pas un animal
    # Hésitation de classe : un intervalle bref contenu dans l'intervalle bien plus long (≥ 3× plus de
    # détections) d'une autre espèce est la même bête vue sous un autre angle -> absorbé.
    keep = [x for x in ivs if not any(y[0] != x[0] and y[1] <= x[1] and x[2] <= y[2] and y[4] >= 3 * x[4]
                                      for y in ivs)]
    animals = [{"label": lab, "label_fr": LABEL_FR[lab], "t0": round(a, 2), "t1": round(b + 1.0 / cfg.animal_fps, 2),
                "conf": round(c, 3), "hits": n} for lab, a, b, c, n in keep]
    animals.sort(key=lambda x: x["t0"])
    return {"animals": animals, "frames_analysed": analysed, "_best": best}


# ---------------------------------------------------------------- extraits utiles
def thumbnail(path: str, out: str, best=None) -> int:
    """Vignette JPEG : meilleure détection (boîtes dessinées) ou trame du milieu."""
    if best is not None:
        _, _, frame, dets = best
        for d in dets:
            x0, y0, x1, y1 = d["box"]
            cv2.rectangle(frame, (x0, y0), (x1, y1), (40, 200, 60), 2)
            cv2.putText(frame, f"{LABEL_FR.get(d['label'], d['label'])} {d['conf']:.2f}", (x0 + 3, max(14, y0 + 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 200, 60), 1, cv2.LINE_AA)
    else:
        cap = cv2.VideoCapture(path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) // 2)))
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return 0
    h, w = frame.shape[:2]
    if w > 480:
        frame = cv2.resize(frame, (480, int(h * 480 / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    with open(out, "wb") as f:
        f.write(buf.tobytes())
    return len(buf)


def merge_windows(animals, duration, pad=0.5, max_clips=3):
    """Fenêtres [t0, t1] à extraire (union des intervalles animaux, élargis de `pad`)."""
    w = sorted([max(0.0, a["t0"] - pad), min(duration or 1e9, a["t1"] + pad)] for a in animals)
    out = []
    for a, b in w:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    out.sort(key=lambda x: x[1] - x[0], reverse=True)
    return sorted(out[:max_clips])


def cut_clip(src: str, t0: float, t1: float, out: str, quality: str) -> int:
    hgt, crf, ab = EXTRACT_Q[quality]
    _run([ffmpeg_bin(), "-loglevel", "error", "-y", "-ss", f"{t0:.2f}", "-i", src, "-t", f"{t1 - t0:.2f}",
          "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale=-2:'min({hgt},ih)'", "-c:v", "libx264",
          "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", ab, "-ac", "1",
          "-movflags", "+faststart", out])
    return os.path.getsize(out)
