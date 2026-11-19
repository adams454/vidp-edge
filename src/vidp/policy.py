"""Politique de placement et de transfert.

1) OÙ traiter une vidéo (après Downscale) — mode hybride seulement :
     ETA_local  = travail en attente sur l'Edge × coût de l'étage goulot + durée × coût total
     ETA_cloud  = (backlog d'envoi + taille de la vidéo réduite) / débit WAN mesuré + RTT
                  + (file du Cloud + durée) × coût Cloud
   On déporte seulement si l'Edge est encombré (une file d'attente existe), le WAN joignable, le débit
   connu, et ETA_cloud < ETA_local × (1 − marge) : c'est le « point de bascule » Edge/Cloud.
   Les coûts sont appris en ligne (secondes de calcul par seconde de vidéo) ; ceux du Cloud sont
   publiés par le Cloud lui-même (/healthz), lus par la sonde du téléverseur.
   cloud_only : toujours le Cloud (vidéo source complète) ; edge_first : toujours l'Edge.

2) QUOI envoyer après un traitement local — extraits dont l'ETA d'envoi tient dans le budget :
     full (360p) > low (240p) > thumb (vignette seule). Lien coupé : on garde la meilleure qualité
     tant que le spool le permet (le quota local évince d'abord les extraits de plus faible intérêt).
"""
from . import stages

LOCAL_STAGES = ["langident", "subtitle", "animal"]
PRIOR_S_PER_S = 0.35         # coût local initial (s de calcul / s de vidéo), corrigé dès les 1ers résultats
PRIOR_BPS = {"full": 45_000, "low": 15_000}   # octets par seconde d'extrait, corrigé en ligne


class Policy:
    def __init__(self, mode: str, t_budget_s=20.0, margin=0.2):
        self.mode = mode
        self.t_budget = t_budget_s
        self.margin = margin
        self.bps = dict(PRIOR_BPS)
        self.last = {}

    # --------------------------------------------------------------- coûts appris
    @staticmethod
    def local_costs(recent):
        """(coût total, coût de l'étage goulot) en s par s de vidéo, médiane des derniers travaux."""
        tot, bott = [], []
        for dur, t in recent:
            per = [t.get(s, 0) / dur for s in LOCAL_STAGES]
            tot.append(sum(per))
            bott.append(max(per))
        if not tot:
            return PRIOR_S_PER_S, PRIOR_S_PER_S * 0.6
        tot.sort(), bott.sort()
        return tot[len(tot) // 2], bott[len(bott) // 2]

    # --------------------------------------------------------------- 1. placement
    def route(self, duration, nbytes, local_pending_s, upload_backlog_b, wan: dict, recent) -> tuple:
        if self.mode == "cloud_only":
            return "cloud", {"why": "baseline"}
        if self.mode == "edge_first":
            return "edge", {"why": "edge_first"}
        s_tot, s_bot = self.local_costs(recent)
        eta_local = local_pending_s * s_bot + duration * s_tot
        info = {"eta_local": round(eta_local, 2), "s_local": round(s_tot, 3)}
        bw = float(wan.get("bw") or 0)
        if local_pending_s * s_bot <= self.margin * duration * s_tot:
            # Edge non encombré : on reste local (aucun octet WAN de plus, résultat aussi rapide)
            self.last = {**info, "why": "edge_free"}
            return "edge", self.last
        if not wan.get("up") or bw <= 0:
            self.last = {**info, "why": "wan_down" if not wan.get("up") else "bw_unknown"}
            return "edge", self.last
        c = float(wan.get("cloud_s_per_s") or s_tot)
        eta_cloud = (upload_backlog_b + nbytes) / bw + float(wan.get("rtt") or 0) + \
            (float(wan.get("cloud_queue_s") or 0) + duration) * c
        info.update(eta_cloud=round(eta_cloud, 2), bw=round(bw))
        where = "cloud" if eta_cloud < eta_local * (1 - self.margin) else "edge"
        self.last = {**info, "why": "eta"}
        return where, self.last

    # --------------------------------------------------------------- 2. extraits
    def observe_clip(self, quality, nbytes, seconds):
        if quality in self.bps and seconds > 0:
            self.bps[quality] = 0.7 * self.bps[quality] + 0.3 * (nbytes / seconds)

    def choose_quality(self, wan: dict, backlog_bytes: int, clip_seconds: float) -> str:
        if self.mode != "hybrid":
            return "full"
        if not wan.get("up", True):
            return "full"        # rien ne part pendant la coupure : on garde la qualité, le quota protège le disque
        bw = max(float(wan.get("bw") or 0), 20_000.0)
        for q in stages.ORDER:
            size = self.bps.get(q, 0) * clip_seconds
            if (backlog_bytes + size) / bw <= self.t_budget:
                return q
        return "thumb"
