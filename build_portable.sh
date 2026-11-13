#!/usr/bin/env sh
# builds dist/aureon.pyz: the whole aureon brain in one file (~18 KB).
# copy it to a flash drive and run on any machine with python 3.10+:
#   python aureon.pyz            -> starts the api on :8000
#   python aureon.pyz plan 0.05  -> prints the multi-chain plan at 5c/kWh
#   python aureon.pyz simulate   -> prints the 24-month bitcoin simulation
set -eu
cd "$(dirname "$0")"
rm -rf build/pyz && mkdir -p build/pyz dist
cp aureon_core.py payout.py addresses.py chains.py app.py simulate.py build/pyz/
cat > build/pyz/__main__.py <<'PY'
import json
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        import app
        app.main()
    elif cmd == "plan":
        import chains
        tariff = float(sys.argv[2]) if len(sys.argv) > 2 else 0.06
        print(json.dumps(chains.plan_all(tariff), indent=1, default=str))
    elif cmd == "simulate":
        import simulate
        sys.argv = sys.argv[1:]
        simulate.main()
    else:
        sys.exit("usage: aureon.pyz [serve | plan <usd_per_kwh> | simulate]")


main()
PY
python3 -m zipapp build/pyz -o dist/aureon.pyz -p "/usr/bin/env python3" -c
rm -rf build
ls -l dist/aureon.pyz
