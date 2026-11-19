#!/usr/bin/env python3
"""Secrets et certificat du Cloud quand il est déployé SANS Terraform (VM fournie, ou pas d'accès AWS).

    python3 scripts/gen_secrets.py --cloud-host 192.0.2.10 --out infra/ansible/inventory/secrets.generated.yml

- secret maître (HMAC des jetons de nœud), mot de passe Grafana : générés une fois puis conservés ;
- certificat TLS auto-signé lié à l'adresse du Cloud (épinglé par l'Edge) : régénéré si l'adresse change.
Fichier écrit en 0600, ignoré par Git. Utilise `cryptography` (installé avec Ansible).
"""
import argparse
import datetime
import ipaddress
import os
import secrets
import sys

import yaml


def make_cert(host):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    try:
        san = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vidp-cloud")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=60))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud-host", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cur = {}
    if os.path.exists(a.out):
        cur = yaml.safe_load(open(a.out)) or {}
    s = dict(cur)
    s.setdefault("cloud_master_secret", secrets.token_hex(24))
    s.setdefault("grafana_password", secrets.token_urlsafe(15))
    if cur.get("cloud_ip") != a.cloud_host or "tls_cert_pem" not in cur:
        s["tls_cert_pem"], s["tls_key_pem"] = make_cert(a.cloud_host)
        print(f"certificat TLS généré pour {a.cloud_host}", file=sys.stderr)
    s.update(cloud_ip=a.cloud_host, storage=os.environ.get("STORAGE", "local"))
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(s, f, default_flow_style=False)


if __name__ == "__main__":
    main()
