#!/usr/bin/env bash
# Émulation du WAN sur l'interface du téléverseur (conteneur vidp-wan) avec tc/netem + tbf.
#   wanctl.sh set [--rate MBIT] [--delay MS] [--loss PCT]   profil continu
#   wanctl.sh down | up                                     coupure / rétablissement
#   wanctl.sh reset | status
set -euo pipefail
C=${WAN_CONTAINER:-vidp-wan}; IF=${WAN_IFACE:-eth0}
STATE="${WAN_STATE:-$(dirname "$0")/../.wanstate}"
RATE=""; DELAY=0; LOSS=0; DOWN=0
[ -f "$STATE" ] && source "$STATE"
save(){ printf 'RATE=%q\nDELAY=%q\nLOSS=%q\nDOWN=%q\n' "$RATE" "$DELAY" "$LOSS" "$DOWN" > "$STATE"; }
tcx(){ docker exec "$C" tc "$@"; }
apply(){
  tcx qdisc del dev "$IF" root 2>/dev/null || true
  local loss="$LOSS"; [ "$DOWN" = 1 ] && loss=100
  if [ "$DELAY" != 0 ] || [ "$loss" != 0 ] || [ -n "$RATE" ]; then
    tcx qdisc add dev "$IF" root handle 1: netem delay "${DELAY}ms" loss "${loss}%" limit 100000
    [ -n "$RATE" ] && tcx qdisc add dev "$IF" parent 1:1 handle 10: tbf rate "${RATE}mbit" burst 64kb latency 400ms
  fi
  save
}
cmd=${1:-status}; shift || true
case "$cmd" in
  set) while [ $# -gt 0 ]; do case "$1" in
         --rate) RATE=$2; shift 2;; --delay) DELAY=$2; shift 2;; --loss) LOSS=$2; shift 2;;
         *) echo "option inconnue $1"; exit 2;; esac; done; DOWN=0; apply;;
  down) DOWN=1; apply;;
  up) DOWN=0; apply;;
  reset) RATE=""; DELAY=0; LOSS=0; DOWN=0; apply;;
  status) tcx -s qdisc show dev "$IF";;
  *) sed -n 2,6p "$0"; exit 2;;
esac
echo "WAN: rate=${RATE:-illimité}Mbit delay=${DELAY}ms loss=${LOSS}% down=${DOWN}"
