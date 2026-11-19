"""Téléverseur Edge : envoi fiable vers le Cloud (reprise, déduplication, backoff)."""
import hashlib
import json
import logging
import os
import random
import threading
import time

import requests
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from .common import append_jsonl, write_json_atomic
from .config import UploaderCfg
from .metrics import iface_bytes
from .outbox import Outbox

log = logging.getLogger("uploader")

M_BYTES = Counter("uploader_app_bytes_sent_total", "Octets applicatifs envoyés", ["kind"])
M_ACK = Counter("uploader_acked_total", "Éléments acquittés", ["kind"])
M_RETRY = Counter("uploader_retries_total", "Échecs / reprises", ["kind"])
M_DUP = Counter("uploader_duplicates_total", "Réponses 'déjà reçu'")
M_UP = Gauge("uploader_wan_up", "1 si le Cloud répond à la sonde")
M_BW = Gauge("uploader_bw_ewma_bytes_per_s", "Débit utile EWMA")
M_RTT = Gauge("uploader_probe_rtt_seconds", "RTT de la sonde")
M_QI = Gauge("uploader_queue_items", "Éléments en attente")
M_QB = Gauge("uploader_queue_bytes", "Octets en attente")
M_IFTX = Gauge("wan_if_tx_bytes_total", "Octets émis sur l'interface WAN (compteur noyau)")
M_IFRX = Gauge("wan_if_rx_bytes_total", "Octets reçus sur l'interface WAN (compteur noyau)")
M_LAT = Histogram("uploader_result_latency_seconds", "Événement -> acquittement Cloud", ["kind"],
                  buckets=(.1, .25, .5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 90, 150, 300, 600))


class Uploader:
    def __init__(self, cfg: UploaderCfg):
        self.cfg = cfg
        self.ob = Outbox(cfg.spool_dir)
        self.res_dir = os.path.join(cfg.results_dir, cfg.run_id)
        os.makedirs(self.res_dir, exist_ok=True)
        self.s = requests.Session()
        self.verify = cfg.ca_bundle if cfg.ca_bundle else True
        self.hdr = {"X-Node-Id": cfg.node_id, "X-Node-Token": cfg.node_token, "X-Run-Id": cfg.run_id}
        self.bw = 0.0                # EWMA octets/s
        self.up = True
        self.rtt = 0.0
        self.busy = set()
        self.lock = threading.Lock()
        self.was_down = False

    # --------------------------------------------------------------
    def probe_loop(self):
        csvp = os.path.join(self.res_dir, "uploader_backlog.csv")
        if not os.path.exists(csvp):
            open(csvp, "w").write("t,items,bytes,wan_up,bw,tx_bytes\n")
        cloud = {}
        while True:
            t = time.time()
            try:
                r = self.s.get(self.cfg.cloud_url + "/healthz", headers=self.hdr,
                               timeout=(2, 2), verify=self.verify)
                self.up = r.status_code == 200
                self.rtt = time.time() - t
                if self.up:
                    cloud = r.json()           # charge du Cloud : file (s de vidéo) et coût par s de vidéo
            except (requests.RequestException, ValueError):
                self.up = False
            if self.up and self.was_down:
                self.ob.wake_all()       # reprise immédiate à la reconnexion
            self.was_down = not self.up
            n, b = self.ob.backlog()
            rx, tx = iface_bytes(self.cfg.iface)
            M_UP.set(1 if self.up else 0); M_RTT.set(self.rtt)
            M_QI.set(n); M_QB.set(b); M_BW.set(self.bw)
            M_IFTX.set(tx); M_IFRX.set(rx)
            write_json_atomic(os.path.join(self.cfg.spool_dir, "wan_state.json"),
                              {"up": self.up, "bw": self.bw, "rtt": self.rtt, "t": t,
                               "cloud_queue_s": cloud.get("queue_s", 0), "cloud_s_per_s": cloud.get("s_per_s")})
            with open(csvp, "a") as f:
                f.write(f"{time.time():.3f},{n},{b},{int(self.up)},{self.bw:.0f},{tx}\n")
            time.sleep(1.0)

    # --------------------------------------------------------------
    def _post_meta(self, it):
        body = it["payload"].encode()
        r = self.s.post(f"{self.cfg.cloud_url}/v1/videos/{it['event_id']}/{it['kind']}", data=body,
                        verify=self.verify,
                        headers={**self.hdr, "Content-Type": "application/json",
                                 "Idempotency-Key": f"{it['event_id']}:{it['kind']}"},
                        timeout=(self.cfg.connect_timeout, self.cfg.read_timeout))
        return r, len(body) + 400

    def _upload_file(self, it):
        """Téléversement par morceaux avec reprise : on interroge l'offset du serveur."""
        kind = it["kind"]
        path = it["path"]
        size = os.path.getsize(path)
        base = f"{self.cfg.cloud_url}/v1/uploads/{it['event_id']}_{kind}"
        to = (self.cfg.connect_timeout, self.cfg.read_timeout)
        r = self.s.get(base, headers=self.hdr, timeout=to, verify=self.verify)
        r.raise_for_status()
        st = r.json()
        if st.get("committed"):
            return st, 300
        off = min(st.get("offset", 0), size)
        sent = 300
        chunk = self.cfg.chunk_kb * 1024
        with open(path, "rb") as f:
            f.seek(off)
            while off < size:
                data = f.read(chunk)
                t = time.time()
                r = self.s.patch(base, params={"offset": off}, data=data, verify=self.verify,
                                 headers={**self.hdr, "Content-Type": "application/octet-stream"}, timeout=to)
                if r.status_code == 409:   # décalage : on se resynchronise
                    off = r.json().get("offset", 0)
                    f.seek(off)
                    continue
                r.raise_for_status()
                dt = max(time.time() - t, 1e-3)
                if len(data) > 20_000:
                    inst = len(data) / dt
                    self.bw = inst if self.bw == 0 else 0.7 * self.bw + 0.3 * inst
                off += len(data)
                sent += len(data) + 300
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        meta = it["payload"] or "{}"
        r = self.s.post(base + "/commit", params={"size": size, "sha256": h.hexdigest()},
                        data=meta.encode(), verify=self.verify,
                        headers={**self.hdr, "Content-Type": "application/json"},
                        timeout=(self.cfg.connect_timeout, max(self.cfg.read_timeout, 60)))
        r.raise_for_status()
        return r.json(), sent + 400

    # --------------------------------------------------------------
    def handle(self, it):
        kind = it["kind"]
        try:
            if kind in ("status", "result"):
                r, sent = self._post_meta(it)
                r.raise_for_status()
                resp = r.json()
            else:
                resp, sent = self._upload_file(it)
            t_ack = time.time()
            if resp.get("duplicate"):
                M_DUP.inc()
            M_BYTES.labels(kind).inc(sent)
            M_ACK.labels(kind).inc()
            self.ob.done(it["id"], t_ack)
            if it["path"]:          # fichier livré et vérifié (SHA-256) : on libère le tampon Edge
                try:
                    os.remove(it["path"])
                except OSError:
                    pass
            lat = t_ack - it["t_event"]
            M_LAT.labels(kind).observe(lat)
            append_jsonl(os.path.join(self.res_dir, "acks.jsonl"), {
                "event_id": it["event_id"], "kind": kind, "t_event": it["t_event"],
                "t_created": it["created"], "t_ack": t_ack, "latency": lat,
                "attempts": it["attempts"] + 1, "bytes": sent, "file_bytes": it["size"],
                "quality": it["quality"], "duplicate": bool(resp.get("duplicate"))})
        except (requests.RequestException, ValueError) as e:
            M_RETRY.labels(kind).inc()
            delay = min(30.0, 0.5 * (2 ** min(it["attempts"], 6))) * random.uniform(0.7, 1.3)
            self.ob.retry(it["id"], delay)
            log.debug("échec %s %s : %s (retry %.1fs)", kind, it["event_id"], type(e).__name__, delay)
        finally:
            with self.lock:
                self.busy.discard(it["id"])

    def run(self):
        start_http_server(self.cfg.metrics_port)
        threading.Thread(target=self.probe_loop, daemon=True).start()
        log.info("téléverseur -> %s", self.cfg.cloud_url)
        while True:
            with self.lock:
                nbusy = len(self.busy)
                skip = tuple(self.busy)
            if nbusy >= self.cfg.workers or (not self.up and nbusy > 0):
                time.sleep(0.05)
                continue
            it = self.ob.next_ready(skip_ids=skip)
            if not it:
                time.sleep(0.1)
                continue
            if not self.up and it["attempts"] > 0:
                time.sleep(0.2)           # pas de martèlement quand le lien est coupé
                continue
            with self.lock:
                self.busy.add(it["id"])
            threading.Thread(target=self.handle, args=(it,), daemon=True).start()
