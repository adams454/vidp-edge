#!/usr/bin/env python3
"""Calcule le jeton d'identité d'un nœud Edge : HMAC-SHA256(secret_maître, node_id)."""
import hashlib, hmac, os, sys
print(hmac.new(os.environ["CLOUD_MASTER_SECRET"].encode(), sys.argv[1].encode(), hashlib.sha256).hexdigest(), end="")
