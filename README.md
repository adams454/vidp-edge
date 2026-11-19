# VidP-Edge — pipeline vidéo hybride Edge–Cloud

Projet Cloud, sujet 1 (VidP-Edge). Les vidéos stockées localement sur le nœud **Edge** passent par **4 conteneurs
en série** — **Downscale → LangIdent → Subtitle → Animal detect** — puis seules les **métadonnées** (langue,
sous-titres, animaux et instants) et les **extraits utiles** (vignette, courts extraits autour des animaux)
traversent le WAN. Le **Cloud** (VM EC2, ou VM fournie) les agrège dans **S3 + DynamoDB** (ou SQLite + disque) et
les publie sur une **page web publique (Apache2)**, avec les vidéos traitées et celles en cours. Le système
continue de fonctionner si le WAN se dégrade ou disparaît (tampon durable, reprise contrôlée, 0 perte, 0 doublon).

```
EDGE (Docker Compose)                                                        CLOUD (VM, Terraform/Ansible)
 magasin   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  tampon   ┌────────┐  TLS épinglé  ┌─────────┐  S3 + DynamoDB
 local ──▶ │downscale │─▶│langident │─▶│ subtitle │─▶│  animal  │─▶ (SQLite) │uploader│═══ WAN ══════▶│ ingest  │─▶ (ou SQLite)
 (inbox)   │ ffmpeg   │  │ Whisper  │  │ Whisper  │  │MobileNet │   quota   │reprise │  jeton HMAC   │ worker  │       │
           └────┬─────┘  └──────────┘  └──────────┘  │  -SSD    │           └────────┘               │(Video   │       ▼
                │ politique hybride : déporter vers le Cloud si l'Edge sature et que le WAN le permet │Product.)│  Apache2 :80
                └──────────────────────────────────────────────────────────────────────────────────▶ └─────────┘  page publique
 Prometheus · Grafana · cAdvisor · node-exporter · énergie (RAPL) · eBPF          Prometheus Cloud (lu par Grafana)
```

## Les trois modes (scénarios obligatoires)
| Mode | Comportement |
|---|---|
| `cloud_only` | **Référence** : la vidéo source complète part au Cloud, qui exécute les 4 étages. |
| `edge_first` | Les 4 étages tournent sur l'Edge ; seuls métadonnées et extraits utiles (pleine qualité) partent. |
| `hybrid` | **Politique adaptative** : traitement local tant que l'Edge suit ; s'il sature et que le WAN le permet, la vidéo réduite est déportée au Cloud (comparaison des ETA, coûts appris en ligne) ; qualité des extraits adaptée au débit mesuré ; en coupure tout reste local et part à la reprise. |

Coupure WAN de 2 min et débit limité à 5/10 Mbit/s : `scripts/wanctl.sh` (tc/netem) et `scripts/run_scenarios.py`.

> **Mode d'emploi pas à pas : [`docs/GUIDE_UTILISATION.md`](docs/GUIDE_UTILISATION.md)** ·
> Architecture et modèle de menace : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
> Protocole de mesure : [`docs/MESURES.md`](docs/MESURES.md)

## Démarrage rapide sur un PC (sans Docker ni AWS)
```bash
pip install -r requirements-dev.txt     # + ffmpeg (et espeak-ng pour les voix de synthèse) dans le PATH
make models videos                      # modèles vérifiés par SHA-256 + 8 vidéos de test à vérité terrain
make test                               # unitaires, API (local + S3/DynamoDB simulés), 4 étages réels, bout en bout avec coupure
make smoke && python3 scripts/analyze.py --results /tmp/vidp_smoke/results --out /tmp/vidp_smoke/analysis
```

## Déploiement (une commande, depuis une machine vierge)
Poste de contrôle : `ansible` (+ `terraform` si AWS) et une clé SSH. Machines Edge/Cloud : Linux Debian/Ubuntu.
```bash
export EDGE_HOSTS="IP_EDGE"          # adresses données par l'enseignant (virgules), ou "local"
export CLOUD_HOST="IP_CLOUD"         # machine Cloud fournie… OU, à la place, les clés AWS (Terraform crée EC2+S3+DynamoDB)
./scripts/deploy.sh all              # installe Docker, construit/récupère les images, secrets, démarre, VÉRIFIE
./scripts/deploy.sh load-start       # régime nominal : vidéos déposées en continu (démo)
```
Page publique `http://IP_CLOUD/` · Grafana `http://IP_EDGE:3000` (admin / mot de passe généré).

## Arborescence
`src/vidp/` : `stages.py` (les 4 étages), `pipeline.py`, `edge_worker.py` (conteneurs d'étage), `jobs.py` (file de
travaux), `outbox.py` + `edge_uploader.py` (tampon et envoi fiable), `policy.py`, `cloud.py` + `storage.py` (Cloud),
`web/index.html` (page publique), `synth.py` (vidéos de test), `feeder.py` (charge), `models.py` (modèles vérifiés),
`energy.py` · `edge/`, `cloud/` : Compose + monitoring · `infra/terraform`, `infra/ansible` · `scripts/` · `tests/` · `docs/`.

## Ce qui a été validé, et ce qui ne l'a pas été
**Validé sur poste de développement (Windows, sans Docker)** : tests unitaires et API (stockage local ET S3/DynamoDB
simulés par moto), les 4 étages réels sur les vidéos de test (langue 8/8, animaux 6/6, 0 faux positif),
bout en bout dans les 3 modes avec coupure brutale du Cloud (0 perte, 0 doublon, tampon vidé), chaîne d'analyse.
**Non exécuté ici** (pas de Docker, de Linux root ni de compte AWS sur ce poste) : `docker compose`, Ansible,
`terraform apply`, `tc netem`, RAPL, Grafana, eBPF. À valider sur les vraies machines avant la démo :
[`docs/CHECKLIST.md`](docs/CHECKLIST.md).

## Limites connues
- Vidéos de test synthétiques (photos d'animaux incrustées, voix de synthèse) : reproductibles et à vérité terrain,
  mais plus simples que des scènes réelles. MobileNet-SSD ne connaît que 6 animaux (VOC) ; Whisper *tiny* est
  sobre mais moins précis que les modèles plus gros (compromis énergie).
- Une seule VM Cloud (pas de Load Balancer) ; DynamoDB lu par Scan (suffisant au volume du projet).
- Jeton de nœud statique (pas d'anti-rejeu) ; certificat auto-signé épinglé.
- En `cloud_only`, une coupure très longue peut dépasser le quota disque (les sources ne sont jamais évincées).
