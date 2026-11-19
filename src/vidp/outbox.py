"""File d'attente persistante (SQLite/WAL) partagée entre processeur et téléverseur.

Garanties : au-moins-une-fois côté Edge + idempotence côté Cloud => effet exactement-une-fois.
Priorités : 0 = métadonnées (status/result, petites, urgentes), 1 = fichiers (vignette, sous-titres,
extraits, vidéo à traiter dans le Cloud). Reprise contrôlée : après une coupure, les résultats partent
avant les fichiers, et les fichiers dans l'ordre d'arrivée.
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, kind TEXT NOT NULL, priority INTEGER NOT NULL,
  path TEXT, size INTEGER DEFAULT 0, payload TEXT,
  state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
  created REAL, t_event REAL, score REAL DEFAULT 0, quality TEXT,
  acked REAL, UNIQUE(event_id, kind));
CREATE INDEX IF NOT EXISTS ix_state ON items(state, priority, id);
"""


class Outbox:
    def __init__(self, spool_dir: str):
        os.makedirs(spool_dir, exist_ok=True)
        self.dir = spool_dir
        self.path = os.path.join(spool_dir, "outbox.db")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def put(self, event_id, kind, priority, payload="", path=None, size=0, t_event=0.0,
            score=0.0, quality=None) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO items(event_id,kind,priority,path,size,payload,created,t_event,score,quality)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, kind, priority, path, size, payload, time.time(), t_event, score, quality))
            return cur.rowcount == 1

    def next_ready(self, now=None, skip_ids=()):
        now = now or time.time()
        q = "SELECT id,event_id,kind,priority,path,size,payload,attempts,created,t_event,quality FROM items " \
            "WHERE state='pending' AND next_try<=? "
        args = [now]
        if skip_ids:
            q += "AND id NOT IN (%s) " % ",".join("?" * len(skip_ids))
            args += list(skip_ids)
        q += "ORDER BY priority ASC, id ASC LIMIT 1"
        with self.lock:
            r = self.db.execute(q, args).fetchone()
        if not r:
            return None
        keys = "id event_id kind priority path size payload attempts created t_event quality".split()
        return dict(zip(keys, r))

    def done(self, item_id, t_ack):
        with self.lock:
            self.db.execute("UPDATE items SET state='done', acked=? WHERE id=?", (t_ack, item_id))

    def retry(self, item_id, delay):
        with self.lock:
            self.db.execute("UPDATE items SET attempts=attempts+1, next_try=? WHERE id=?",
                            (time.time() + delay, item_id))

    def wake_all(self):
        with self.lock:
            self.db.execute("UPDATE items SET next_try=0 WHERE state='pending'")

    def backlog(self):
        with self.lock:
            n, b = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0) FROM items WHERE state='pending'").fetchone()
        return n, b

    def pending_by_kind(self):
        with self.lock:
            return dict(self.db.execute(
                "SELECT kind, COUNT(*) FROM items WHERE state='pending' GROUP BY kind").fetchall())

    def purge_done_files(self):
        with self.lock:
            rows = self.db.execute("SELECT id,path FROM items WHERE state='done' AND path IS NOT NULL").fetchall()
            for i, p in rows:
                try:
                    os.remove(p)
                except OSError:
                    pass
                self.db.execute("UPDATE items SET path=NULL WHERE id=?", (i,))

    def evict(self, quota_bytes):
        """Si la file dépasse le quota : supprime d'abord les extraits de plus faible score.
        Les métadonnées et les vidéos à traiter dans le Cloud ne sont jamais supprimées (ce serait
        une perte de résultat). Retourne (nb_supprimés, octets)."""
        n_b = 0
        dropped = 0
        while True:
            _, b = self.backlog()
            if b <= quota_bytes:
                return dropped, n_b
            with self.lock:
                r = self.db.execute(
                    "SELECT id,path,size FROM items WHERE state='pending' "
                    "AND kind NOT IN ('status','result','video') "
                    "AND priority>=1 ORDER BY score ASC, id ASC LIMIT 1").fetchone()
                if not r:
                    return dropped, n_b
                self.db.execute("UPDATE items SET state='dropped', path=NULL WHERE id=?", (r[0],))
            try:
                if r[1]:
                    os.remove(r[1])
            except OSError:
                pass
            dropped += 1
            n_b += r[2]

    def close(self):
        self.db.close()
