> **Essai de développement** (19/11/2026, PC Windows, sans Docker) : `scripts/dev_smoke.py`, 60 s de charge à 8 vidéos/min, coupure = arrêt brutal de l'API Cloud pendant 21 s. Edge et Cloud sur la même machine : la latence et le CPU ne représentent pas le vrai déploiement, et ni tc ni l'énergie ne sont mesurés. Les vraies mesures se font avec `scripts/run_scenarios.py` sur l'Edge.

# Résultats agrégés (généré par analyze.py)

## Latence et résilience

| Mode | Condition | n | p50 (s) | p95 (s) [IC95] | p99 (s) [IC95] | Perdues | Doublons | Backlog max | Reprise (s) | % déporté |
|---|---|---|---|---|---|---|---|---|---|---|
| cloud_only | blackout | 9 | 53.01 | 69.35 [61.43–69.48] | 69.45 [66.01–69.48] | 0 | 0 | 8 | 5.8 | 100 |
| cloud_only | nominal | 9 | 19.21 | 28.30 [19.54–29.66] | 29.38 [25.40–29.66] | 0 | 0 | 1 | — | 100 |
| edge_first | blackout | 9 | 26.22 | 40.57 [27.86–40.90] | 40.84 [28.73–40.90] | 0 | 0 | 16 | 3.2 | 0 |
| edge_first | nominal | 9 | 22.47 | 24.29 [22.63–25.08] | 24.92 [22.71–25.08] | 0 | 0 | 4 | — | 0 |
| hybrid | blackout | 9 | 22.80 | 40.68 [26.24–40.98] | 40.92 [26.24–40.98] | 0 | 0 | 16 | 3.1 | 11 |
| hybrid | nominal | 9 | 18.31 | 22.81 [19.67–23.64] | 23.47 [19.68–23.64] | 0 | 0 | 3 | — | 33 |

## Réseau, ressources, énergie, qualité

| Mode | Condition | WAN (Mo) | Évité vs source (%) | Évité vs cloud_only (%) | CPU (% cœur) | WSS (Mo) | J nets | J / résultat utile | Langue | Précision | Rappel | WER |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cloud_only | blackout | 13.7 ± — | -0.2 | -0.0 | 1 | 194 | — | — (n/a) | 1.00 | 1.00 | 1.00 | 0.02 |
| cloud_only | nominal | 13.7 ± — | -0.2 | 0.0 | 1 | 195 | — | — (n/a) | 1.00 | 1.00 | 1.00 | 0.02 |
| edge_first | blackout | 0.8 ± — | 93.9 | 93.9 | 179 | 711 | — | — (n/a) | 1.00 | 1.00 | 1.00 | 0.02 |
| edge_first | nominal | 0.8 ± — | 93.9 | 93.9 | 173 | 713 | — | — (n/a) | 1.00 | 1.00 | 1.00 | 0.02 |
| hybrid | blackout | 1.2 ± — | 91.6 | 91.6 | 169 | 714 | — | — (n/a) | 1.00 | 1.00 | 1.00 | 0.02 |
| hybrid | nominal | 1.4 ± — | 89.5 | 89.5 | 126 | 709 | — | — (n/a) | 1.00 | 1.00 | 1.00 | 0.02 |

IC : bootstrap 95 % sur les vidéos poolées (percentiles) ; t de Student sur les répétitions (moyennes ±). Énergie `model` = estimation (pas de RAPL) : indicative seulement.
