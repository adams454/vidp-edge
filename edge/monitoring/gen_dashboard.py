"""Génère le tableau de bord Grafana (JSON) : python3 gen_dashboard.py > grafana/dashboards/vidp.json"""
import json

pid = [0]


def panel(title, exprs, unit="short", x=0, y=0, w=12, h=8, ds="edge"):
    pid[0] += 1
    return {"id": pid[0], "type": "timeseries", "title": title, "datasource": {"type": "prometheus", "uid": ds},
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8}}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": lg, "datasource": {"type": "prometheus", "uid": ds}}
                        for i, (e, lg) in enumerate(exprs)]}


q = lambda p: f'histogram_quantile({p}, sum by (le) (rate(cloud_publish_latency_seconds_bucket[2m])))'
panels = [
    panel("Latence arrivée → résultat publié (Cloud)", [(q(0.5), "p50"), (q(0.95), "p95"), (q(0.99), "p99")], "s", 0, 0, ds="cloud"),
    panel("WAN — débit sortant mesuré au noyau (bit/s)", [("rate(wan_if_tx_bytes_total[15s])*8", "tx"), ("rate(wan_if_rx_bytes_total[15s])*8", "rx")], "bps", 12, 0),
    panel("Tampon Edge (éléments / octets)", [("uploader_queue_items", "éléments"), ("uploader_queue_bytes", "octets")], "short", 0, 8),
    panel("Lien WAN (1 = joignable) et débit utile estimé", [("uploader_wan_up", "wan_up"), ("uploader_bw_ewma_bytes_per_s*8", "débit EWMA (bit/s)")], "short", 12, 8),
    panel("File par étage (vidéos en attente / en cours)", [("max by (stage) (vidp_queue_videos)", "{{stage}}")], "short", 0, 16),
    panel("Durée par étage (p95, s)", [("histogram_quantile(0.95, sum by (le, stage) (rate(vidp_stage_seconds_bucket[5m])))", "{{stage}}")], "s", 12, 16),
    panel("Placement (hybride) : vidéos par minute", [("sum by (route) (rate(vidp_route_total[2m])*60)", "{{route}}"),
                                                     ("vidp_policy_eta_seconds", "ETA {{where}} (s)")], "short", 0, 24),
    panel("Qualité des extraits envoyés (5 min)", [("sum by (quality) (increase(vidp_extract_quality_total[5m]))", "{{quality}}")], "short", 12, 24),
    panel("CPU par conteneur d'étage (% d'un cœur)", [("vidp_stage_cpu_percent", "{{stage}}")], "short", 0, 32),
    panel("Mémoire — working set conteneurs", [('container_memory_working_set_bytes{name=~"vidp-.*"}', "{{name}}")], "bytes", 12, 32),
    panel("Puissance hôte (W) et énergie (J/min)", [("host_power_watts", "W {{source}}"), ("rate(host_energy_joules_total[1m])*60", "J/min")], "short", 0, 40),
    panel("Cloud — octets reçus (bit/s) et file Video Production", [("sum by (kind) (rate(cloud_ingest_bytes_total[30s])*8)", "{{kind}}"),
                                                                   ("cloud_queue_videos", "file Cloud")], "short", 12, 40, ds="cloud"),
    panel("Cloud — doublons ignorés, authentifications refusées", [("increase(cloud_duplicates_total[5m])", "doublons {{kind}}"),
                                                                  ("increase(cloud_auth_failures_total[5m])", "auth KO")], "short", 0, 48, ds="cloud"),
    panel("Cloud — durée des étages (p95, s)", [("histogram_quantile(0.95, sum by (le, stage) (rate(cloud_stage_seconds_bucket[5m])))", "{{stage}}")], "s", 12, 48, ds="cloud"),
]
print(json.dumps({"uid": "vidp-edge", "title": "VidP — pipeline, WAN, SLO, énergie", "schemaVersion": 39, "version": 2,
                  "refresh": "5s", "time": {"from": "now-30m", "to": "now"}, "panels": panels, "tags": ["vidp"]},
                 indent=1, ensure_ascii=False))
