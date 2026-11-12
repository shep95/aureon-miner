"""aureon http api. stdlib only; railway sets PORT.

GET  /health
GET  /payout                 payout address + stratum settings for your asics
POST /hashprice              {difficulty, subsidy_btc, avg_fees_btc}
POST /ledger                 {machine, site, hashprice_btc_th_day | chain, btc_usd}
POST /breakeven              {machine, hashprice_btc_th_day | chain, btc_usd, pool_fee?}
POST /mine-vs-buy            {machine, site, hashprice_btc_th_day | chain, btc_usd, run_days, monthly_hashprice_decay?}
POST /max-capex              {hashprice_btc_th_day | chain, btc_usd, run_days, pool_fee?, monthly_hashprice_decay?}

"chain" = {difficulty, subsidy_btc, avg_fees_btc}; use it instead of a hashprice.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aureon_core as core
from payout import payout_address, stratum_config

MAX_BODY = 64 * 1024
MAX_RUN_DAYS = 3650


def _hashprice(body):
    if "hashprice_btc_th_day" in body:
        return float(body["hashprice_btc_th_day"])
    c = body["chain"]
    return core.hashprice_btc_per_th_day(float(c["difficulty"]), float(c["subsidy_btc"]),
                                         float(c["avg_fees_btc"]))


def _run_days(body):
    d = int(body["run_days"])
    if not 1 <= d <= MAX_RUN_DAYS:
        raise ValueError(f"run_days must be 1..{MAX_RUN_DAYS}")
    return d


def _machine(body):
    return core.Machine(**body["machine"])


def _site(body):
    return core.Site(**body.get("site", {}))


ROUTES = {
    "/hashprice": lambda b: {"hashprice_btc_th_day": core.hashprice_btc_per_th_day(
        float(b["difficulty"]), float(b["subsidy_btc"]), float(b["avg_fees_btc"]))},
    "/ledger": lambda b: core.daily_ledger(_machine(b), _site(b), _hashprice(b), float(b["btc_usd"])),
    "/breakeven": lambda b: {"breakeven_tariff_usd_kwh": core.breakeven_tariff(
        _machine(b), _hashprice(b), float(b["btc_usd"]), float(b.get("pool_fee", 0.02)))},
    "/mine-vs-buy": lambda b: core.mine_vs_buy(
        _machine(b), _site(b), _hashprice(b), float(b["btc_usd"]), _run_days(b),
        float(b.get("monthly_hashprice_decay", 0.0))),
    "/max-capex": lambda b: {"max_capex_usd_per_th": core.max_capex_per_th(
        _hashprice(b), float(b["btc_usd"]), _run_days(b), float(b.get("pool_fee", 0.02)),
        float(b.get("monthly_hashprice_decay", 0.0)))},
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health"):
            return self._send(200, {"ok": True, "service": "aureon", "routes": ["/payout", *ROUTES]})
        if path == "/payout":
            return self._send(200, stratum_config())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        fn = ROUTES.get(self.path.split("?")[0])
        if fn is None:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            result = fn(body)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        result["payout_address"] = payout_address()
        self._send(200, result)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    addr = payout_address()  # fail fast on a bad address
    port = int(os.environ.get("PORT", "8000"))
    print(f"aureon listening on :{port}, payout -> {addr}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
