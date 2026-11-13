import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import app
from app import Handler, RateLimiter, Server
from payout import DEFAULT_PAYOUT_ADDRESS, is_valid_mainnet_segwit, payout_address

CHAIN = {"difficulty": 148.36e12, "subsidy_btc": 3.125, "avg_fees_btc": 0.0228}
S21 = {"th": 234, "watts": 3510, "capex_usd": 3000}
HEAT = {"tariff_usd_kwh": 0.15, "heat_credit_usd_kwh": 0.15, "heat_fraction_used": 1}


# ------------------------------------------------------------ payout address

def test_default_address_valid():
    assert is_valid_mainnet_segwit(DEFAULT_PAYOUT_ADDRESS)


@pytest.mark.parametrize("addr", [
    DEFAULT_PAYOUT_ADDRESS[:-1] + "q",                          # bad checksum
    "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",               # testnet
    "1BoatSLRHtKNngkdXEeobR76b53LETtpyT",                       # legacy, unsupported
    "bc1QWHU7xz88spa56mfvmg0zhtwlr4sny0qel2k2t8",               # mixed case
    "",
])
def test_rejects_bad_addresses(addr):
    assert not is_valid_mainnet_segwit(addr)


def test_accepts_taproot():
    assert is_valid_mainnet_segwit("bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297")


def test_env_override_validated(monkeypatch):
    monkeypatch.setenv("PAYOUT_ADDRESS", "bc1qnotreal")
    with pytest.raises(ValueError):
        payout_address()


# ------------------------------------------------------------ http fixture

@pytest.fixture(scope="module")
def base():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def no_limit(monkeypatch):
    monkeypatch.setattr(app, "LIMITER", RateLimiter(0))


def _req(base, path, body=None, method=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


# ------------------------------------------------------------ functional

def test_health(base):
    code, h, body = _req(base, "/health")
    assert code == 200 and body["ok"]
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "Python" not in h.get("Server", "")


def test_payout_endpoint(base):
    code, _, cfg = _req(base, "/payout")
    assert code == 200 and cfg["payout_address"] == DEFAULT_PAYOUT_ADDRESS
    assert cfg["address_as_username"]["stratum_user"].startswith(DEFAULT_PAYOUT_ADDRESS + ".")


def test_hashprice_endpoint(base):
    code, _, r = _req(base, "/hashprice", CHAIN)
    assert code == 200 and abs(r["hashprice_btc_th_day"] * 1000 - 0.00042749) / 0.00042749 < 0.01


def test_ledger_endpoint(base):
    code, _, r = _req(base, "/ledger", {"machine": S21, "site": {"tariff_usd_kwh": 0.15},
                                        "chain": CHAIN, "btc_usd": 74624})
    assert code == 200 and r["run"] is False and r["payout_address"] == DEFAULT_PAYOUT_ADDRESS


def test_breakeven_endpoint(base):
    code, _, r = _req(base, "/breakeven", {"machine": S21, "chain": CHAIN, "btc_usd": 74624})
    assert code == 200 and 0.085 < r["breakeven_tariff_usd_kwh"] < 0.090


def test_mine_vs_buy_endpoint(base):
    used = {"th": 95, "watts": 3250, "capex_usd": 500}
    code, _, r = _req(base, "/mine-vs-buy", {"machine": used, "site": HEAT, "chain": CHAIN,
                                             "btc_usd": 74624, "run_days": 300})
    assert code == 200 and r["mining_wins"] is True


def test_max_capex_endpoint(base):
    code, _, r = _req(base, "/max-capex", {"chain": CHAIN, "btc_usd": 74624, "run_days": 300})
    assert code == 200 and 9 < r["max_capex_usd_per_th"] < 10


def test_infinite_ratio_becomes_null(base):
    free = {"th": 100, "watts": 3000, "capex_usd": 0}
    code, _, r = _req(base, "/mine-vs-buy", {"machine": free, "site": HEAT, "chain": CHAIN,
                                             "btc_usd": 74624, "run_days": 10})
    assert code == 200 and r["ratio"] is None


# ------------------------------------------------------------ hostile input

@pytest.mark.parametrize("body", [
    {"machine": S21},                                                            # missing fields
    {"machine": {**S21, "watts": 0}, "chain": CHAIN, "btc_usd": 74624},          # div by zero
    {"machine": {**S21, "th": "234"}, "chain": CHAIN, "btc_usd": 74624},         # string
    {"machine": {**S21, "th": True}, "chain": CHAIN, "btc_usd": 74624},          # bool
    {"machine": {**S21, "evil": 1}, "chain": CHAIN, "btc_usd": 74624},           # unknown key
    {"machine": S21, "chain": CHAIN, "btc_usd": -1},                             # negative price
    {"machine": S21, "chain": {**CHAIN, "difficulty": 0}, "btc_usd": 1},         # zero difficulty
    {"machine": [], "chain": CHAIN, "btc_usd": 74624},                           # wrong type
])
def test_bad_bodies_are_400(base, body):
    code, _, r = _req(base, "/breakeven", body)
    assert code == 400 and "error" in r


@pytest.mark.parametrize("run_days", [0, -5, 3651, 10 ** 9, 1.5, "30", True])
def test_run_days_bounded(base, run_days):
    code, _, _ = _req(base, "/max-capex", {"chain": CHAIN, "btc_usd": 1, "run_days": run_days})
    assert code == 400


@pytest.mark.parametrize("raw", [b"[1,2]", b"not json", b'{"btc_usd": NaN}',
                                 b'{"btc_usd": Infinity}', b"\xff\xfe"])
def test_malformed_json_is_400(base, raw):
    code, _, _ = _req(base, "/breakeven", raw=raw)
    assert code == 400


def test_oversized_body_413(base):
    code, _, _ = _req(base, "/breakeven", raw=b"{" + b" " * 20000 + b"}")
    assert code == 413


def test_wrong_content_type_415(base):
    code, _, _ = _req(base, "/breakeven", raw=b"a=1", headers={"Content-Type": "text/plain"})
    assert code == 415


def test_negative_content_length_does_not_hang(base):
    host, port = base.rsplit(":", 1)[0][7:], int(base.rsplit(":", 1)[1])
    with socket.create_connection((host, port), timeout=3) as s:
        s.sendall(b"POST /breakeven HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n")
        assert s.recv(64).startswith(b"HTTP/1.1 400")


def test_unknown_route_and_method(base):
    assert _req(base, "/nope")[0] == 404
    assert _req(base, "/ledger", {}, method="DELETE")[0] == 405


def test_rate_limit(base, monkeypatch):
    monkeypatch.setattr(app, "LIMITER", RateLimiter(2))
    codes = [_req(base, "/payout")[0] for _ in range(3)]
    assert codes == [200, 200, 429]
    assert _req(base, "/health")[0] == 200  # health never limited


def test_connection_cap(monkeypatch):
    monkeypatch.setattr(app, "MAX_CONNECTIONS", 1)
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        hog = socket.create_connection(("127.0.0.1", port), timeout=3)  # holds the only slot
        import time; time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            assert s.recv(64) == b""  # refused
        hog.close()
        time.sleep(0.3)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            assert r.status == 200  # slot freed
    finally:
        srv.shutdown()


def test_rate_limit_ignores_spoofed_forwarded_for(base, monkeypatch):
    monkeypatch.setattr(app, "LIMITER", RateLimiter(2))
    codes = [_req(base, "/payout", headers={"X-Forwarded-For": f"6.6.6.{i}, 9.9.9.9"})[0]
             for i in range(3)]
    assert codes == [200, 200, 429]
