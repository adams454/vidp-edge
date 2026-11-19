#!/usr/bin/env bash
# Déploiement automatisé depuis une machine vierge. Poste de contrôle : ansible (+ terraform si AWS), clé SSH.
#
#  Jour de la démo — le professeur donne les adresses :
#     export EDGE_HOSTS="IP_EDGE1,IP_EDGE2"      # machines Edge (ou "local" = cette machine)
#     export CLOUD_HOST="IP_CLOUD"               # machine Cloud fournie (ou "local")   ... OU ...
#     export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... [AWS_SESSION_TOKEN=...]   (sans CLOUD_HOST)
#     ./scripts/deploy.sh all                    # Cloud puis Edge, vérifications comprises
#
#  ./scripts/deploy.sh cloud | edge | status | load-start | load-stop | destroy
#  Variables facultatives : SSH_USER (ubuntu), SSH_KEY (~/.ssh/id_ed25519), VIDP_MODE (hybrid),
#     FEED_RATE (4 vidéos/min), VIDP_REGISTRY/VIDP_TAG (image pré-publiée : pull au lieu de build),
#     EDGE_CIDRS ("IP1/32,IP2/32" : sources autorisées sur le Cloud si elles diffèrent de EDGE_HOSTS),
#     CLOUD_FIREWALL=0 (désactive le filtrage des ports 8443/9090 sur une VM fournie),
#     TF_VAR_admin_cidr, TF_VAR_region, TF_VAR_instance_profile_name (AWS Academy : LabInstanceProfile)
set -euo pipefail
cd "$(dirname "$0")/.."
TF=infra/terraform; AN=infra/ansible; INV=$AN/inventory
SSH_USER=${SSH_USER:-${EDGE_USER:-ubuntu}}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/id_ed25519}
cmd=${1:-all}
T0=$(date +%s)
mkdir -p "$INV"

host_line() {  # nom adresse id
  if [ "$2" = "local" ]; then echo "$1 ansible_connection=local node_id=$3"
  else echo "$1 ansible_host=$2 ansible_user=$SSH_USER ansible_ssh_private_key_file=$SSH_KEY node_id=$3"; fi
}

gen_edge_inventory() {
  : "${EDGE_HOSTS:?définir EDGE_HOSTS (adresses IP séparées par des virgules, ou le mot local)}"
  { echo "[edge]"; i=1; IFS=, read -ra H <<< "$EDGE_HOSTS"
    for h in "${H[@]}"; do host_line "$(printf 'edge%02d' $i)" "$h" "$(printf 'edge-%02d' $i)"; i=$((i+1)); done
  } > "$INV/edge.ini"
}

edge_cidrs() {
  if [ -n "${EDGE_CIDRS:-}" ]; then echo "$EDGE_CIDRS"; return; fi
  IFS=, read -ra H <<< "${EDGE_HOSTS:-}"; out=""
  for h in "${H[@]}"; do [ "$h" != "local" ] && out="$out${out:+,}$h/32"; done; echo "$out"
}

colocated() {  # le Cloud partage-t-il une machine Edge ?
  [ "${CLOUD_HOST:-}" = "local" ] && return 0
  [[ ",${EDGE_HOSTS:-}," == *",${CLOUD_HOST:-none},"* ]]
}

cloud_provision() {
  if [ -n "${CLOUD_HOST:-}" ]; then
    # --- Cloud sur une machine fournie (pas besoin d'AWS) : stockage local (« BD simple »)
    local addr="$CLOUD_HOST"
    if [ "$addr" = "local" ]; then addr=$(hostname -I | awk '{print $1}'); fi
    python3 scripts/gen_secrets.py --cloud-host "$addr" --out "$INV/secrets.generated.yml"
    { echo "[cloud]"; host_line cloud01 "$CLOUD_HOST" cloud; } > "$INV/cloud.generated.ini"
  else
    # --- Cloud AWS : EC2 + IP élastique + S3 + DynamoDB + rôle IAM + TLS + secrets (Terraform)
    : "${AWS_ACCESS_KEY_ID:?ni CLOUD_HOST ni clés AWS : définir l'un ou l'autre}"
    export TF_VAR_edge_cidrs="[\"$(edge_cidrs | sed 's/,/","/g')\"]"
    [ "$TF_VAR_edge_cidrs" = '[""]' ] && unset TF_VAR_edge_cidrs
    export TF_VAR_ssh_public_key="${SSH_KEY}.pub" TF_VAR_ssh_private_key="$SSH_KEY"
    terraform -chdir=$TF init -input=false
    terraform -chdir=$TF apply -auto-approve -input=false
    echo "Attente de SSH sur la VM…"
    ip=$(terraform -chdir=$TF output -raw cloud_ip)
    for _ in $(seq 60); do nc -z -w 2 "$ip" 22 2>/dev/null && break; sleep 3; done
  fi
}

play() {
  local extra=(-e "@inventory/secrets.generated.yml" -e "vidp_mode=${VIDP_MODE:-hybrid}"
               -e "feed_rate=${FEED_RATE:-4}" -e "edge_cidrs=$(edge_cidrs)" -e "cloud_firewall=${CLOUD_FIREWALL:-1}")
  colocated && extra+=(-e "cloud_colocated=true")
  [ -n "${VIDP_REGISTRY:-}" ] && extra+=(-e "vidp_registry=$VIDP_REGISTRY" -e "vidp_tag=${VIDP_TAG:-latest}")
  (cd "$AN" && ansible-playbook site.yml "${extra[@]}" "$@")
}

cloud_ip() { python3 -c "import yaml;print(yaml.safe_load(open('$INV/secrets.generated.yml'))['cloud_ip'])"; }

case "$cmd" in
  all)   gen_edge_inventory; cloud_provision; play
         echo "Déploiement terminé en $(( $(date +%s) - T0 )) s. Page publique : http://$(cloud_ip)/" ;;
  cloud) gen_edge_inventory; cloud_provision; play --limit localhost,cloud ;;
  edge)  gen_edge_inventory; play --limit localhost,edge ;;
  load-start) gen_edge_inventory; play --limit localhost,edge -e vidp_load=true ;;
  load-stop)  gen_edge_inventory; (cd "$AN" && ansible edge -b -m command -a "docker compose --profile load stop feeder chdir=/opt/vidp/edge") ;;
  status)
    ip=$(cloud_ip); echo "Cloud : $ip"
    curl -fsS "http://$ip/api/stats" && echo
    echo "Page publique : http://$ip/    Grafana (Edge) : http://<IP_EDGE>:3000 (admin / grafana_password dans $INV/secrets.generated.yml)" ;;
  destroy)
    if [ -z "${CLOUD_HOST:-}" ]; then terraform -chdir=$TF destroy -auto-approve
    else echo "Cloud fourni ($CLOUD_HOST) : rien à détruire côté AWS ; arrêter la pile avec docker compose down."; fi ;;
  *) sed -n 2,18p "$0"; exit 2 ;;
esac
