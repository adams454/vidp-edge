# Architecture et modèle de menace

## Correspondance avec l'énoncé
| Énoncé (VidP + VidP-Edge) | Réalisation |
|---|---|
| Vidéos stockées en local, traitées par un ensemble de conteneurs | magasin local `data/inbox` ; **4 conteneurs en série** `downscale → langident → subtitle → animal` (Docker Compose — « Kubernetes ou autre ») |
| Downscale / LangIdent / Subtitle / Animal detect | ffmpeg (H.264 360p) / Whisper *tiny* int8 + VAD / Whisper (WebVTT) / MobileNet-SSD VOC (OpenCV DNN) |
| Buffer Edge, mode déconnecté | file de travaux + tampon d'envoi SQLite/WAL durables (`spool/`), quota disque, éviction par intérêt |
| Décision locale : traiter sur l'Edge, envoyer les métadonnées, seulement les extraits utiles | politique `policy.py` : placement Edge/Cloud par ETA, extraits autour des animaux seulement, qualité adaptée au débit |
| Métadonnées + vidéos agrégées par des VMs (EC2) qui enregistrent sur S3 | API d'ingestion + worker « Video Production » ; S3 (sources, extraits, vignettes, sous-titres) + DynamoDB (métadonnées) ; ou SQLite + disque (« BD simple ») sans AWS |
| Page web publique (Apache2) : vidéos traitées + métadonnées et en cours | conteneur `httpd` (Apache2) + API publique en lecture seule |
| Terraform + Ansible, déploiement depuis une machine vierge | `scripts/deploy.sh all` (Terraform si AWS, sinon VM fournie) puis Ansible (Docker, images, modèles vérifiés, secrets, vérifications) |
| eBPF / Prometheus / Grafana | métriques par étage, WAN, tampon, Cloud ; cAdvisor, node-exporter, RAPL ; sonde eBPF `ebpf/tcp_bytes.bt` |
| Scénarios cloud-only, edge-first, hybride, coupure 2 min, 5/10 Mbit/s | `scripts/run_scenarios.py` + `scripts/wanctl.sh` |

Un Load Balancer devant plusieurs VMs « Video Production » figurait dans l'ancien schéma ; il n'est pas exigé par
l'énoncé actuel (« VM ou services dans le Cloud »). Une seule VM suffit au volume du projet ; le worker est
découplé de l'API par une file durable, donc ajouter des VMs derrière un ALB ne demanderait pas de changer le code.

## Parcours d'une vidéo
1. **Arrivée** : le générateur dépose `X.mp4` dans le magasin local (renommage atomique). `t0` = son mtime.
   L'étage downscale crée un travail (id stable = hash du nœud, du run, du nom, de la taille et de `t0`) et envoie
   un statut « en cours » (petit, prioritaire).
2. **Placement** :
   - `cloud_only` (référence) : la vidéo source complète part au Cloud, qui exécute les 4 étages ;
   - `edge_first` : les 4 étages tournent sur l'Edge ;
   - `hybrid` : après Downscale (qui divise le volume par ~10), la politique compare
     `ETA_local` = file locale × coût de l'étage goulot + durée × coût total (appris sur les dernières vidéos) et
     `ETA_cloud` = (tampon + vidéo réduite) / débit mesuré + RTT + (file Cloud + durée) × coût Cloud (publié par le
     Cloud). On déporte si `ETA_cloud < (1 − 20 %) × ETA_local` et si le WAN est joignable.
3. **Traitement local** : chaque étage réclame le plus ancien travail prêt, le traite, le fait avancer.
4. **Sortie** : métadonnées (langue, sous-titres, animaux et instants, temps par étage) + extraits utiles :
   vignette annotée, sous-titres WebVTT, courts extraits vidéo **seulement autour des animaux**. Qualité choisie
   pour que l'envoi tienne dans le budget (full 360p > low 240p > vignette seule).
5. **Tampon et envoi** : résultats avant fichiers ; téléversement par morceaux avec reprise à l'octet près,
   SHA-256 vérifié ; backoff exponentiel avec gigue pendant une coupure ; à la reconnexion, reprise immédiate,
   2 envois en parallèle au plus (le lien rétabli n'est pas saturé).
6. **Cloud** : écriture conditionnelle (un résultat n'est publié qu'une fois, un statut en retard n'écrase pas un
   résultat) → au-moins-une-fois côté Edge + idempotence côté Cloud = **exactement une fois**.

## Modèle de menace (STRIDE résumé)
| Actif / menace | Mesure | Risque résiduel |
|---|---|---|
| Usurpation d'un nœud Edge (S) | jeton HMAC-SHA256(secret maître, node_id) par nœud ; le Cloud vérifie que le `node` du document = nœud authentifié | jeton statique (pas d'anti-rejeu) ; rotation = nouveau secret + redéploiement |
| Interception / altération sur le WAN (T, I) | TLS ; certificat du Cloud **épinglé** côté Edge ; SHA-256 de chaque fichier vérifié au commit | certificat auto-signé (pas d'autorité publique) |
| Faux Cloud (S) | épinglage : l'Edge n'accepte que le certificat déployé par Ansible | — |
| Fuite de secrets (I) | secrets générés (Terraform ou `gen_secrets.py`), fichiers 0600, jamais dans Git ni dans les images ; le secret maître ne quitte pas le poste de contrôle (jetons calculés là) ; le jeton n'est donné qu'au conteneur téléverseur ; l'API publique ne reçoit aucun secret | secrets en clair sur disque des VMs |
| Accès aux données AWS (E) | rôle IAM d'instance limité à `s3:Get/PutObject` sur le bucket et `dynamodb:GetItem/UpdateItem/Scan` sur la table ; bucket privé chiffré ; IMDSv2 | AWS Academy : profil `LabInstanceProfile` plus large que nécessaire |
| Exposition réseau (E) | groupe de sécurité (ou iptables `DOCKER-USER` sur une VM fournie) : 8443 et Prometheus réservés aux IP Edge, SSH à l'IP admin, 80 public | page publique sans authentification (exigence de l'énoncé) |
| Compromission d'un conteneur (E) | utilisateur non-root, `read_only`, `cap_drop: ALL`, `no-new-privileges`, limites CPU/mémoire ; seul `wan` a `NET_ADMIN` | node-exporter/cAdvisor ont des montages hôte en lecture |
| Chaîne d'approvisionnement (T) | modèles téléchargés au build avec **SHA-256 épinglés** (révision Whisper figée) ; CI : tests, trivy, signature cosign | dépendances pip non épinglées par hash |
| Déni de service par saturation du disque Edge (D) | quota du tampon ; éviction des extraits les moins intéressants d'abord ; métadonnées et vidéos à déporter jamais évincées | coupure très longue en `cloud_only` : le quota peut être dépassé (sources jamais supprimées) |
| Contenu sensible (I) | les sources ne sont jamais publiques (`/media/sources/...` refusé) ; seuls les extraits utiles quittent l'Edge en edge_first/hybride | — |
