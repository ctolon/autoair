#!/usr/bin/env bash
# One-shot: copy examples/marketplace into the shared volume, create a signing key once,
# build a signed index and write the AUTOAIR_MARKETPLACE_SOURCES value next to it.
set -euo pipefail
MARKET=/opt/airflow/marketplace
mkdir -p "$MARKET"
rm -rf "$MARKET/index"
cp -r /opt/autoair-demo/examples/marketplace "$MARKET/index"
if [[ ! -s "$MARKET/market.pem" ]]; then
  autoair marketplace keygen --out "$MARKET/market.pem" > "$MARKET/market.pub"
fi
autoair marketplace build "$MARKET/index" --name examples --key "$MARKET/market.pem" \
  --description "AutoAir demo: examples/marketplace"
python - "$MARKET" <<'PY'
import json, pathlib, sys
market = pathlib.Path(sys.argv[1])
sources = [{
    "name": "examples",
    "url": f"file://{market}/index/index.json",
    "public_key": (market / "market.pub").read_text().strip(),
}]
(market / "sources.json").write_text(json.dumps(sources))
PY
# The private key never leaves this volume's owner; replicas mount the volume read-only.
chown -R 50000:0 "$MARKET"
chmod 600 "$MARKET/market.pem"
echo "marketplace ready: $(ls "$MARKET/index/templates" | wc -l) templates"
