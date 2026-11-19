import logging
import sys

USAGE = """usage : python -m vidp <commande>
  stage <downscale|langident|subtitle|animal>   conteneur d'étage Edge
  uploader                                      téléverseur Edge (tampon -> Cloud)
  feeder                                        générateur de charge (vidéos -> magasin local)
  cloud-worker                                  traitement « Video Production » dans le Cloud
  energy-exporter                               exportateur d'énergie (RAPL ou modèle)
  synth                                         vidéos de test avec vérité terrain
  fetch-models                                  télécharge et vérifie les modèles
(API Cloud : uvicorn --factory vidp.cloud_app:ingest | vidp.cloud_app:public)"""


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    if cmd == "stage" and args:
        from .config import EdgeCfg
        from .edge_worker import StageWorker
        StageWorker(args[0], EdgeCfg()).run()
    elif cmd in ("uploader", "edge-uploader"):
        from .config import UploaderCfg
        from .edge_uploader import Uploader
        Uploader(UploaderCfg()).run()
    elif cmd == "feeder":
        from .feeder import main as m
        m(args)
    elif cmd == "cloud-worker":
        from .cloud import run_worker
        run_worker()
    elif cmd == "energy-exporter":
        from .energy import main as m
        m()
    elif cmd == "synth":
        from .synth import main as m
        m(args)
    elif cmd == "fetch-models":
        from .models import main as m
        m(args)
    else:
        print(USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
