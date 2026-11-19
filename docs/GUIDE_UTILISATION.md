# Guide d'utilisation — VidP-Edge

Dans l'ordre : **tester** sur un PC, **déployer** (avec ou sans AWS), **mesurer**, **présenter**.
La section 1 se fait sur n'importe quel PC. Les sections 2 à 6 demandent des machines Linux (Edge, Cloud).

---
## 0. Comprendre en 1 minute
- **Edge** : une vidéo déposée dans le magasin local traverse 4 conteneurs en série — **Downscale** (réduction 360p),
  **LangIdent** (langue parlée), **Subtitle** (sous-titres), **Animal detect** (animaux et instants). Seuls les
  métadonnées et les extraits utiles sont envoyés ; un tampon disque garde tout pendant une coupure du WAN.
- **Cloud** : reçoit, range dans **S3 + DynamoDB** (ou SQLite + disque sans AWS), traite les vidéos déportées
  (worker « Video Production ») et publie sur une **page web Apache2** (vidéos traitées + en cours).
- **3 modes** : `cloud_only` (tout part au Cloud : référence), `edge_first` (tout sur l'Edge), `hybrid` (l'Edge
  traite tant qu'il suit ; s'il sature et que le WAN le permet, il déporte au Cloud).

## 1. Test sur votre PC (sans Docker, sans AWS)
Prérequis : Python 3.10+, `ffmpeg` dans le PATH, `espeak-ng` pour les voix (sinon vidéos sans voix).
```bash
cd vidp-edge
pip install -r requirements-dev.txt
make models          # télécharge Whisper tiny + MobileNet-SSD + 3 photos, vérifie les SHA-256
make videos          # 8 vidéos de test (20 s) + vérité terrain data/videos/vidNN.gt.json  (~4 min)
make test            # tous les tests, dont bout en bout avec coupure du Cloud (~10 min)
make smoke           # 3 modes × (nominal, coupure) en processus locaux
python3 scripts/analyze.py --results /tmp/vidp_smoke/results --out /tmp/vidp_smoke/analysis
```
La coupure est simulée en arrêtant brutalement l'API du Cloud. Ni octets WAN noyau, ni énergie, ni débit limité :
ces mesures-là demandent la section 4.

## 2. Déploiement complet
### 2.1 Poste de contrôle
`ansible`, `python3` (avec `pyyaml` et `cryptography`, installés avec Ansible), une clé SSH (`~/.ssh/id_ed25519`),
`terraform` seulement pour AWS. Les machines Edge/Cloud : Debian/Ubuntu joignables en SSH avec sudo.
Docker, les images, les modèles, les vidéos de test et les secrets sont installés automatiquement.

### 2.2 Variables (le jour J, avec les adresses données par l'enseignant)
```bash
export EDGE_HOSTS="IP_EDGE1,IP_EDGE2"     # ou "local" pour cette machine
export SSH_USER=ubuntu                    # utilisateur SSH (défaut ubuntu)

# Cas A — machine Cloud fournie (pas besoin d'AWS) :
export CLOUD_HOST="IP_CLOUD"              # ou "local" (Cloud sur la même machine que l'Edge, pour un essai)

# Cas B — AWS (sans CLOUD_HOST) : Terraform crée EC2 + IP élastique + S3 + DynamoDB + rôle IAM
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=...   # le jeton s'il est fourni
export TF_VAR_admin_cidr="$(curl -s ifconfig.me)/32"
# AWS Academy (création de rôle interdite) : export TF_VAR_instance_profile_name=LabInstanceProfile
```

### 2.3 Lancer
```bash
./scripts/deploy.sh all
```
Enchaînement : (Terraform si AWS, sinon génération des secrets + certificat lié à `CLOUD_HOST`) → Ansible Cloud
(Docker, image, `.env` 0600, pare-feu, démarrage, attente de la page et de l'API) → Ansible Edge (Docker, image,
jeton du nœud, vidéos de test, démarrage, **vérification que l'Edge joint bien le Cloud**). Durée affichée à la fin.

### 2.4 Vérifier
```bash
./scripts/deploy.sh status          # adresse du Cloud et statistiques
./scripts/deploy.sh load-start      # dépôt continu de vidéos (régime nominal) ; load-stop pour arrêter
```
- Page publique : `http://IP_CLOUD/` (vidéos « en cours » puis « traitées », extrait avec sous-titres, animaux).
- Grafana : `http://IP_EDGE:3000`, utilisateur `admin`, mot de passe `grafana_password` dans
  `infra/ansible/inventory/secrets.generated.yml`.
- Logs : `docker logs -f vidp-downscale` (et `vidp-langident`, `vidp-subtitle`, `vidp-animal`, `vidp-uploader`) ;
  côté Cloud `docker logs -f vidp-worker`.

### 2.5 Changer de mode ou redéployer une partie
```bash
VIDP_MODE=edge_first ./scripts/deploy.sh edge     # cloud_only | edge_first | hybrid
FEED_RATE=6 ./scripts/deploy.sh load-start        # vidéos par minute
./scripts/deploy.sh cloud
```

## 3. Vidéos de test
Ansible les génère sur l'Edge (une fois). Pour de vraies vidéos : déposer des `.mp4` dans `/opt/vidp/data/inbox`
(traitement immédiat) ou dans `data/videos` (bibliothèque du générateur). Sans fichier `.gt.json`, précision,
rappel et exactitude de langue ne sont pas calculables.

## 4. Scénarios de mesure (sur l'Edge)
Les perturbations réseau passent par `tc netem` dans le conteneur `vidp-wan`.
```bash
./scripts/wanctl.sh set --rate 5              # débit 5 Mbit/s (ou 10)
./scripts/wanctl.sh set --delay 100 --loss 1  # 100 ms, 1 % de pertes
./scripts/wanctl.sh down / up / status / reset

python3 scripts/run_scenarios.py --preset demo    # hybrid, nominal + coupure : ~15 min
python3 scripts/run_scenarios.py --preset quick   # 3 modes × 5 conditions × 1 : ~2 h
python3 scripts/run_scenarios.py --preset full    # 3 répétitions : ~6 h (intervalles de confiance)
python3 scripts/run_scenarios.py --modes hybrid --conditions blackout120 --reps 1 --duration 300 --rate 6
```
Conditions : `nominal`, `bw10`, `bw5`, `lat100_loss1`, `blackout120` (coupure de 2 min à t = 60 s).
Ne lancez pas d'autres charges lourdes sur l'Edge pendant les mesures (énergie faussée).

## 5. Résultats
```bash
python3 scripts/analyze.py      # lit edge/results/, écrit results/summary.md, summary.csv, results/figures/*.png
```
`summary.md` : p50/p95/p99 avec IC, pertes, doublons, backlog, temps de reprise, % déporté, octets WAN, volume
évité, CPU, WSS, énergie et J par résultat utile, exactitude de langue, précision/rappel animaux, WER.
Définitions : `docs/MESURES.md` (à citer). Les données brutes `edge/results/<run>/` sont à joindre au dépôt.
Sans RAPL (VM), l'énergie est un modèle marqué `model` : indicatif, à signaler.

## 6. Jour de la démo
Avant : passer `docs/CHECKLIST.md` au moins une fois sur de vraies machines et chronométrer `deploy.sh all`.
Pour gagner du temps : publier l'image à l'avance (CI → GHCR, modèles inclus) et `export VIDP_REGISTRY=ghcr.io/<org>/<repo>`.
```bash
export EDGE_HOSTS="IP1" CLOUD_HOST="IP2"         # adresses données le jour même
./scripts/deploy.sh all && ./scripts/deploy.sh load-start
# régime nominal : montrer la page publique et Grafana
./scripts/wanctl.sh set --rate 5                 # perturbation 1 (sur l'Edge)
./scripts/wanctl.sh down ; sleep 120 ; ./scripts/wanctl.sh up   # perturbation 2 : coupure de 2 min
```
Montrer : la latence p99 qui monte et le tampon qui grossit pendant la coupure, puis la reprise (tampon vidé, résultats
arrivés), sans perte ni doublon sur la page.

## 7. Arrêter et nettoyer
```bash
./scripts/deploy.sh destroy                      # AWS : supprime VM, IP, bucket, table, rôle
docker compose -f edge/docker-compose.yml down   # sur l'Edge ; cloud/docker-compose.yml sur le Cloud
```

## 8. Dépannage
| Symptôme | Piste |
|---|---|
| `EDGE_HOSTS` manquant | exporter `EDGE_HOSTS` (IP, ou `local`) |
| « ni CLOUD_HOST ni clés AWS » | exporter l'un ou l'autre |
| Ansible : SSH refusé | vérifier `SSH_USER`, la clé, `TF_VAR_admin_cidr` (votre IP a pu changer) |
| Échec « Connexion Edge -> Cloud vérifiée » | le Cloud voit une autre IP que `EDGE_HOSTS` (NAT) : `EDGE_CIDRS="IP_vue/32"` ou `CLOUD_FIREWALL=0` ; `docker logs vidp-uploader` |
| Vidéos bloquées « traitement Edge » | `docker logs vidp-langident` etc. ; `sqlite3 edge/spool/jobs.db "select stage,state,error from jobs"` |
| Page vide | `docker logs vidp-web vidp-public` ; `curl http://IP_CLOUD/healthz` |
| Terraform : rôle IAM refusé | `export TF_VAR_instance_profile_name=LabInstanceProfile` |
| Énergie `model` | RAPL illisible sur cette VM : le signaler |

## 9. Ce qui est validé ou non
Validé en développement : tests unitaires/API (local + S3/DynamoDB simulés), 4 étages réels, bout en bout dans les
3 modes avec coupure (0 perte, 0 doublon), chaîne d'analyse. **Jamais exécuté** : Docker Compose, Ansible,
Terraform, tc netem, RAPL, Grafana, eBPF — à valider sur vos machines avant la démo (`docs/CHECKLIST.md`).
