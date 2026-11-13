import datetime as dt

import simulate as sim


def _get(results, scen, mkt):
    return next(r for r in results if r.scenario.startswith(scen) and r.market == mkt)


R = sim.run_all(24)


def test_every_scenario_market_pair_runs():
    assert len(R) == len(sim.SCENARIOS) * len(sim.MARKETS)


def test_aureon_never_runs_a_losing_15c_grid_miner():
    assert _get(R, "S21 on 15c grid, aureon", "base").days_run == 0


def test_following_aureon_beats_ignoring_it():
    on = _get(R, "S21 on 15c grid, always", "base")
    off = _get(R, "S21 on 15c grid, aureon", "base")
    assert off.cash_profit > on.cash_profit


def test_heat_only_runs_in_heating_season():
    r = _get(R, "Used S19", "base")
    summer = [m for m in r.monthly if m["month"].endswith(("-06", "-07", "-08"))]
    winter = [m for m in r.monthly if m["month"].endswith(("-12", "-01", "-02"))]
    assert all(m["days_run"] == 0 for m in summer)
    assert all(m["days_run"] > 25 for m in winter)


def test_halving_cuts_daily_sats_roughly_in_half():
    r = _get(R, "S21 on 15c grid, always", "base")  # runs every day
    per_day = {m["month"]: m["sats"] / m["days_run"] for m in r.monthly}
    expected = (sim.SUBSIDY / 2 + sim.FEES_BTC) / (sim.SUBSIDY + sim.FEES_BTC)
    assert abs(per_day["2028-04"] / per_day["2028-03"] - expected) < 0.03


def test_flat_price_cash_equals_hodl():
    r = _get(R, "Used S19", "base")
    assert abs(r.cash_profit - r.hodl_value_end) < 1e-6
