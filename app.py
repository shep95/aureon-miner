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
import math
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aureon_core as core
from payout import payout_address, stratum_config

MAX_BODY = 16 * 1024
MAX_RUN_DAYS = 3650
SOCKET_TIMEOUT_S = 10
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") == "1"  # railway sits behind a proxy


_LOG_LOCK = threading.Lock()


def log(msg, err=False):
    stream = sys.stderr if err else sys.stdout
    with _LOG_LOCK:
        stream.write(msg + "\n")
        stream.flush()


class BadInput(ValueError):
    pass


# ---------------------------------------------------------------- validation

def _num(obj, key, *, lo=0.0, hi=float("inf"), lo_open=False, default=None):
    if not isinstance(obj, dict):
        raise BadInput("expected a json object")
    if key not in obj:
        if default is not None:
            return default
        raise BadInput(f"missing field: {key}")
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadInput(f"{key} must be a number")
    v = float(v)
    if not math.isfinite(v) or v < lo or v > hi or (lo_open and v == lo):
        bound = "(" if lo_open else "["
        raise BadInput(f"{key} must be in {bound}{lo}, {hi}]")
    return v


def _obj(body, key, required=True):
    v = body.get(key, None if required else {})
    if not isinstance(v, dict):
        raise BadInput(f"{key} must be a json object")
    return v


_ALLOWED_MACHINE = {"th", "watts", "capex_usd"}
_ALLOWED_SITE = {"tariff_usd_kwh", "heat_credit_usd_kwh", "heat_fraction_used", "surplus_kwh_day",
                 "export_credit_usd_kwh", "pool_fee", "wear_usd_day"}


def _no_extra(obj, allowed, name):
    extra = set(obj) - allowed
    if extra:
        raise BadInput(f"unknown {name} field(s): {', '.join(sorted(extra))}")


def _machine(body):
    m = _obj(body, "machine")
    _no_extra(m, _ALLOWED_MACHINE, "machine")
    return core.Machine(th=_num(m, "th", lo_open=True, hi=1e6),
                        watts=_num(m, "watts", lo_open=True, hi=1e7),
                        capex_usd=_num(m, "capex_usd", hi=1e10))


def _site(body):
    s = _obj(body, "site", required=False)
    _no_extra(s, _ALLOWED_SITE, "site")
    return core.Site(
        tariff_usd_kwh=_num(s, "tariff_usd_kwh", hi=100, default=0.0),
        heat_credit_usd_kwh=_num(s, "heat_credit_usd_kwh", hi=100, default=0.0),
        heat_fraction_used=_num(s, "heat_fraction_used", hi=1, default=0.0),
        surplus_kwh_day=_num(s, "surplus_kwh_day", hi=1e7, default=0.0),
        export_credit_usd_kwh=_num(s, "export_credit_usd_kwh", hi=100, default=0.0),
        pool_fee=_num(s, "pool_fee", hi=0.99, default=0.02),
        wear_usd_day=_num(s, "wear_usd_day", hi=1e6, default=0.0),
    )


def _chain_hashprice(c):
    return core.hashprice_btc_per_th_day(
        _num(c, "difficulty", lo_open=True, hi=1e30),
        _num(c, "subsidy_btc", hi=50),
        _num(c, "avg_fees_btc", hi=1000),
    )


def _hashprice(body):
    if "hashprice_btc_th_day" in body:
        return _num(body, "hashprice_btc_th_day", hi=1.0)
    if "chain" in body:
        return _chain_hashprice(_obj(body, "chain"))
    raise BadInput("provide hashprice_btc_th_day or chain")


def _price(body):
    return _num(body, "btc_usd", lo_open=True, hi=1e9)


def _run_days(body):
    v = body.get("run_days")
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_RUN_DAYS:
        raise BadInput(f"run_days must be an integer 1..{MAX_RUN_DAYS}")
    return v


def _pool_fee(body):
    return _num(body, "pool_fee", hi=0.99, default=0.02)


def _decay(body):
    return _num(body, "monthly_hashprice_decay", hi=0.99, default=0.0)


ROUTES = {
    "/hashprice": lambda b: {"hashprice_btc_th_day": _chain_hashprice(b)},
    "/ledger": lambda b: core.daily_ledger(_machine(b), _site(b), _hashprice(b), _price(b)),
    "/breakeven": lambda b: {"breakeven_tariff_usd_kwh": core.breakeven_tariff(
        _machine(b), _hashprice(b), _price(b), _pool_fee(b))},
    "/mine-vs-buy": lambda b: core.mine_vs_buy(
        _machine(b), _site(b), _hashprice(b), _price(b), _run_days(b), _decay(b)),
    "/max-capex": lambda b: {"max_capex_usd_per_th": core.max_capex_per_th(
        _hashprice(b), _price(b), _run_days(b), _pool_fee(b), _decay(b))},
}


def _finite(obj):
    """json has no inf/nan; report them as null rather than emit invalid json."""
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in obj.items()}


# ---------------------------------------------------------------- rate limit

class RateLimiter:
    """fixed-window per-client limiter. in-memory, per instance."""

    def __init__(self, per_min):
        self.per_min = per_min
        self.lock = threading.Lock()
        self.window = 0
        self.counts = {}

    def allow(self, key):
        if self.per_min <= 0:
            return True
        now = int(time.monotonic() // 60)
        with self.lock:
            if now != self.window:
                self.window, self.counts = now, {}
            n = self.counts.get(key, 0) + 1
            self.counts[key] = n
            return n <= self.per_min


LIMITER = RateLimiter(RATE_LIMIT_PER_MIN)


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "aureon"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_S
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj, extra_headers=None):
        data = json.dumps(obj, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _client(self):
        headers = getattr(self, "headers", None)  # absent if the request line never arrived
        if TRUST_PROXY and headers is not None:
            xff = headers.get("X-Forwarded-For")
            if xff:
                # rightmost hop is the one appended by railway's edge; earlier hops are
                # client-supplied and spoofable, so never key the rate limiter on them
                return xff.rsplit(",", 1)[-1].strip()[:64]
        return self.client_address[0]

    def _limited(self):
        if LIMITER.allow(self._client()):
            return False
        self._send(429, {"error": "rate limit exceeded"}, {"Retry-After": "60"})
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health"):
            return self._send(200, {"ok": True, "service": "aureon",
                                    "routes": ["GET /payout", *(f"POST {r}" for r in ROUTES)]})
        if self._limited():
            return
        if path == "/payout":
            return self._send(200, stratum_config())
        self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        if self._limited():
            return
        fn = ROUTES.get(self.path.split("?", 1)[0])
        if fn is None:
            return self._send(404, {"error": "not found"})
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._send(411, {"error": "content-length required"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return self._send(400, {"error": "invalid content-length"})
        if n > MAX_BODY:
            self.close_connection = True
            return self._send(413, {"error": f"body exceeds {MAX_BODY} bytes"})
        ctype = self.headers.get("Content-Type", "application/json").split(";")[0].strip().lower()
        if n and ctype != "application/json":
            self.rfile.read(n)
            return self._send(415, {"error": "content-type must be application/json"})
        try:
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw, parse_constant=_reject_constant)
            if not isinstance(body, dict):
                raise BadInput("request body must be a json object")
            result = _finite(fn(body))
        except (BadInput, json.JSONDecodeError, UnicodeDecodeError) as e:
            return self._send(400, {"error": str(e)})
        except (ArithmeticError, ValueError):
            return self._send(400, {"error": "inputs produce an out-of-range result"})
        except Exception:  # never leak internals
            log("internal error on %s" % self.path.split("?", 1)[0], err=True)
            return self._send(500, {"error": "internal error"})
        result["payout_address"] = payout_address()
        self._send(200, result)

    def do_PUT(self):
        self._send(405, {"error": "method not allowed"}, {"Allow": "GET, HEAD, POST"})

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def log_message(self, fmt, *args):
        # no bodies or query strings in logs
        line = fmt % args
        log("%s %s" % (self._client(), line.split("?", 1)[0]))


def _reject_constant(name):
    raise BadInput(f"{name} is not allowed")


MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "64"))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        # cap concurrent connections so floods cannot exhaust threads/memory
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return  # idle / dropped clients are routine, not errors
        log("unhandled error from %s: %s" % (client_address[0], type(exc).__name__), err=True)


def main():
    addr = payout_address()  # fail fast on a bad address
    port = int(os.environ.get("PORT", "8000"))
    srv = Server(("0.0.0.0", port), Handler)

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"aureon listening on :{port}, payout -> {addr}")
    srv.serve_forever()
    srv.server_close()
    log("aureon stopped")


if __name__ == "__main__":
    main()
