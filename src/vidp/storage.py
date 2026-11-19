"""Stockage Cloud : métadonnées (DynamoDB ou SQLite) + fichiers (S3 ou disque local).

STORAGE=aws   : Amazon S3 (vidéos source, extraits, vignettes, sous-titres) + DynamoDB (métadonnées),
                identifiants fournis par le rôle IAM de l'instance (aucune clé sur la VM).
                S3_ENDPOINT / DDB_ENDPOINT permettent de tester avec MinIO / DynamoDB Local.
STORAGE=local : « BD simple pour les métadonnées » (énoncé) : SQLite + disque — pour travailler sans AWS.

Idempotence : un résultat n'est publié qu'une fois (écriture conditionnelle) ; un statut « en cours »
n'écrase jamais un résultat publié. Un renvoi après coupure est donc sans effet (0 doublon).
"""
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager

DONE = "done"


# ====================================================================== métadonnées
class SqliteMeta:
    SCHEMA = """CREATE TABLE IF NOT EXISTS videos(
      video_id TEXT PRIMARY KEY, run_id TEXT, t0 REAL, state TEXT, published_ts REAL, updated REAL,
      doc TEXT DEFAULT '{}', media TEXT DEFAULT '{}');
      CREATE INDEX IF NOT EXISTS ix_run ON videos(run_id, t0);"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(self.SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            yield c
        finally:
            c.close()

    def upsert_status(self, doc) -> bool:
        with self.lock, self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT state, doc FROM videos WHERE video_id=?", (doc["video_id"],)).fetchone()
            if r and r[0] == DONE:
                c.execute("COMMIT")
                return False
            merged = {**(json.loads(r[1]) if r else {}), **doc}
            c.execute("INSERT INTO videos(video_id,run_id,t0,state,updated,doc) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(video_id) DO UPDATE SET state=excluded.state, updated=excluded.updated, "
                      "doc=excluded.doc", (doc["video_id"], doc.get("run_id"), doc.get("t0"), doc["state"],
                                           time.time(), json.dumps(merged)))
            c.execute("COMMIT")
            return r is None

    def put_result(self, doc) -> bool:
        now = time.time()
        with self.lock, self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT state, doc FROM videos WHERE video_id=?", (doc["video_id"],)).fetchone()
            if r and r[0] == DONE:
                c.execute("COMMIT")
                return False
            merged = {**(json.loads(r[1]) if r else {}), **doc, "state": DONE}
            c.execute("INSERT INTO videos(video_id,run_id,t0,state,published_ts,updated,doc) VALUES(?,?,?,?,?,?,?) "
                      "ON CONFLICT(video_id) DO UPDATE SET state=excluded.state, published_ts=excluded.published_ts,"
                      " updated=excluded.updated, doc=excluded.doc",
                      (doc["video_id"], doc.get("run_id"), doc.get("t0"), DONE, now, now, json.dumps(merged)))
            c.execute("COMMIT")
            return True

    def add_media(self, video_id, kind, key, nbytes):
        with self.lock, self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT media FROM videos WHERE video_id=?", (video_id,)).fetchone()
            m = json.loads(r[0]) if r else {}
            m[kind] = {"key": key, "bytes": nbytes}
            c.execute("INSERT INTO videos(video_id,state,updated,media) VALUES(?,?,?,?) ON CONFLICT(video_id) "
                      "DO UPDATE SET media=excluded.media, updated=excluded.updated",
                      (video_id, "receiving", time.time(), json.dumps(m)))
            c.execute("COMMIT")

    def _out(self, row):
        vid, state, pub, upd, doc, media = row
        d = json.loads(doc or "{}")
        d.update(video_id=vid, state=state, published_ts=pub, updated=upd, media=json.loads(media or "{}"))
        return d

    def get(self, video_id):
        with self.conn() as c:
            r = c.execute("SELECT video_id,state,published_ts,updated,doc,media FROM videos WHERE video_id=?",
                          (video_id,)).fetchone()
        return self._out(r) if r else None

    def list(self, run_id=None, limit=100):
        q = "SELECT video_id,state,published_ts,updated,doc,media FROM videos "
        a = []
        if run_id:
            q += "WHERE run_id=? "
            a.append(run_id)
        q += "ORDER BY COALESCE(t0, updated) DESC LIMIT ?"
        a.append(limit)
        with self.conn() as c:
            return [self._out(r) for r in c.execute(q, a).fetchall()]


class DynamoMeta:
    """Table DynamoDB (clé : video_id). Les médias sont des attributs « m_<type> »."""

    def __init__(self, table, region, endpoint=""):
        import boto3
        self.t = boto3.resource("dynamodb", region_name=region, endpoint_url=endpoint or None).Table(table)
        from boto3.dynamodb.conditions import Attr
        self.Attr = Attr

    @staticmethod
    def _enc(doc):
        # DynamoDB refuse les float : on stocke le document en JSON (lecture simple et fidèle)
        return json.dumps(doc, ensure_ascii=False)

    def upsert_status(self, doc) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.t.update_item(
                Key={"video_id": doc["video_id"]},
                UpdateExpression="SET #s=:s, run_id=:r, t0=:t, #d=if_not_exists(#d, :d), updated=:u",
                ConditionExpression="attribute_not_exists(video_id) OR #s <> :done",
                ExpressionAttributeNames={"#s": "state", "#d": "doc"},
                ExpressionAttributeValues={":s": doc["state"], ":r": doc.get("run_id") or "-",
                                           ":t": str(doc.get("t0")), ":d": self._enc(doc), ":u": str(time.time()),
                                           ":done": DONE})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def put_result(self, doc) -> bool:
        from botocore.exceptions import ClientError
        now = str(time.time())
        try:
            self.t.update_item(
                Key={"video_id": doc["video_id"]},
                UpdateExpression="SET #s=:done, run_id=:r, t0=:t, #d=:d, published_ts=:p, updated=:p",
                ConditionExpression="attribute_not_exists(video_id) OR #s <> :done",
                ExpressionAttributeNames={"#s": "state", "#d": "doc"},
                ExpressionAttributeValues={":done": DONE, ":r": doc.get("run_id") or "-", ":t": str(doc.get("t0")),
                                           ":d": self._enc({**doc, "state": DONE}), ":p": now})
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def add_media(self, video_id, kind, key, nbytes):
        self.t.update_item(Key={"video_id": video_id}, UpdateExpression="SET #m=:m, updated=:u",
                           ExpressionAttributeNames={"#m": f"m_{kind}"},
                           ExpressionAttributeValues={":m": json.dumps({"key": key, "bytes": nbytes}),
                                                      ":u": str(time.time())})

    def _out(self, it):
        d = json.loads(it.get("doc") or "{}")
        d.update(video_id=it["video_id"], state=it.get("state"),
                 published_ts=float(it["published_ts"]) if it.get("published_ts") else None,
                 updated=float(it.get("updated") or 0),
                 media={k[2:]: json.loads(v) for k, v in it.items() if k.startswith("m_")})
        return d

    def get(self, video_id):
        it = self.t.get_item(Key={"video_id": video_id}).get("Item")
        return self._out(it) if it else None

    def list(self, run_id=None, limit=100):
        kw = {"FilterExpression": self.Attr("run_id").eq(run_id)} if run_id else {}
        items, start = [], None
        while True:              # Scan paginé : adapté au volume du projet (quelques milliers d'items)
            r = self.t.scan(**kw, **({"ExclusiveStartKey": start} if start else {}))
            items += r.get("Items", [])
            start = r.get("LastEvaluatedKey")
            if not start:
                break
        out = [self._out(i) for i in items]
        out.sort(key=lambda d: d.get("t0") or d.get("updated") or 0, reverse=True)
        return out[:limit]


# ====================================================================== fichiers
class LocalBlobs:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _p(self, key):
        p = os.path.normpath(os.path.join(self.root, key))
        if not p.startswith(os.path.normpath(self.root) + os.sep):
            raise ValueError("clé invalide")
        return p

    def put_file(self, key, path, content_type):
        dst = self._p(key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(path, dst + ".tmp")
        os.replace(dst + ".tmp", dst)

    def open(self, key):
        """-> (itérateur d'octets, taille) ou None."""
        p = self._p(key)
        if not os.path.exists(p):
            return None

        def it():
            with open(p, "rb") as f:
                for blk in iter(lambda: f.read(1 << 16), b""):
                    yield blk
        return it(), os.path.getsize(p)


class S3Blobs:
    def __init__(self, bucket, region, endpoint=""):
        import boto3
        self.b = bucket
        self.s3 = boto3.client("s3", region_name=region, endpoint_url=endpoint or None)

    def put_file(self, key, path, content_type):
        self.s3.upload_file(path, self.b, key, ExtraArgs={"ContentType": content_type})

    def open(self, key):
        from botocore.exceptions import ClientError
        try:
            o = self.s3.get_object(Bucket=self.b, Key=key)
        except ClientError:
            return None
        return o["Body"].iter_chunks(1 << 16), o["ContentLength"]


def ensure_emulated(cfg):
    """Émulateurs (MinIO / DynamoDB Local) : créer bucket et table s'ils manquent.
    Sur AWS, c'est Terraform qui les crée — le code applicatif n'a pas ce droit (moindre privilège)."""
    import boto3
    if cfg.s3_endpoint:
        s3 = boto3.client("s3", region_name=cfg.aws_region, endpoint_url=cfg.s3_endpoint)
        if cfg.s3_bucket not in [b["Name"] for b in s3.list_buckets().get("Buckets", [])]:
            s3.create_bucket(Bucket=cfg.s3_bucket, CreateBucketConfiguration={"LocationConstraint": cfg.aws_region})
    if cfg.ddb_endpoint:
        ddb = boto3.client("dynamodb", region_name=cfg.aws_region, endpoint_url=cfg.ddb_endpoint)
        if cfg.ddb_table not in ddb.list_tables().get("TableNames", []):
            ddb.create_table(TableName=cfg.ddb_table, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "video_id", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "video_id", "AttributeType": "S"}])


def make_storage(cfg):
    if cfg.storage == "aws":
        if not (cfg.s3_bucket and cfg.ddb_table):
            raise SystemExit("STORAGE=aws exige S3_BUCKET et DDB_TABLE")
        if cfg.s3_endpoint or cfg.ddb_endpoint:
            ensure_emulated(cfg)
        return DynamoMeta(cfg.ddb_table, cfg.aws_region, cfg.ddb_endpoint), \
            S3Blobs(cfg.s3_bucket, cfg.aws_region, cfg.s3_endpoint)
    os.makedirs(cfg.data_dir, exist_ok=True)
    return SqliteMeta(os.path.join(cfg.data_dir, "videos.db")), LocalBlobs(os.path.join(cfg.data_dir, "blobs"))
