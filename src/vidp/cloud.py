"""Cloud VidP : API d'ingestion (TLS, identité de nœud) + traitement « Video Production » + API publique.

Rôles (même image, un conteneur chacun) :
  ingest  :8443  reçoit statuts, résultats et fichiers (téléversement par morceaux avec reprise,
                 SHA-256 vérifié, idempotent) ; range les fichiers (S3) et les métadonnées (DynamoDB)
  worker         « Video Production » : exécute les étages restants sur les vidéos déportées
                 (cloud_only : les 4 étages ; hybride : LangIdent -> Subtitle -> Animal)
  public  :8080  lecture seule pour la page web (servie par Apache2) : /api/videos, /api/stats, /media/...
"""
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.concurrency import run_in_threadpool

from . import pipeline, stages
from .common import check_token, read_json, write_json_atomic
from .config import STAGES, CloudCfg
from .jobs import JobQueue
from .storage import make_storage

log = logging.getLogger("cloud")

M_IN = Counter("cloud_ingest_bytes_total", "Octets reçus", ["kind"])
M_VID = Counter("cloud_videos_total", "Vidéos par événement", ["event"])
M_DUP = Counter("cloud_duplicates_total", "Doublons ignorés (idempotence)", ["kind"])
M_AUTH = Counter("cloud_auth_failures_total", "Authentifications refusées")
M_COMMIT = Histogram("cloud_commit_seconds", "Durée de validation d'un fichier", ["kind"],
                     buckets=(.01, .05, .1, .25, .5, 1, 2, 5, 10, 30))
M_STAGE = Histogram("cloud_stage_seconds", "Durée d'un étage dans le Cloud", ["stage"],
                    buckets=(.1, .25, .5, 1, 2, 4, 8, 15, 30, 60, 120))
M_QUEUE = Gauge("cloud_queue_videos", "Vidéos en attente de traitement Cloud")
M_PUB = Histogram("cloud_publish_latency_seconds", "t0 (arrivée Edge) -> publication", ["processed_at"],
                  buckets=(1, 2, 3, 5, 8, 13, 21, 34, 55, 90, 150, 300, 600))

EXT = {"thumb": "jpg", "subs": "vtt", "video": "mp4"}


def media_key(run_id, vid, kind):
    if kind == "video":
        return f"sources/{vid}.mp4"           # la vidéo source est conservée (énoncé : « enregistre sur S3 »)
    return f"{run_id or 'norun'}/{vid}/{kind}.{EXT.get(kind, 'mp4')}"


def cloud_load(cfg: CloudCfg, jobs: JobQueue):
    n, secs = jobs.pending(STAGES)
    st = read_json(os.path.join(cfg.data_dir, "worker_state.json"), {})
    return {"queue_videos": n, "queue_s": round(secs, 2), "s_per_s": st.get("s_per_s")}


# ====================================================================== application web
def create_app(role: str, cfg: CloudCfg | None = None) -> FastAPI:
    cfg = cfg or CloudCfg()
    meta, blobs = make_storage(cfg)
    os.makedirs(os.path.join(cfg.data_dir, "uploads"), exist_ok=True)
    jobs = JobQueue(os.path.join(cfg.data_dir, "cloud_jobs.db"))
    app = FastAPI(title=f"VidP cloud ({role})", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "role": role, **(cloud_load(cfg, jobs) if role != "public" else {})}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if role in ("ingest", "all"):
        def auth(x_node_id: str = Header(""), x_node_token: str = Header("")):
            if not check_token(cfg.master_secret, x_node_id, x_node_token):
                M_AUTH.inc()
                raise HTTPException(401, "identité du nœud refusée")
            return x_node_id

        def check_vid(vid):
            if not (vid.isalnum() and len(vid) <= 64):
                raise HTTPException(400, "identifiant invalide")

        @app.post("/v1/videos/{vid}/{kind}")
        async def post_meta(vid: str, kind: str, request: Request, node=Depends(auth)):
            check_vid(vid)
            body = await request.body()
            M_IN.labels(kind).inc(len(body))
            doc = json.loads(body)
            if doc.get("node") != node or doc.get("video_id") != vid:
                raise HTTPException(403, "nœud ou vidéo usurpé")
            if kind == "status":
                new = meta.upsert_status(doc)
            elif kind == "result":
                new = meta.put_result(doc)
                if new:
                    M_PUB.labels("edge").observe(max(0.0, time.time() - float(doc.get("t0") or time.time())))
            else:
                raise HTTPException(404)
            M_VID.labels(kind).inc() if new else M_DUP.labels(kind).inc()
            return {"ok": True, "duplicate": not new}

        def up_paths(uid):
            if not uid.replace("_", "").isalnum() or len(uid) > 96:
                raise HTTPException(400, "identifiant invalide")
            d = os.path.join(cfg.data_dir, "uploads")
            return os.path.join(d, uid + ".part"), os.path.join(d, uid + ".done")

        @app.get("/v1/uploads/{uid}")
        def up_status(uid: str, node=Depends(auth)):
            part, done = up_paths(uid)
            if os.path.exists(done):
                return {"committed": True, "offset": 0}
            return {"committed": False, "offset": os.path.getsize(part) if os.path.exists(part) else 0}

        @app.patch("/v1/uploads/{uid}")
        async def up_chunk(uid: str, offset: int, request: Request, node=Depends(auth)):
            part, _ = up_paths(uid)
            cur = os.path.getsize(part) if os.path.exists(part) else 0
            if offset != cur:
                return JSONResponse({"offset": cur}, status_code=409)
            data = await request.body()
            with open(part, "ab") as f:
                f.write(data)
            M_IN.labels("chunk").inc(len(data))
            return {"offset": cur + len(data)}

        def _commit(uid, size, sha256, raw, node, run_id):
            t_start = time.time()
            part, done = up_paths(uid)
            if os.path.exists(done):
                M_DUP.labels("upload").inc()
                return json.loads(open(done, encoding="utf-8").read()) | {"duplicate": True}
            if not os.path.exists(part) or os.path.getsize(part) != size:
                raise HTTPException(409, "taille incohérente")
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            if h.hexdigest() != sha256:
                os.remove(part)
                raise HTTPException(422, "empreinte invalide : renvoyer")
            vid, kind = uid.rsplit("_", 1)
            info = json.loads(raw or b"{}")
            key = media_key(run_id, vid, kind)
            ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
            if kind == "video":
                # vidéo à traiter dans le Cloud : archivée (S3) puis confiée au worker « Video Production »
                if info.get("node") != node or info.get("video_id") != vid:
                    raise HTTPException(403, "nœud ou vidéo usurpé")
                blobs.put_file(key, part, ctype)
                wd = pipeline.wdir(os.path.join(cfg.data_dir, "work"), vid)
                start = info.get("start_stage", "downscale")
                src = os.path.join(wd, "src.mp4" if start == "downscale" else "down.mp4")
                os.replace(part, src)
                data = info.get("data") or {}
                dur = data.get("duration") or stages.probe(src)["duration"]
                jobs.add(vid, start, info.get("run_id", run_id), info.get("name", ""), src, float(info["t0"]),
                         dur, route="cloud", data={**data, "_mode": info.get("mode"), "_node": node,
                                                   "_timings": info.get("timings", {})})
                meta.upsert_status({k: info.get(k) for k in ("video_id", "run_id", "node", "mode", "name", "t0")}
                                   | {"state": "cloud_processing"})
                M_VID.labels("offloaded").inc()
            else:
                blobs.put_file(key, part, ctype)
                meta.add_media(vid, kind, key, size)
                os.remove(part)
            resp = {"ok": True, "key": key}
            with open(done, "w", encoding="utf-8") as f:
                f.write(json.dumps(resp))
            M_COMMIT.labels("video" if kind == "video" else kind.rstrip("0123456789")).observe(time.time() - t_start)
            return resp

        @app.post("/v1/uploads/{uid}/commit")
        async def up_commit(uid: str, size: int, sha256: str, request: Request, node=Depends(auth),
                            x_run_id: str = Header("")):
            raw = await request.body()
            return await run_in_threadpool(_commit, uid, size, sha256, raw, node, x_run_id)

    if role in ("public", "all"):
        @app.get("/api/videos")
        def api_videos(run_id: str | None = None, limit: int = 100):
            return meta.list(run_id, max(1, min(limit, 2000)))

        @app.get("/api/videos/{vid}")
        def api_video(vid: str):
            v = meta.get(vid)
            if not v:
                raise HTTPException(404)
            return v

        @app.get("/api/stats")
        def api_stats(run_id: str | None = None):
            vs = meta.list(run_id, 5000)
            by_state, animals, langs, nbytes = {}, {}, {}, 0
            for v in vs:
                by_state[v.get("state")] = by_state.get(v.get("state"), 0) + 1
                for a in v.get("animals") or []:
                    animals[a["label"]] = animals.get(a["label"], 0) + 1
                if v.get("lang"):
                    langs[v["lang"]] = langs.get(v["lang"], 0) + 1
                nbytes += sum(m.get("bytes", 0) for k, m in (v.get("media") or {}).items() if k != "video")
            return {"videos": len(vs), "by_state": by_state, "animals": animals, "langs": langs,
                    "extract_bytes": nbytes}

        @app.get("/media/{key:path}")
        def media(key: str):
            if ".." in key or key.startswith("sources/"):      # les vidéos source ne sont pas publiques
                raise HTTPException(404)
            o = blobs.open(key)
            if not o:
                raise HTTPException(404)
            it, n = o
            return StreamingResponse(it, media_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
                                     headers={"Content-Length": str(n), "Cache-Control": "public, max-age=3600"})

        @app.get("/", response_class=HTMLResponse)
        def index():                     # en production, Apache2 sert cette même page (cloud/docker-compose.yml)
            with open(os.path.join(os.path.dirname(__file__), "web", "index.html"), encoding="utf-8") as f:
                return f.read()

    return app


# ====================================================================== worker « Video Production »
def run_worker(cfg: CloudCfg | None = None):
    from prometheus_client import start_http_server
    cfg = cfg or CloudCfg()
    meta, blobs = make_storage(cfg)
    jobs = JobQueue(os.path.join(cfg.data_dir, "cloud_jobs.db"))
    work = os.path.join(cfg.data_dir, "work")
    start_http_server(int(os.environ.get("METRICS_PORT", 9200)))
    for s in STAGES:
        jobs.requeue_running(s)
    s_per_s = None
    log.info("worker Video Production prêt (stockage=%s)", cfg.storage)
    while True:
        M_QUEUE.set(jobs.pending(STAGES)[0])
        job = next((j for j in (jobs.claim(s) for s in STAGES) if j), None)
        if not job:
            time.sleep(0.3)
            continue
        vid = job["video_id"]
        try:
            # la vidéo reste « running » jusqu'à publication : après un crash, elle est reprise
            # depuis son étage d'entrée (traitement rejouable, publication idempotente)
            stage = job["stage"]
            while stage != "done":
                res, dt = pipeline.run_stage(stage, job, work, cfg.models)
                M_STAGE.labels(stage).observe(dt)
                job["data"].update(res)
                job["timings"][stage] = round(dt, 4)
                stage = pipeline.NEXT[stage]
            files, clips = pipeline.build_outputs(job, work, "full")
            for kind, p, n in files:
                key = media_key(job["run_id"], vid, kind)
                blobs.put_file(key, p, mimetypes.guess_type(key)[0] or "application/octet-stream")
                meta.add_media(vid, kind, key, n)
            d = job["data"]
            job["timings"] = {**d.pop("_timings", {}), **job["timings"]}
            doc = pipeline.result_doc(job, d.get("_node", ""), d.get("_mode", ""), "cloud", clips, "full")
            if meta.put_result(doc):
                M_PUB.labels("cloud").observe(max(0.0, time.time() - job["t0"]))
                M_VID.labels("published").inc()
            jobs.advance(vid, "done", {"timings_cloud": job["timings"]}, None)
            dur = float(d.get("duration") or 0)
            if dur > 0:
                local = sum(job["timings"].get(s, 0) for s in ("langident", "subtitle", "animal")) / dur
                s_per_s = local if s_per_s is None else 0.7 * s_per_s + 0.3 * local
                write_json_atomic(os.path.join(cfg.data_dir, "worker_state.json"),
                                  {"s_per_s": round(s_per_s, 4), "t": time.time()})
            shutil.rmtree(os.path.join(work, vid), ignore_errors=True)
        except Exception as e:
            log.exception("traitement Cloud de %s", vid)
            jobs.fail(vid, f"{type(e).__name__}: {e}")
