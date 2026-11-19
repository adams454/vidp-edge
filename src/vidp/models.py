"""Téléchargement VÉRIFIÉ des modèles et des images de test (empreintes SHA-256 épinglées).

    python -m vidp fetch-models --dir /models [--animals data/animals]

Les fichiers sont intégrés à l'image Docker au build : aucun téléchargement à l'exécution,
et une empreinte qui ne correspond pas fait échouer le build (chaîne d'approvisionnement).
Sources : Whisper tiny et base convertis CTranslate2 (Systran, licence MIT) ; MobileNet-SSD VOC (Caffe, MIT) ;
images de test du dépôt darknet (domaine public).
"""
import argparse
import hashlib
import os
import sys
import urllib.request

HF = "https://huggingface.co/Systran/faster-whisper-tiny/resolve/d90ca5fe260221311c53c58e660288d3deb8d356/"  # révision épinglée
HFB = "https://huggingface.co/Systran/faster-whisper-base/resolve/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66/"
SSD = "https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/"
DARKNET = "https://raw.githubusercontent.com/pjreddie/darknet/master/data/"

MODELS = {
    "MobileNetSSD_deploy.prototxt": (SSD + "MobileNetSSD_deploy.prototxt",
                                     "e781559c4f5beaec2a486ccd952af5b6fa408e9498761bf5f4fb80b4e9f0d25e"),
    "MobileNetSSD_deploy.caffemodel": (SSD + "MobileNetSSD_deploy.caffemodel",
                                       "761c86fbae3d8361dd454f7c740a964f62975ed32f4324b8b85994edec30f6af"),
    "whisper-tiny/config.json": (HF + "config.json", "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8"),
    "whisper-tiny/model.bin": (HF + "model.bin", "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"),
    "whisper-tiny/tokenizer.json": (HF + "tokenizer.json",
                                    "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "whisper-tiny/vocabulary.txt": (HF + "vocabulary.txt", "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
    # LangIdent : Whisper « base » (identification de langue nettement plus fiable que « tiny »)
    "whisper-base/config.json": (HFB + "config.json", "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a"),
    "whisper-base/model.bin": (HFB + "model.bin", "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
    "whisper-base/tokenizer.json": (HFB + "tokenizer.json",
                                    "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "whisper-base/vocabulary.txt": (HFB + "vocabulary.txt", "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
}
ANIMALS = {
    "dog.jpg": (DARKNET + "dog.jpg", "5a9522051c3cec2bbd2f6323fccba32e8fbf3ddcc2b3e2fd46b04c720bc6f866"),
    "horses.jpg": (DARKNET + "horses.jpg", "c8f0a677a1356569e2ce71d2fa88c1030c0ae57ecf5e14170e02d9a86a20dcb4"),
    "eagle.jpg": (DARKNET + "eagle.jpg", "f95f57297fd51a6c5ddceb5ed6916fa759d1d65f402fee34c05b31a9608755c0"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def fetch(manifest, root):
    ok = True
    for rel, (url, digest) in manifest.items():
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not (os.path.exists(dst) and sha256(dst) == digest):
            print(f"téléchargement {rel} …", flush=True)
            urllib.request.urlretrieve(url, dst + ".part")
            os.replace(dst + ".part", dst)
        got = sha256(dst)
        if got != digest:
            print(f"EMPREINTE INVALIDE {rel} : {got}", file=sys.stderr)
            ok = False
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vidp fetch-models")
    ap.add_argument("--dir", default=os.environ.get("MODELS_DIR", "/models"))
    ap.add_argument("--animals", default="", help="dossier des images d'animaux (vidéos de test)")
    a = ap.parse_args(argv)
    ok = fetch(MODELS, a.dir)
    if a.animals:
        ok = fetch(ANIMALS, a.animals) and ok
    if not ok:
        sys.exit("vérification des empreintes échouée")
    print("modèles vérifiés :", a.dir)


if __name__ == "__main__":
    main()
