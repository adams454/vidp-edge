"""Conteneur d'étage Edge : `python -m vidp stage <downscale|langident|subtitle|animal>`.

- downscale : surveille le magasin vidéo local (INBOX_DIR), crée un travail par vidéo arrivée (t0),
              applique la politique (cloud_only / edge_first / hybrid) puis réduit la vidéo.
- langident, subtitle : étages intermédiaires.
- animal    : dernier étage ; construit les métadonnées et les extraits utiles et les dépose dans le
              tampon Edge (outbox) d'où le téléverseur les envoie quand le WAN le permet.
"""
import json
import logging
import os
import shutil
import threading
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from . import pipeline, stages
from .common import append_jsonl, read_json, video_id
from .config import STAGES, EdgeCfg
from .jobs import JobQueue
from .metrics import ResourceSampler
from .outbox import Outbox
from .policy import Policy

log = logging.getLogger("stage")

M_STAGE = Histogram("vidp_stage_seconds", "Durée d'un étage par vidéo", ["stage"],
                    buckets=(.1, .25, .5, 1, 2, 4, 8, 15, 30, 60, 120))
M_DONE = Counter("vidp_stage_videos_total", "Vidéos traitées par étage", ["stage", "result"])
M_QUEUE = Gauge("vidp_queue_videos", "Vidéos en attente ou en cours, par étage", ["stage"])
M_ARRIVED = Counter("vidp_videos_arrived_total", "Vidéos arrivées dans le magasin local")
M_ROUTE = Counter("vidp_route_total", "Décisions de placement", ["route"])
M_ETA = Gauge("vidp_policy_eta_seconds", "ETA estimées par la politique", ["where"])
M_QUAL = Counter("vidp_extract_quality_total", "Qualité des extraits choisie", ["quality"])
M_SPOOL = Gauge("vidp_spool_bytes", "Octets en attente dans le tampon Edge")
M_EVICT = Counter("vidp_spool_evicted_total", "Extraits évincés (quota disque)")
M_CPU = Gauge("vidp_stage_cpu_percent", "CPU du conteneur d'étage (% d'un cœur)", ["stage"])


class StageWorker:
    def __init__(self, stage: str, cfg: EdgeCfg):
        if stage not in STAGES:
            raise SystemExit(f"étage inconnu : {stage} (attendu : {', '.join(STAGES)})")
        self.stage, self.cfg = stage, cfg
        self.jobs = JobQueue(os.path.join(cfg.spool_dir, "jobs.db"))
        self.ob = Outbox(cfg.spool_dir)
        self.work = os.path.join(cfg.spool_dir, "work")
        self.res_dir = os.path.join(cfg.results_dir, cfg.run_id)
        os.makedirs(self.res_dir, exist_ok=True)
        self.policy = Policy(cfg.mode, cfg.t_budget_s, cfg.offload_margin)
        self.seen = set()

    def wan(self):
        return read_json(os.path.join(self.cfg.spool_dir, "wan_state.json"), {"up": True, "bw": 0})

    def log(self, **kv):
        append_jsonl(os.path.join(self.res_dir, "videos.jsonl"), {"t": time.time(), "stage": self.stage, **kv})

    # ------------------------------------------------------------------ boucle principale
    def run(self):
        start_http_server(self.cfg.metrics_port)
        ResourceSampler(os.path.join(self.res_dir, f"res_{self.stage}.csv"), M_CPU.labels(self.stage)).start()
        self.jobs.requeue_running(self.stage)
        if self.stage == "downscale":
            os.makedirs(self.cfg.inbox_dir, exist_ok=True)
            threading.Thread(target=self.watch_inbox, daemon=True).start()
        log.info("étage %s prêt (mode=%s, run=%s)", self.stage, self.cfg.mode, self.cfg.run_id)
        while True:
            job = self.jobs.claim(self.stage)
            if not job:
                time.sleep(0.2)
                continue
            try:
                self.process(job)
                M_DONE.labels(self.stage, "ok").inc()
            except Exception as e:
                log.exception("échec %s sur %s", self.stage, job["video_id"])
                M_DONE.labels(self.stage, "error").inc()
                self.jobs.fail(job["video_id"], f"{type(e).__name__}: {e}")
                self.log(video_id=job["video_id"], event="error", error=str(e)[:300])

    # ------------------------------------------------------------------ magasin local -> travaux
    def watch_inbox(self):
        while True:
            try:
                self.scan_inbox()
                for s in STAGES:
                    M_QUEUE.labels(s).set(self.jobs.pending([s])[0])
                M_SPOOL.set(self.ob.backlog()[1])
            except Exception:
                log.exception("magasin local")
            time.sleep(0.5)

    def scan_inbox(self):
        for name in sorted(os.listdir(self.cfg.inbox_dir)):
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")) or name in self.seen:
                continue
            path = os.path.join(self.cfg.inbox_dir, name)
            st = os.stat(path)
            vid = video_id(self.cfg.node_id, self.cfg.run_id, name, st.st_size, st.st_mtime)
            t0 = st.st_mtime          # instant d'arrivée : le chronomètre de latence démarre ici
            self.seen.add(name)
            route = "cloud" if self.cfg.mode == "cloud_only" else "edge"
            if not self.jobs.add(vid, "remote" if route == "cloud" else "downscale", self.cfg.run_id, name, path,
                                 t0, route=route):
                continue
            M_ARRIVED.inc()
            base = {"video_id": vid, "run_id": self.cfg.run_id, "node": self.cfg.node_id, "mode": self.cfg.mode,
                    "name": name, "t0": t0}
            self.ob.put(vid, "status", 0, json.dumps({**base, "state": "edge_processing" if route == "edge"
                                                      else "uploading"}), t_event=t0)
            if route == "cloud":       # baseline : la vidéo source complète traverse le WAN
                M_ROUTE.labels("cloud_raw").inc()
                self.ob.put(vid, "video", 1, json.dumps({**base, "start_stage": "downscale", "data": {}}),
                            path=path, size=st.st_size, t_event=t0, score=1.0)
            self.log(video_id=vid, event="arrived", name=name, t0=t0, bytes=st.st_size, route=route)

    # ------------------------------------------------------------------ un étage
    def process(self, job):
        vid = job["video_id"]
        res, dt = pipeline.run_stage(self.stage, job, self.work, self.cfg.models)
        M_STAGE.labels(self.stage).observe(dt)
        if self.stage == "downscale":
            return self.after_downscale(job, res, dt)
        if self.stage == "animal":
            job["data"].update(res)
            job["timings"][self.stage] = round(dt, 4)
            return self.finalize(job)
        self.jobs.advance(vid, pipeline.NEXT[self.stage], res, (self.stage, dt))

    def after_downscale(self, job, res, dt):
        vid = job["video_id"]
        try:
            os.remove(job["src"])          # la source n'est plus nécessaire : le magasin local reste borné
        except OSError:
            pass
        down = os.path.join(self.work, vid, "down.mp4")
        n_local, s_local = self.jobs.pending(["langident", "subtitle", "animal"])
        where, info = self.policy.route(res["duration"], res["downscaled_bytes"], s_local, self.ob.backlog()[1],
                                        self.wan(), self.jobs.recent_done())
        for k in ("eta_local", "eta_cloud"):
            if k in info:
                M_ETA.labels(k[4:]).set(info[k])
        M_ROUTE.labels(where).inc()
        self.log(video_id=vid, event="routed", route=where, **info)
        if where == "cloud":
            data = {**job["data"], **res}
            payload = {"video_id": vid, "run_id": job["run_id"], "node": self.cfg.node_id, "mode": self.cfg.mode,
                       "name": job["name"], "t0": job["t0"], "start_stage": "langident", "data": data,
                       "timings": {"downscale": round(dt, 4)}}
            self.ob.put(vid, "video", 1, json.dumps(payload), path=down, size=res["downscaled_bytes"],
                        t_event=job["t0"], score=1.0)
            self.jobs.advance(vid, "remote", res, ("downscale", dt), route="cloud", duration=res["duration"])
        else:
            self.jobs.advance(vid, "langident", res, ("downscale", dt), duration=res["duration"])

    def finalize(self, job):
        vid = job["video_id"]
        animals = job["data"].get("animals", [])
        clip_s = sum(b - a for a, b in stages.merge_windows(animals, job["data"].get("duration", 0)))
        q = self.policy.choose_quality(self.wan(), self.ob.backlog()[1], clip_s)
        files, clips = pipeline.build_outputs(job, self.work, q)
        for kind, p, n in files:
            if kind.startswith("clip") and clip_s:
                self.policy.observe_clip(q, n, clip_s / max(1, len(clips)))
        M_QUAL.labels(q).inc()
        doc = pipeline.result_doc(job, self.cfg.node_id, self.cfg.mode, "edge", clips, q)
        self.ob.put(vid, "result", 0, json.dumps(doc), t_event=job["t0"], score=2.0)
        for kind, p, n in files:
            # priorité d'éviction : vignette/sous-titres d'abord gardés, puis les extraits les plus sûrs
            score = 1.5 if kind in ("thumb", "subs") else 1.0 + 0.4 * max([a["conf"] for a in animals] or [0])
            self.ob.put(vid, kind, 1, json.dumps({"kind": kind}), path=p, size=n, t_event=job["t0"], score=score,
                        quality=q)
        try:
            os.remove(os.path.join(self.work, vid, "down.mp4"))
        except OSError:
            pass
        dropped, nb = self.ob.evict(self.cfg.spool_quota_mb * 1_000_000)
        if dropped:
            M_EVICT.inc(dropped)
            self.log(event="evicted", dropped=dropped, bytes=nb)
        self.jobs.advance(vid, "done", {"animals": animals, "frames_analysed": job["data"].get("frames_analysed")},
                          ("animal", job["timings"]["animal"]))
        self.log(video_id=vid, event="done", route="edge", quality=q, timings=job["timings"],
                 n_animals=len(animals), lang=job["data"].get("lang"))


def clean_work(work_root):
    shutil.rmtree(work_root, ignore_errors=True)
