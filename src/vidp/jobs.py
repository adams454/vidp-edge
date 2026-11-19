"""File de travaux persistante (SQLite/WAL) partagée par les conteneurs du pipeline.

Chaque vidéo est un travail qui avance d'étage en étage :
    downscale -> langident -> subtitle -> animal -> done          (traitement local)
    downscale -> remote                                          (déporté vers le Cloud, mode hybride)
    remote                                                       (cloud_only : la vidéo source part telle quelle)
Un conteneur par étage « réclame » (claim) un travail prêt, le traite, puis le fait avancer.
Après un redémarrage, les travaux restés « running » de l'étage repassent « ready » : rien n'est perdu.
Le même code sert de file de traitement dans le Cloud.
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  video_id TEXT PRIMARY KEY, run_id TEXT, name TEXT, src TEXT, t0 REAL, duration REAL DEFAULT 0,
  stage TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready', route TEXT DEFAULT 'edge',
  data TEXT DEFAULT '{}', timings TEXT DEFAULT '{}', attempts INTEGER DEFAULT 0, error TEXT,
  created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS ix_jobs ON jobs(stage, state, t0);
"""
MAX_ATTEMPTS = 3


class JobQueue:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------------
    def _row(self, r):
        if r is None:
            return None
        keys = [d[0] for d in self.cur_desc]
        d = dict(zip(keys, r))
        d["data"] = json.loads(d.get("data") or "{}")
        d["timings"] = json.loads(d.get("timings") or "{}")
        return d

    def _q1(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.cur_desc = cur.description
        return self._row(cur.fetchone())

    # ------------------------------------------------------------------
    def add(self, video_id, stage, run_id="", name="", src="", t0=0.0, duration=0.0, route="edge",
            data=None) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO jobs(video_id,run_id,name,src,t0,duration,stage,state,route,data,created,updated)"
                " VALUES(?,?,?,?,?,?,?,'ready',?,?,?,?)",
                (video_id, run_id, name, src, t0, duration, stage, route, json.dumps(data or {}),
                 time.time(), time.time()))
            return cur.rowcount == 1

    def get(self, video_id):
        with self.lock:
            return self._q1("SELECT * FROM jobs WHERE video_id=?", (video_id,))

    def claim(self, stage):
        """Réserve le plus ancien travail prêt pour cet étage (atomique entre processus)."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                j = self._q1("SELECT * FROM jobs WHERE stage=? AND state='ready' ORDER BY t0 LIMIT 1", (stage,))
                if j:
                    self.db.execute("UPDATE jobs SET state='running', updated=? WHERE video_id=?",
                                    (time.time(), j["video_id"]))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return j

    def advance(self, video_id, next_stage, data=None, timing=None, route=None, duration=None):
        """Termine l'étage courant : fusionne les données, note le temps passé, passe à l'étage suivant."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                j = self._q1("SELECT data, timings FROM jobs WHERE video_id=?", (video_id,))
                d = {**j["data"], **(data or {})}
                t = dict(j["timings"])
                if timing:
                    t[timing[0]] = round(float(timing[1]), 4)
                sets = "stage=?, state=?, data=?, timings=?, updated=?, attempts=0, error=NULL"
                args = [next_stage, "done" if next_stage in ("done", "remote") else "ready",
                        json.dumps(d), json.dumps(t), time.time()]
                if route:
                    sets += ", route=?"
                    args.append(route)
                if duration is not None:
                    sets += ", duration=?"
                    args.append(duration)
                self.db.execute(f"UPDATE jobs SET {sets} WHERE video_id=?", (*args, video_id))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def fail(self, video_id, error: str):
        with self.lock:
            self.db.execute(
                "UPDATE jobs SET attempts=attempts+1, error=?, updated=?,"
                " state=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'ready' END WHERE video_id=?",
                (error[:500], time.time(), MAX_ATTEMPTS, video_id))

    def requeue_running(self, stage):
        with self.lock:
            self.db.execute("UPDATE jobs SET state='ready' WHERE stage=? AND state='running'", (stage,))

    def pending(self, stages):
        """(nombre, secondes de vidéo) des travaux en attente ou en cours dans ces étages."""
        q = ",".join("?" * len(stages))
        with self.lock:
            n, s = self.db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(duration),0) FROM jobs WHERE stage IN ({q})"
                " AND state IN ('ready','running')", list(stages)).fetchone()
        return n, s

    def recent_done(self, n=20):
        """[(durée vidéo, timings)] des derniers travaux terminés localement (apprentissage des temps)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT duration, timings FROM jobs WHERE stage='done' AND route='edge' AND duration>0"
                " ORDER BY updated DESC LIMIT ?", (n,)).fetchall()
        return [(d, json.loads(t or "{}")) for d, t in rows]

    def counts(self):
        with self.lock:
            return {f"{s}:{st}": n for s, st, n in self.db.execute(
                "SELECT stage, state, COUNT(*) FROM jobs GROUP BY stage, state").fetchall()}

    def close(self):
        self.db.close()
