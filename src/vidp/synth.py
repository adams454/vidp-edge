"""Vidéos de test reproductibles AVEC vérité terrain (langue parlée, texte, animaux et instants).

    python -m vidp synth --out data/videos --animals data/animals --count 8 --duration 20

Chaque vidéo 1280×720 @25 i/s (qualité « caméra », pour que Downscale et le volume WAN aient un sens) :
  - fond animé (dégradé + bruit capteur) ;
  - une photo d'animal (chien, chevaux, aigle — images darknet) qui traverse l'écran entre t0 et t1,
    ou aucune (vidéos négatives, pour mesurer la précision) ;
  - une phrase parlée (synthèse vocale espeak-ng) en fr / en / es / de, ou aucune voix.
Sortie : <nom>.mp4 + <nom>.gt.json  {duration, lang, text, animals:[{label, t0, t1}]}
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import wave

import cv2
import numpy as np

from .common import ffmpeg_bin

PHOTOS = {"dog.jpg": "dog", "horses.jpg": "horse", "eagle.jpg": "bird"}
# Recadrage sur l'animal (x0, y0, x1, y1) : les photos darknet sont des scènes (vélo, voiture…) où l'animal
# serait trop petit une fois réduit ; la vérité terrain doit être sans ambiguïté (un seul animal visible).
CROPS = {"dog.jpg": (110, 190, 350, 560), "horses.jpg": (0, 165, 320, 420), "eagle.jpg": (110, 60, 620, 470)}
TEXT = {
    "fr": "Bonjour, ceci est une vidéo de test du projet VidP. Le traitement se fait au plus près de la caméra.",
    "en": "Hello, this is a test video for the VidP project. Processing happens close to the camera.",
    "es": "Hola, este es un video de prueba del proyecto VidP. El procesamiento se realiza cerca de la cámara.",
    "de": "Hallo, dies ist ein Testvideo des Projekts VidP. Die Verarbeitung erfolgt nahe an der Kamera.",
}
SAPI_CULTURE = {"fr": "fr-FR", "en": "en-US", "es": "es-ES", "de": "de-DE"}


def tts(lang, text, out_wav) -> bool:
    """Synthèse vocale : espeak-ng (Linux/Docker) ; secours Windows : SAPI si la voix est installée."""
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe:
        r = subprocess.run([exe, "-v", lang, "-s", "150", "-w", out_wav, text], capture_output=True)
        return r.returncode == 0 and os.path.exists(out_wav)
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              f"$v=$s.GetInstalledVoices()|?{{$_.VoiceInfo.Culture.Name -eq '{SAPI_CULTURE[lang]}'}}|select -First 1;"
              "if(-not $v){exit 3}; $s.SelectVoice($v.VoiceInfo.Name);"
              f"$s.SetOutputToWaveFile('{out_wav}'); $s.Speak($env:VIDP_TTS_TEXT); $s.Dispose()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           env={**os.environ, "VIDP_TTS_TEXT": text})
        return r.returncode == 0 and os.path.exists(out_wav)
    return False


def read_wav_mono16k(path):
    """WAV -> float32 16 kHz mono (via ffmpeg, quel que soit le format produit par la synthèse)."""
    raw = subprocess.run([ffmpeg_bin(), "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "s16le",
                          "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def make_video(i, out_dir, photos, rng, dur=20.0, w=1280, h=720, fps=25,
               langs=("fr", "en", "es", "de", None, "fr", "en", None)):
    name = f"vid{i:02d}"
    tmp = tempfile.mkdtemp(prefix="vidp_synth_")
    try:
        # --------------------------------------------------- audio (voix éventuelle, sinon bruit de fond faible)
        lang = langs[i % len(langs)]
        audio = (np.random.default_rng(i).normal(0, 0.003, int(dur * 16000))).astype(np.float32)
        text, t_speech = None, None
        if lang and tts(lang, TEXT[lang], os.path.join(tmp, "v.wav")):
            v = read_wav_mono16k(os.path.join(tmp, "v.wav"))[: int((dur - 1) * 16000)]
            t_speech = rng.uniform(0.5, max(0.6, dur - len(v) / 16000 - 0.5))
            k = int(t_speech * 16000)
            audio[k:k + len(v)] += 0.8 * v[: len(audio) - k]
            text = TEXT[lang]
        elif lang:
            print(f"  ! pas de synthèse vocale disponible : {name} sans voix", file=sys.stderr)
            lang = None
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        with wave.open(os.path.join(tmp, "a.wav"), "wb") as wf:
            wf.setnchannels(1), wf.setsampwidth(2), wf.setframerate(16000), wf.writeframes(pcm.tobytes())

        # --------------------------------------------------- scénario animal (1 vidéo sur 4 sans animal)
        animals = []
        photo = None
        if i % 4 != 3 and photos:
            fname = sorted(photos)[i % len(photos)]
            x0c, y0c, x1c, y1c = CROPS[fname]
            img = photos[fname][y0c:y1c, x0c:x1c]
            ph = int(h * rng.uniform(0.5, 0.65))            # l'animal occupe 50 à 65 % de la hauteur
            photo = cv2.resize(img, (int(img.shape[1] * ph / img.shape[0]), ph), interpolation=cv2.INTER_AREA)
            a0 = round(rng.uniform(2.0, dur * 0.35), 2)
            a1 = round(min(dur - 1.0, a0 + rng.uniform(5.0, 9.0)), 2)
            animals.append({"label": PHOTOS[fname], "t0": a0, "t1": a1})
            y0 = rng.randint(0, max(0, h - photo.shape[0]))

        # --------------------------------------------------- vidéo
        cmd = [ffmpeg_bin(), "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
               "-r", str(fps), "-i", "-", "-i", os.path.join(tmp, "a.wav"), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest",
               "-movflags", "+faststart", os.path.join(out_dir, name + ".mp4")]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        nrng = np.random.default_rng(1000 + i)
        base = np.zeros((h, w, 3), np.uint8)
        gx = np.linspace(0, 1, w, dtype=np.float32)[None, :]
        gy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        hue = rng.uniform(0, 1)
        for n in range(int(dur * fps)):
            t = n / fps
            s = 0.5 + 0.5 * np.sin(2 * np.pi * (gx * 1.5 + gy * 0.7 + 0.03 * t + hue))
            base[..., 0] = (60 + 80 * s).astype(np.uint8)
            base[..., 1] = (70 + 60 * (1 - s)).astype(np.uint8)
            base[..., 2] = (50 + 40 * s * gy).astype(np.uint8)
            frame = cv2.add(base, nrng.integers(0, 12, (h, w, 3), dtype=np.uint8))
            if photo is not None and animals[0]["t0"] <= t <= animals[0]["t1"]:
                u = (t - animals[0]["t0"]) / max(0.1, animals[0]["t1"] - animals[0]["t0"])
                x0 = int(u * (w - photo.shape[1]))
                frame[y0:y0 + photo.shape[0], x0:x0 + photo.shape[1]] = photo
            p.stdin.write(frame.tobytes())
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError("encodage ffmpeg échoué")
        gt = {"name": name + ".mp4", "duration": dur, "lang": lang, "text": text,
              "speech_t0": round(t_speech, 2) if t_speech is not None else None, "animals": animals}
        with open(os.path.join(out_dir, name + ".gt.json"), "w", encoding="utf-8") as f:
            json.dump(gt, f, ensure_ascii=False, indent=1)
        return gt
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vidp synth")
    ap.add_argument("--out", default="data/videos")
    ap.add_argument("--animals", default="data/animals")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    photos = {}
    for f in PHOTOS:
        p = os.path.join(a.animals, f)
        if os.path.exists(p):
            photos[f] = cv2.imread(p)
    if not photos:
        print("! aucune image d'animal (python -m vidp fetch-models --animals data/animals)", file=sys.stderr)
    rng = random.Random(a.seed)
    for i in range(a.count):
        gt = make_video(i, a.out, photos, rng, a.duration)
        print(f"{gt['name']} : langue={gt['lang']} animaux={[x['label'] for x in gt['animals']]}", flush=True)


if __name__ == "__main__":
    main()
