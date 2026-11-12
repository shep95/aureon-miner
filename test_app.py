import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app import Handler
from payout import DEFAULT_PAYOUT_ADDRESS, is_valid_mainnet_segwit, payout_address

CHAIN = {"difficulty": 148.36e12, "subsidy_btc": 3.125, "avg_fees_btc": 0.0228}
S21 = {"th": 234, "watts": 3510, "capex_usd": 3000}


def test_default_address_valid():
    assert is_valid_mainnet_segwit(DEFAULT_PAYOUT_ADDRESS)


def test_rejects_corrupted_address():
    bad = DEFAULT_PAYOUT_ADDRESS[:-1] + ("q" if DEFAULT_PAYOUT_ADDRESS[-1] != "q" else "p")
    assert not is_valid_mainnet_segwit(bad)
    assert not is_valid_mainnet_segwit("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx")


def test_env_override_validated(monkeypatch):
    monkeypatch.setenv("PAYOUT_ADDRESS", "bc1qnotreal")
    with pytest.raises(ValueError):
        payout_address()


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_payout_endpoint(base):
    with urllib.request.urlopen(base + "/payout") as r:
        cfg = json.load(r)
    assert cfg["payout_address"] == DEFAULT_PAYOUT_ADDRESS
    assert cfg["address_as_username"]["stratum_user"].startswith(DEFAULT_PAYOUT_ADDRESS + ".")


def test_ledger_endpoint(base):
    r = _post(base, "/ledger", {"machine": S21, "site": {"tariff_usd_kwh": 0.15},
                                "chain": CHAIN, "btc_usd": 74624})
    assert r["run"] is False and r["payout_address"] == DEFAULT_PAYOUT_ADDRESS


def test_breakeven_endpoint(base):
    r = _post(base, "/breakeven", {"machine": S21, "chain": CHAIN, "btc_usd": 74624})
    assert 0.085 < r["breakeven_tariff_usd_kwh"] < 0.090


def test_bad_input_is_400(base):
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(base, "/mine-vs-buy", {"machine": S21})
    assert e.value.code == 400
