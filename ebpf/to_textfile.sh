#!/usr/bin/env bash
# Convertit la sortie de tcp_bytes.bt en métrique Prometheus (collecteur textfile de node-exporter).
OUT=${1:-edge/textfile/ebpf_tcp.prom}
awk -v out="$OUT" '
  /^@tx\[/ { match($0,/\[(.*)\]: ([0-9]+)/,m); b[m[1]]+=m[2]; 
             f=out".tmp"; print "# TYPE ebpf_tcp_sendmsg_bytes counter" > f;
             for (k in b) printf "ebpf_tcp_sendmsg_bytes{comm=\"%s\"} %d\n", k, b[k] >> f; close(f);
             system("mv " f " " out) }'
