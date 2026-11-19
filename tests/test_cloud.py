"""API Cloud : identité de nœud, idempotence, téléversement avec reprise — stockage local ET S3/DynamoDB (moto)."""
import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from vidp.cloud import create_app
from vidp.common import node_token
from vidp.config import CloudCfg

H = {"X-Node-Id": "edge-01", "X-Node-Token": node_token("m", "edge-01"), "X-Run-Id": "r"}
ST = {"video_id": "ab12", "node": "edge-01", "run_id": "r", "mode": "hybrid", "name": "a.mp4", "t0": 1.0,
      "state": "edge_processing"}
RES = {**ST, "processed_at": "edge", "lang": "fr", "animals": [{"label": "dog", "t0": 1, "t1": 3, "conf": .9}],
       "subtitles": [{"start": 0, "end": 1, "text": "bonjour"}]}


@pytest.fixture(params=["local", "aws"])
def client(request, tmp_path, monkeypatch):
    cfg = CloudCfg(data_dir=str(tmp_path), master_secret="m", storage=request.param,
                   s3_bucket="vidp-test", ddb_table="vidp-test", aws_region="eu-west-3")
    if request.param == "aws":
        moto = pytest.importorskip("moto")
        import boto3
        for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "eu-west-3"}.items():
            monkeypatch.setenv(k, v)
        m = moto.mock_aws()
        m.start()
        boto3.client("s3", region_name="eu-west-3").create_bucket(
            Bucket="vidp-test", CreateBucketConfiguration={"LocationConstraint": "eu-west-3"})
        boto3.client("dynamodb", region_name="eu-west-3").create_table(
            TableName="vidp-test", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "video_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "video_id", "AttributeType": "S"}])
        request.addfinalizer(m.stop)
    return TestClient(create_app("all", cfg))


def test_auth_required(client):
    u = "/v1/videos/ab12/status"
    assert client.post(u, json=ST).status_code == 401
    assert client.post(u, json=ST, headers={**H, "X-Node-Token": "x"}).status_code == 401
    assert client.post(u, json={**ST, "node": "edge-99"}, headers=H).status_code == 403   # usurpation


def test_status_then_result_idempotent(client):
    assert client.post("/v1/videos/ab12/status", json=ST, headers=H).status_code == 200
    v = client.get("/api/videos").json()
    assert len(v) == 1 and v[0]["state"] == "edge_processing"
    assert client.post("/v1/videos/ab12/result", json=RES, headers=H).json()["duplicate"] is False
    assert client.post("/v1/videos/ab12/result", json=RES, headers=H).json()["duplicate"] is True
    # un statut en retard (renvoi après coupure) ne fait pas régresser un résultat publié
    client.post("/v1/videos/ab12/status", json=ST, headers=H)
    v = client.get("/api/videos?run_id=r").json()
    assert len(v) == 1 and v[0]["state"] == "done" and v[0]["lang"] == "fr" and v[0]["published_ts"]
    s = client.get("/api/stats").json()
    assert s["by_state"] == {"done": 1} and s["animals"] == {"dog": 1}


def test_resumable_upload_integrity_and_media(client):
    data = b"A" * 1000 + b"B" * 500
    u = "/v1/uploads/ab12_thumb"
    assert client.get(u, headers=H).json() == {"committed": False, "offset": 0}
    assert client.patch(u, params={"offset": 0}, content=data[:1000], headers=H).json()["offset"] == 1000
    r = client.patch(u, params={"offset": 0}, content=data[:1000], headers=H)   # reprise : mauvais offset
    assert r.status_code == 409 and r.json()["offset"] == 1000
    client.patch(u, params={"offset": 1000}, content=data[1000:], headers=H)
    bad = client.post(u + "/commit", params={"size": 1500, "sha256": "0" * 64}, content=b"{}", headers=H)
    assert bad.status_code == 422                                               # empreinte invalide
    client.patch(u, params={"offset": 0}, content=data, headers=H)
    sha = hashlib.sha256(data).hexdigest()
    assert client.post(u + "/commit", params={"size": 1500, "sha256": sha}, content=b"{}", headers=H).status_code == 200
    again = client.post(u + "/commit", params={"size": 1500, "sha256": sha}, content=b"{}", headers=H)
    assert again.json()["duplicate"] is True
    client.post("/v1/videos/ab12/result", json=RES, headers=H)
    v = client.get("/api/videos/ab12").json()
    assert v["media"]["thumb"]["bytes"] == 1500
    assert client.get("/media/" + v["media"]["thumb"]["key"]).content == data
    assert client.get("/media/sources/ab12.mp4").status_code == 404           # sources jamais publiques


def test_public_role_has_no_ingest(tmp_path):
    c = TestClient(create_app("public", CloudCfg(data_dir=str(tmp_path), master_secret="m")))
    assert c.post("/v1/videos/ab12/status", json=ST, headers=H).status_code in (404, 405)
    assert c.get("/").status_code == 200
