# Image unique (Edge + Cloud) — multi-architecture (amd64 / arm64) via buildx.
# Les modèles sont téléchargés AU BUILD et vérifiés par SHA-256 (vidp/models.py) : rien n'est téléchargé
# à l'exécution, et une empreinte invalide fait échouer le build. Aucun secret dans l'image.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vidp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ /app/src/
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 MODELS_DIR=/models HF_HUB_OFFLINE=1
RUN python -m vidp fetch-models --dir /models --animals /app/animals && chmod -R a+rX /models /app/animals
USER 10001
ENTRYPOINT ["python", "-m", "vidp"]
