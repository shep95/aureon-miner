"""aureon v2 core: heat-and-surplus mining decision math.

pure functions, no network access. every input is explicit so results can be
reconstructed. money in usd floats for illustration; production ledger should
store integer sats and integer milli-cents.
"""
from dataclasses import dataclass

SATS_PER_BTC = 100_000_000
TWO_32 = 2 ** 32


def hashprice_btc_per_th_day(difficulty: float, subsidy_btc: float, avg_fees_btc: float) -> float:
    """expected btc per th/s per day, computed from chain data (no third party needed)."""
    hashes_per_day = 86_400 * 1e12            # one th/s for one day
    hashes_per_block = difficulty * TWO_32
    return hashes_per_day / hashes_per_block * (subsidy_btc + avg_fees_btc)


@dataclass
class Machine:
    th: float            # measured accepted hashrate, th/s
    watts: float         # measured wall power
    capex_usd: float     # all-in: machine + wiring + ducting + shipping


@dataclass
class Site:
    tariff_usd_kwh: float            # marginal price of the next kwh
    heat_credit_usd_kwh: float = 0.0 # cost of the heat you would otherwise buy, per kwh of heat
    heat_fraction_used: float = 0.0  # share of miner heat that actually displaces heating (0..1)
    surplus_kwh_day: float = 0.0     # solar or other surplus available to the miner
    export_credit_usd_kwh: float = 0.0  # what that surplus would earn if exported instead
    pool_fee: float = 0.02
    wear_usd_day: float = 0.0


def heat_credit_from_system(fuel_price_per_kwh_delivered: float) -> float:
    """helper: pass the cost of 1 kwh of delivered heat from the system the miner replaces.
    resistive electric: tariff / 1.0 ; heat pump: tariff / cop ; gas: price_per_kwh_fuel / furnace_eff."""
    return fuel_price_per_kwh_delivered


def daily_ledger(m: Machine, s: Site, hp_btc_th_day: float, btc_usd: float) -> dict:
    kwh = m.watts * 24 / 1000
    btc = m.th * hp_btc_th_day * (1 - s.pool_fee)
    revenue = btc * btc_usd
    surplus_used = min(kwh, s.surplus_kwh_day)
    grid_kwh = kwh - surplus_used
    power_cost = grid_kwh * s.tariff_usd_kwh + surplus_used * s.export_credit_usd_kwh
    heat_credit = kwh * s.heat_fraction_used * s.heat_credit_usd_kwh
    net = revenue + heat_credit - power_cost - s.wear_usd_day
    return {
        "kwh_day": kwh,
        "btc_day": btc,
        "sats_day": btc * SATS_PER_BTC,
        "revenue_usd": revenue,
        "power_cost_usd": power_cost,
        "heat_credit_usd": heat_credit,
        "net_usd": net,
        "run": net > 0,
    }


def breakeven_tariff(m: Machine, hp_btc_th_day: float, btc_usd: float, pool_fee: float = 0.02) -> float:
    kwh = m.watts * 24 / 1000
    return m.th * hp_btc_th_day * (1 - pool_fee) * btc_usd / kwh


def mine_vs_buy(m: Machine, s: Site, hp_btc_th_day: float, btc_usd: float,
                run_days: int, monthly_hashprice_decay: float = 0.0) -> dict:
    """compares two ways to spend the same dollars.
    mine: pay capex, earn sats, pay net power (after heat/surplus credits).
    buy:  spend capex + any positive net power cost on btc at today's price instead.
    hashprice decay models difficulty growth / halving (fraction per 30 days)."""
    mined_btc = 0.0
    extra_cash = 0.0  # net out-of-pocket for power after credits (positive = cost)
    for d in range(run_days):
        hp = hp_btc_th_day * (1 - monthly_hashprice_decay) ** (d / 30)
        led = daily_ledger(m, s, hp, btc_usd)
        mined_btc += led["btc_day"]
        extra_cash += led["power_cost_usd"] - led["heat_credit_usd"] + s.wear_usd_day
    spend = m.capex_usd + max(extra_cash, 0.0)
    bought_btc = spend / btc_usd
    # if credits exceeded power cost, mining also freed cash; count it as btc bought
    mined_total = mined_btc + max(-extra_cash, 0.0) / btc_usd
    return {
        "mined_btc": mined_total,
        "bought_btc": bought_btc,
        "mining_wins": mined_total > bought_btc,
        "ratio": mined_total / bought_btc if bought_btc else float("inf"),
    }


def max_capex_per_th(hp_btc_th_day: float, btc_usd: float, run_days: int,
                     pool_fee: float = 0.02, monthly_hashprice_decay: float = 0.0) -> float:
    """with power fully offset by heat, the most you can pay per th/s and still beat buying."""
    total = sum(hp_btc_th_day * (1 - monthly_hashprice_decay) ** (d / 30) for d in range(run_days))
    return total * (1 - pool_fee) * btc_usd
