"""Points d'entrée uvicorn (avec --factory) : `vidp.cloud_app:ingest` / `vidp.cloud_app:public` / `:all`."""
from .cloud import create_app


def ingest():
    return create_app("ingest")


def public():
    return create_app("public")


def all():  # noqa: A001 — développement : un seul processus pour tout
    return create_app("all")
