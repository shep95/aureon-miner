import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

import addresses as A
import app
import chains as mc
from app import Handler, RateLimiter, Server

# ------------------------------------------------------------ address vectors (real, published)

VALID = {
    "BTC": ["bc1qwhu7xz88spa56mfvmg0zhtwlr4sny0qel2k2t8"],
    "BCH": ["bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a",   # cashaddr spec vector
            "qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"],
    "KAS": ["kaspa:qqkqkzjvr7zwxxmjxjkmxxdwju9kjs6e9u82uh59z07vgaks6gg62v8707g73",
            "kaspa:qpzpfwcsqsxhxwup26r55fd0ghqlhyugz8cp6y3wxuddc02vcxtjg75pspnwz"],
    "XMR": ["44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A"],
    "DOGE": ["DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L"],
    "LTC": ["LaMT348PWRnrqeeWArpwQPbuanpXDZGEUz", A.b58check_encode(0x32, bytes(20))],
}


@pytest.mark.parametrize("sym,addr", [(s, a) for s, xs in VALID.items() for a in xs])
def test_valid_addresses(sym, addr):
    assert A.is_valid(sym, addr)


def _flip(a):
    i = len(a) - 3
    c = a[i]
    return a[:i] + ("q" if c != "q" else "p") + a[i + 1:] if c.islower() else a[:i] + ("A" if c != "A" else "B") + a[i + 1:]


@pytest.mark.parametrize("sym,addr", [(s, xs[0]) for s, xs in VALID.items()])
def test_single_character_typo_rejected(sym, addr):
    assert not A.is_valid(sym, _flip(addr))


@pytest.mark.parametrize("sym,other", [("LTC", "BTC"), ("DOGE", "LTC"), ("BCH", "KAS"), ("KAS", "BCH"), ("BTC", "LTC")])
def test_address_from_another_chain_rejected(sym, other):
    assert not A.is_valid(sym, VALID[other][0])


def test_keccak_vectors():
    assert A.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert A.keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"


def test_btc_address_never_reused_for_other_chains(monkeypatch):
    for s in A.VALIDATORS:
        monkeypatch.delenv(f"PAYOUT_ADDRESS_{s}", raising=False)
    monkeypatch.delenv("PAYOUT_ADDRESS", raising=False)
    assert list(A.configured_addresses()) == ["BTC"]


def test_invalid_env_address_refuses(monkeypatch):
    monkeypatch.setenv("PAYOUT_ADDRESS_DOGE", VALID["LTC"][0])
    with pytest.raises(ValueError):
        A.configured_addresses()


# ------------------------------------------------------------ revenue maths

def test_btc_matches_core_hashprice():
    m = mc.machine_day(mc.HARDWARE["sha256"], mc.TARGETS["sha256"], mc.CHAINS, 0.15)
    assert abs(m["revenue_usd"] - 7.304) < 0.01   # same as the aureon_core S21 ledger


def test_l9_merged_revenue_matches_market_reports():
    # reported early-nov 2026: about $6.3-6.9/day gross for an L9 on LTC+DOGE
    m = mc.machine_day(mc.HARDWARE["scrypt"], mc.TARGETS["scrypt"], mc.CHAINS, 0.0, pool_fee=0)
    assert 5.8 < m["revenue_usd"] < 7.2 and m["mining"] == ["LTC", "DOGE"]


def test_merged_mining_pays_more_than_either_chain():
    hw, ch = mc.HARDWARE["scrypt"], mc.CHAINS
    both = mc.machine_day(hw, mc.TARGETS["scrypt"], ch, 0)["revenue_usd"]
    ltc = mc.machine_day(hw, mc.Target("x", "x", "scrypt", ("LTC",)), ch, 0)["revenue_usd"]
    assert both > ltc * 2


def test_profit_switching_picks_better_chain():
    ch = dict(mc.CHAINS)
    ch["BCH"] = replace(ch["BCH"], price_usd=ch["BCH"].price_usd * 3)
    m = mc.machine_day(mc.HARDWARE["sha256"], mc.TARGETS["sha256"], ch, 0)
    assert m["mining"] == ["BCH"]
    assert m["revenue_usd"] > mc.machine_day(mc.HARDWARE["sha256"], mc.TARGETS["sha256"], mc.CHAINS, 0)["revenue_usd"]


def test_kaspa_emission_halves_in_a_year():
    k = mc.CHAINS["KAS"]
    assert abs(k.coins_per_day(360) / k.coins_per_day(0) - 0.5) < 0.01


@pytest.mark.parametrize("target", list(mc.TARGETS))
def test_fleet_revenue_never_exceeds_cap(target):
    p = mc.plan_pow(target, 0.05, 3000)
    assert p["revenue_usd_day"] <= 3000
    one = mc.machine_day(mc.HARDWARE[mc.TARGETS[target].algorithm], mc.TARGETS[target], mc.CHAINS, 0.05)
    assert p["revenue_usd_day"] + one["revenue_usd"] > 3000   # one more unit would cross it


def test_losing_fleet_says_do_not_build():
    p = mc.plan_pow("sha256", 0.15)
    assert p["verdict"].startswith("do not build") and p["horizon_days_running"] == 0


def test_staking_capital():
    s = mc.plan_stake("ETH", 3000)
    assert abs(s["capital_usd"] * 0.028 / 365 - 3000) < 1e-6


# ------------------------------------------------------------ http

@pytest.fixture(scope="module")
def base():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def no_limit(monkeypatch):
    monkeypatch.setattr(app, "LIMITER", RateLimiter(0))


def _req(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_plan_endpoint(base):
    code, r = _req(base, "/plan", {"tariff_usd_kwh": 0.05})
    assert code == 200 and r["cap_usd_day"] == 3000
    assert {p["algorithm"] for p in r["pow"]} == {"sha256", "scrypt", "kheavyhash", "randomx"}
    assert all(p["revenue_usd_day"] <= 3000 for p in r["pow"])
    assert next(p for p in r["pow"] if p["algorithm"] == "sha256")["payout_ready"] is True


@pytest.mark.parametrize("cap", [3000.01, 10_000, 0, -1])
def test_plan_cap_enforced(base, cap):
    assert _req(base, "/plan", {"tariff_usd_kwh": 0.05, "cap_usd_day": cap})[0] == 400


def test_plan_overrides(base):
    code, r = _req(base, "/plan", {"tariff_usd_kwh": 0.05, "chains": {"KAS": {"price_usd": 0.29}}})
    kas = next(p for p in r["pow"] if p["algorithm"] == "kheavyhash")
    assert code == 200 and kas["units"] < 100   # 10x price -> ~10x fewer machines


@pytest.mark.parametrize("body", [
    {"tariff_usd_kwh": 0.05, "chains": {"FAKE": {"price_usd": 1}}},
    {"tariff_usd_kwh": 0.05, "chains": {"KAS": {"evil": 1}}},
    {"tariff_usd_kwh": 0.05, "chains": {"KAS": {"price_usd": 0}}},
    {"tariff_usd_kwh": 0.05, "stake": {"ETH": {"apr": 0}}},
    {"tariff_usd_kwh": 0.05, "horizon_days": 5000},
    {"tariff_usd_kwh": 0.05, "chains": []},
])
def test_plan_rejects_bad_overrides(base, body):
    assert _req(base, "/plan", body)[0] == 400


def test_chains_and_payout_endpoints(base):
    code, r = _req(base, "/chains")
    assert code == 200 and {c["symbol"] for c in r["pow"]} == set(mc.CHAINS)
    code, p = _req(base, "/payout")
    assert code == 200 and "BTC" in p["chains"] and "DOGE" in p["missing"]
