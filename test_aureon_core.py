import math
from aureon_core import (Machine, Site, hashprice_btc_per_th_day, daily_ledger,
                         breakeven_tariff, mine_vs_buy, max_capex_per_th)

# observed inputs, hashrate index roundup 2026-11-09: difficulty 148.36t, 7-day avg fees 0.0228 btc,
# btc ~ $74,624, published hashprice 0.00042749 btc / ph / day
DIFF, SUB, FEES, PRICE = 148.36e12, 3.125, 0.0228, 74_624
HP = hashprice_btc_per_th_day(DIFF, SUB, FEES)
S21 = Machine(th=234, watts=3510, capex_usd=3000)  # capex illustrative


def test_hashprice_matches_published_within_1pct():
    published = 0.00042749 / 1000
    assert abs(HP - published) / published < 0.01


def test_breakeven_tariff_about_9_cents():
    t = breakeven_tariff(S21, HP, PRICE)
    assert 0.085 < t < 0.090


def test_grid_15c_no_heat_loses():
    led = daily_ledger(S21, Site(tariff_usd_kwh=0.15), HP, PRICE)
    assert not led["run"] and led["net_usd"] < -5


def test_resistive_heat_replacement_runs():
    s = Site(tariff_usd_kwh=0.15, heat_credit_usd_kwh=0.15, heat_fraction_used=1.0)
    led = daily_ledger(S21, s, HP, PRICE)
    assert led["run"] and math.isclose(led["net_usd"], led["revenue_usd"], rel_tol=1e-9)


def test_heat_pump_home_loses():
    s = Site(tariff_usd_kwh=0.15, heat_credit_usd_kwh=0.15 / 3, heat_fraction_used=1.0)
    assert not daily_ledger(S21, s, HP, PRICE)["run"]


def test_solar_surplus_beats_low_export_credit():
    s = Site(tariff_usd_kwh=0.15, surplus_kwh_day=84.24, export_credit_usd_kwh=0.04)
    assert daily_ledger(S21, s, HP, PRICE)["run"]


def test_new_machine_at_3000_loses_to_buying_in_one_heat_season():
    s = Site(tariff_usd_kwh=0.15, heat_credit_usd_kwh=0.15, heat_fraction_used=1.0)
    r = mine_vs_buy(S21, s, HP, PRICE, run_days=150)
    assert not r["mining_wins"]


def test_capex_ceiling_free_heat_two_seasons():
    cap = max_capex_per_th(HP, PRICE, run_days=300)
    assert 9 < cap < 10   # dollars per th/s, no hashprice decay


def test_cheap_used_machine_free_heat_wins():
    used = Machine(th=95, watts=3250, capex_usd=500)   # s19-class, illustrative price
    s = Site(tariff_usd_kwh=0.15, heat_credit_usd_kwh=0.15, heat_fraction_used=1.0)
    assert mine_vs_buy(used, s, HP, PRICE, run_days=300)["mining_wins"]
