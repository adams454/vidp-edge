"""File de travaux, tampon Edge (outbox) et politique — sans modèle ni réseau."""
from vidp.jobs import JobQueue
from vidp.outbox import Outbox
from vidp.policy import Policy
from vidp.stages import merge_windows


def test_jobs_flow_and_crash_recovery(tmp_path):
    q = JobQueue(str(tmp_path / "jobs.db"))
    assert q.add("v1", "downscale", "r", "a.mp4", "/x/a.mp4", t0=1.0)
    assert not q.add("v1", "downscale", "r", "a.mp4", "/x/a.mp4", t0=1.0)      # même vidéo : ignorée
    q.add("v2", "downscale", t0=2.0)
    j = q.claim("downscale")
    assert j["video_id"] == "v1" and q.claim("downscale")["video_id"] == "v2"
    assert q.claim("downscale") is None
    q.requeue_running("downscale")                    # redémarrage du conteneur : rien n'est perdu
    assert q.claim("downscale")["video_id"] == "v1"
    q.advance("v1", "langident", {"duration": 10.0, "has_audio": True}, ("downscale", 1.5), duration=10.0)
    j = q.claim("langident")
    assert j["data"]["has_audio"] and j["timings"]["downscale"] == 1.5
    assert q.pending(["langident"]) == (1, 10.0)
    for _ in range(3):
        q.fail("v1", "boom")
        q.claim("langident")
    assert q.get("v1")["state"] == "failed"


def test_outbox_priorities_and_quota(tmp_path):
    ob = Outbox(str(tmp_path))
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 5000)
    ob.put("v1", "clip0", 1, "{}", path=str(f), size=5000, score=1.0)
    ob.put("v1", "result", 0, "{}")
    ob.put("v2", "video", 1, "{}", path="/nope", size=9000, score=1.0)
    assert ob.next_ready()["kind"] == "result"         # métadonnées d'abord (reprise contrôlée)
    dropped, nb = ob.evict(4000)
    assert dropped == 1 and nb == 5000 and not f.exists()   # l'extrait part, jamais la vidéo à traiter
    assert ob.pending_by_kind() == {"result": 1, "video": 1}


def test_policy_placement():
    p = Policy("hybrid", margin=0.2)
    recent = [(10.0, {"langident": 1.0, "subtitle": 2.0, "animal": 1.0})] * 5   # 0,4 s de calcul / s de vidéo
    up = {"up": True, "bw": 1_000_000, "rtt": 0.05, "cloud_queue_s": 0, "cloud_s_per_s": 0.2}
    assert p.route(10, 200_000, 0, 0, up, recent)[0] == "edge"           # Edge libre : local
    assert p.route(10, 200_000, 300, 0, up, recent)[0] == "cloud"        # Edge saturé, WAN rapide : Cloud
    assert p.route(10, 200_000, 300, 0, {**up, "up": False}, recent)[0] == "edge"   # WAN coupé
    assert p.route(10, 200_000, 300, 0, {**up, "bw": 2_000}, recent)[0] == "edge"   # WAN trop lent
    assert Policy("cloud_only").route(10, 1, 0, 0, up, [])[0] == "cloud"
    assert Policy("edge_first").route(10, 1, 999, 0, up, [])[0] == "edge"


def test_policy_quality_adapts_to_wan():
    p = Policy("hybrid", t_budget_s=10)
    assert p.choose_quality({"up": True, "bw": 1_000_000}, 0, 8) == "full"
    assert p.choose_quality({"up": True, "bw": 30_000}, 0, 8) == "low"
    assert p.choose_quality({"up": True, "bw": 20_000}, 5_000_000, 8) == "thumb"
    assert Policy("edge_first").choose_quality({"up": True, "bw": 1}, 9e9, 8) == "full"


def test_merge_windows():
    an = [{"t0": 1.0, "t1": 3.0}, {"t0": 3.2, "t1": 5.0}, {"t0": 9.0, "t1": 10.0}]
    assert merge_windows(an, 12, pad=0.5) == [[0.5, 5.5], [8.5, 10.5]]
