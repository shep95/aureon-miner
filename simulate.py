"""day-by-day income simulation built on aureon_core.

each simulated day:
  1. hashprice is recomputed from difficulty (growing), subsidy (halving) and fees
  2. btc price follows the scenario path
  3. heat is only worth something in heating months
  4. aureon's ledger decides run / idle; idle days cost nothing
results are compared with simply buying btc with the same money on day 0.

usage: python simulate.py [--months 24] [--json]
"""
import argparse
import datetime as dt
import json
from dataclasses import dataclass, field, replace

import aureon_core as core

# observed 2026-11-09 (same inputs as the test-suite)
START = dt.date(2026, 12, 1)
DIFFICULTY0 = 148.36e12
SUBSIDY = 3.125
FEES_BTC = 0.0228
BTC_USD0 = 74_624
HALVING = dt.date(2028, 4, 1)  # block 1,050,000; estimate, depends on block times
RAILWAY_USD_MONTH = 5.0        # hobby plan; the api itself mines nothing

# share of miner heat that displaces heating, by month (northern hemisphere home)
HEATING_SEASON = {1: 1.0, 2: 1.0, 3: 0.9, 4: 0.5, 5: 0.15, 6: 0.0, 7: 0.0, 8: 0.0,
                  9: 0.15, 10: 0.5, 11: 0.9, 12: 1.0}


@dataclass
class Market:
    name: str
    btc_monthly_growth: float   # price drift per 30 days
    diff_monthly_growth: float  # network difficulty growth per 30 days


MARKETS = [
    Market("bear", -0.02, 0.010),
    Market("base", 0.00, 0.020),
    Market("bull", +0.03, 0.030),
]


@dataclass
class Scenario:
    name: str
    machine: core.Machine
    site: core.Site
    units: int = 1
    seasonal_heat: bool = False    # scale heat_fraction_used by HEATING_SEASON
    always_on: bool = False        # ignore aureon's run/idle decision
    note: str = ""


S21 = core.Machine(th=234, watts=3510, capex_usd=3000)
S19_USED = core.Machine(th=95, watts=3250, capex_usd=500)
S21_USED = core.Machine(th=200, watts=3500, capex_usd=1500)

SCENARIOS = [
    Scenario("S21 on 15c grid, always on", S21, core.Site(tariff_usd_kwh=0.15), always_on=True,
             note="what happens if you ignore the app"),
    Scenario("S21 on 15c grid, aureon decides", S21, core.Site(tariff_usd_kwh=0.15),
             note="aureon keeps it off when it loses money"),
    Scenario("S21 heating a home (resistive)", S21,
             core.Site(tariff_usd_kwh=0.15, heat_credit_usd_kwh=0.15, heat_fraction_used=1.0),
             seasonal_heat=True, note="replaces electric baseboard heat"),
    Scenario("Used S19 heating a home", S19_USED,
             core.Site(tariff_usd_kwh=0.15, heat_credit_usd_kwh=0.15, heat_fraction_used=1.0),
             seasonal_heat=True, note="cheap hardware, free-ish power in winter"),
    Scenario("S21 on solar surplus", S21,
             core.Site(tariff_usd_kwh=0.15, surplus_kwh_day=60, export_credit_usd_kwh=0.04),
             note="60 kWh/day spare solar, 4c export"),
    Scenario("10x used S21 at 5c industrial", S21_USED,
             core.Site(tariff_usd_kwh=0.05, wear_usd_day=0.50), units=10,
             note="hosting-style power"),
]


@dataclass
class Result:
    scenario: str
    market: str
    days_run: int = 0
    days_total: int = 0
    btc: float = 0.0
    revenue_usd: float = 0.0
    power_usd: float = 0.0
    heat_usd: float = 0.0
    wear_usd: float = 0.0
    capex_usd: float = 0.0
    btc_usd_end: float = 0.0
    monthly: list = field(default_factory=list)

    @property
    def cash_profit(self):
        """sell mined btc each day; ignores the hardware's resale value."""
        return self.revenue_usd + self.heat_usd - self.power_usd - self.wear_usd - self.capex_usd

    @property
    def hodl_value_end(self):
        """keep every mined sat; value it at the end price."""
        return self.btc * self.btc_usd_end + self.heat_usd - self.power_usd - self.wear_usd - self.capex_usd

    @property
    def buy_instead_btc(self):
        out_of_pocket = self.capex_usd + max(self.power_usd + self.wear_usd - self.heat_usd, 0)
        return out_of_pocket / BTC_USD0


def simulate(sc: Scenario, mk: Market, months: int) -> Result:
    days = months * 30
    r = Result(sc.name, mk.name, days_total=days, capex_usd=sc.machine.capex_usd * sc.units)
    month_acc = None
    for d in range(days):
        date = START + dt.timedelta(days=d)
        subsidy = SUBSIDY / 2 if date >= HALVING else SUBSIDY
        difficulty = DIFFICULTY0 * (1 + mk.diff_monthly_growth) ** (d / 30)
        price = BTC_USD0 * (1 + mk.btc_monthly_growth) ** (d / 30)
        hp = core.hashprice_btc_per_th_day(difficulty, subsidy, FEES_BTC)

        site = sc.site
        if sc.seasonal_heat:
            site = replace(site, heat_fraction_used=site.heat_fraction_used * HEATING_SEASON[date.month])
        led = core.daily_ledger(sc.machine, site, hp, price)

        key = date.strftime("%Y-%m")
        if month_acc is None or month_acc["month"] != key:
            month_acc = {"month": key, "sats": 0.0, "net_usd": 0.0, "days_run": 0, "btc_usd": price}
            r.monthly.append(month_acc)

        if sc.always_on or led["run"]:
            n = sc.units
            r.days_run += 1
            r.btc += led["btc_day"] * n
            r.revenue_usd += led["revenue_usd"] * n
            r.power_usd += led["power_cost_usd"] * n
            r.heat_usd += led["heat_credit_usd"] * n
            r.wear_usd += site.wear_usd_day * n
            month_acc["sats"] += led["sats_day"] * n
            month_acc["net_usd"] += led["net_usd"] * n
            month_acc["days_run"] += 1
        r.btc_usd_end = price
    return r


def run_all(months=24):
    return [simulate(sc, mk, months) for sc in SCENARIOS for mk in MARKETS]


def _usd(x):
    return ("-$" if x < 0 else "$") + f"{abs(x):,.0f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    results = run_all(a.months)
    if a.json:
        print(json.dumps([{**{k: v for k, v in r.__dict__.items()},
                           "cash_profit": r.cash_profit, "hodl_value_end": r.hodl_value_end,
                           "buy_instead_btc": r.buy_instead_btc} for r in results], indent=1))
        return
    print(f"{a.months} months from {START}, halving assumed {HALVING}. "
          f"start: btc ${BTC_USD0:,}, difficulty {DIFFICULTY0/1e12:.2f}T\n")
    print(f"aureon api on railway alone: $0 mined, about -{_usd(RAILWAY_USD_MONTH * a.months)} hosting\n")
    hdr = f"{'scenario':34} {'mkt':5} {'run%':>5} {'sats':>11} {'cash P/L':>10} {'hodl P/L':>10} {'vs buy':>7}"
    print(hdr + "\n" + "-" * len(hdr))
    for r in results:
        ratio = r.btc / r.buy_instead_btc if r.buy_instead_btc else float("inf")
        print(f"{r.scenario[:34]:34} {r.market:5} {r.days_run / r.days_total:5.0%} {r.btc * 1e8:11,.0f} "
              f"{_usd(r.cash_profit):>10} {_usd(r.hodl_value_end):>10} {ratio:6.2f}x")


if __name__ == "__main__":
    main()
