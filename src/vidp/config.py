"""Configuration par variables d'environnement (aucun secret dans l'image)."""
import os
from dataclasses import dataclass, field


def _f(name, default):
    return float(os.environ.get(name, default))


def _i(name, default):
    return int(os.environ.get(name, default))


def _s(name, default=""):
    return os.environ.get(name, default)


# Ordre du pipeline VidP (énoncé) : Downscale -> LangIdent -> Subtitle -> Animal detect
STAGES = ["downscale", "langident", "subtitle", "animal"]


@dataclass
class ModelCfg:
    models_dir: str = field(default_factory=lambda: _s("MODELS_DIR", "/models"))
    whisper: str = field(default_factory=lambda: _s("WHISPER_MODEL", "tiny"))       # Subtitle : tiny = sobre
    langid_model: str = field(default_factory=lambda: _s("LANGID_MODEL", "base"))   # LangIdent : base = fiable
    whisper_threads: int = field(default_factory=lambda: _i("WHISPER_THREADS", 1))
    animal_fps: float = field(default_factory=lambda: _f("ANIMAL_FPS", 2.0))           # trames analysées par seconde
    animal_conf: float = field(default_factory=lambda: _f("ANIMAL_CONF", 0.45))
    downscale_height: int = field(default_factory=lambda: _i("DOWNSCALE_HEIGHT", 360))


@dataclass
class EdgeCfg:
    mode: str = field(default_factory=lambda: _s("MODE", "hybrid"))  # cloud_only|edge_first|hybrid
    node_id: str = field(default_factory=lambda: _s("NODE_ID", "edge-01"))
    run_id: str = field(default_factory=lambda: _s("RUN_ID", "dev"))
    inbox_dir: str = field(default_factory=lambda: _s("INBOX_DIR", "/data/inbox"))   # magasin vidéo local
    spool_dir: str = field(default_factory=lambda: _s("SPOOL_DIR", "/spool"))
    results_dir: str = field(default_factory=lambda: _s("RESULTS_DIR", "/results"))
    spool_quota_mb: int = field(default_factory=lambda: _i("SPOOL_QUOTA_MB", 1024))
    t_budget_s: float = field(default_factory=lambda: _f("T_BUDGET_S", 20))      # budget d'envoi des extraits
    offload_margin: float = field(default_factory=lambda: _f("OFFLOAD_MARGIN", 0.2))
    metrics_port: int = field(default_factory=lambda: _i("METRICS_PORT", 9110))
    models: ModelCfg = field(default_factory=ModelCfg)


@dataclass
class UploaderCfg:
    cloud_url: str = field(default_factory=lambda: _s("CLOUD_URL", "https://127.0.0.1:8443"))
    ca_bundle: str = field(default_factory=lambda: _s("CLOUD_CA", ""))  # '' => défaut système
    node_id: str = field(default_factory=lambda: _s("NODE_ID", "edge-01"))
    node_token: str = field(default_factory=lambda: _s("NODE_TOKEN", ""))
    run_id: str = field(default_factory=lambda: _s("RUN_ID", "dev"))
    spool_dir: str = field(default_factory=lambda: _s("SPOOL_DIR", "/spool"))
    results_dir: str = field(default_factory=lambda: _s("RESULTS_DIR", "/results"))
    chunk_kb: int = field(default_factory=lambda: _i("CHUNK_KB", 256))
    connect_timeout: float = field(default_factory=lambda: _f("CONNECT_TIMEOUT", 3))
    read_timeout: float = field(default_factory=lambda: _f("READ_TIMEOUT", 10))
    metrics_port: int = field(default_factory=lambda: _i("METRICS_PORT", 9101))
    iface: str = field(default_factory=lambda: _s("WAN_IFACE", "eth0"))
    workers: int = field(default_factory=lambda: _i("UPLOAD_WORKERS", 2))


@dataclass
class CloudCfg:
    data_dir: str = field(default_factory=lambda: _s("DATA_DIR", "/data"))
    master_secret: str = field(default_factory=lambda: _s("CLOUD_MASTER_SECRET", ""))
    storage: str = field(default_factory=lambda: _s("STORAGE", "local"))   # local | aws
    s3_bucket: str = field(default_factory=lambda: _s("S3_BUCKET", ""))
    ddb_table: str = field(default_factory=lambda: _s("DDB_TABLE", ""))
    aws_region: str = field(default_factory=lambda: _s("AWS_REGION", "eu-west-3"))
    s3_endpoint: str = field(default_factory=lambda: _s("S3_ENDPOINT", ""))     # MinIO (tests sans AWS)
    ddb_endpoint: str = field(default_factory=lambda: _s("DDB_ENDPOINT", ""))   # DynamoDB Local
    workers: int = field(default_factory=lambda: _i("CLOUD_WORKERS", 1))
    models: ModelCfg = field(default_factory=ModelCfg)
