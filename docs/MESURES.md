# Protocole de mesure

Toutes les valeurs du rapport sont recalculées par `scripts/analyze.py` à partir des **données brutes** de
`edge/results/<run>/` (à joindre au dépôt). Une fonctionnalité non mesurée ne prouve rien : chaque affirmation
du rapport doit renvoyer à une ligne de `results/summary.md`.

**Charge** : bibliothèque de 8 vidéos de test 1280×720 @25 i/s, 20 s, générées avec graine fixe (`make videos`) :
6 contiennent un animal (chien, chevaux ou aigle) qui traverse l'écran à des instants connus, 2 n'en contiennent
pas (mesure des faux positifs) ; 6 contiennent une phrase parlée (fr, en, es, de) par synthèse vocale, 2 sont
sans voix. Vérité terrain : `vidNN.gt.json`. Le générateur de charge dépose `FEED_RATE` vidéos/min (défaut 4)
selon un processus de Poisson à graine fixe pendant 300 s.

**Conditions** : nominal · 10 Mbit/s · 5 Mbit/s · 100 ms + 1 % de pertes · coupure WAN de 120 s à t = 60 s
(`tc netem`/`tbf` dans l'espace de noms réseau du téléverseur). 3 répétitions (`--preset full`).

**Latence (événement → résultat)** : `published_ts − t0`, où `t0` = instant d'arrivée de la vidéo dans le magasin
local de l'Edge (mtime du dépôt atomique) et `published_ts` = instant où le Cloud enregistre le résultat complet
(il devient visible sur la page publique). Deux horloges : Edge et Cloud synchronisées par NTP (chrony) — l'écart
(quelques ms) est négligeable devant les latences mesurées (secondes). Percentiles p50/p95/p99,
IC 95 % par bootstrap (1000 rééchantillonnages) sur les vidéos poolées des répétitions.

**Octets WAN** : delta du compteur TX de l'interface `eth0` du conteneur téléverseur (sous tc, retransmissions
incluses). **Volume évité** = 1 − octets WAN / octets des vidéos source déposées (et, en second, par rapport à la
référence `cloud_only` nominal).

**Résilience** : *perdues* = vidéos déposées sans résultat publié ; *doublons* = vidéos publiées plus d'une fois
(objectif 0 et 0) ; *backlog* = éléments en attente dans le tampon Edge (1 Hz) ; *temps de reprise* = délai après
le rétablissement pour revenir à (médiane avant coupure + 2) éléments, stable 5 s.

**Ressources** : CPU (processus + enfants ffmpeg) et working set (`memory.current − inactive_file`, cgroup v2)
de chacun des 4 conteneurs d'étage, échantillonnés à 1 Hz (`res_<étage>.csv`) ; on rapporte la somme.

**Énergie** : RAPL (paquet CPU) ; énergie nette = E_totale − P_repos × durée (P_repos mesurée 60 s, pipeline
arrêté) ; **J par résultat utile** = E_nette / résultats utiles. Sans RAPL (fréquent en VM), un modèle linéaire
CPU étiqueté `model` est utilisé : il n'est qu'indicatif et doit être signalé comme tel.

**Qualité** : *résultat utile* = vidéo publiée dont la langue est correcte ET dont l'ensemble des animaux
détectés est exactement celui de la vérité terrain. Animaux : un intervalle détecté est correct s'il a le bon
label et chevauche (±1 s) l'intervalle réel → précision, rappel. Langue : exactitude. Sous-titres : WER moyen
contre le texte prononcé.

**Bonus énergie** : revendiqué seulement si p99, pertes (0) et qualité (précision, rappel, langue) ne sont pas
dégradés par rapport à `edge_first`.

**Menaces à la validité** : vidéos de synthèse (photos incrustées, voix de synthèse) plus simples que des scènes
réelles ; une seule machine Edge ; `tbf` ne reproduit pas la gigue d'un vrai WAN ; Prometheus/Grafana tournent
sur l'Edge et comptent dans l'énergie brute (retirés par l'énergie au repos) ; le modèle d'énergie sans RAPL.
