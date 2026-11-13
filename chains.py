"""multi-chain revenue engine.

generic proof-of-work maths for any chain, merged mining (scrypt: ltc + doge),
profit switching between chains that share an algorithm (sha-256: btc vs bch),
staking yield for proof-of-stake chains, and fleet sizing with a per-chain
daily revenue cap.

snapshot defaults are mid-november 2026 public figures; every value can be
overridden per request. pure functions, no network access.
"""
from dataclasses import dataclass, field, replace

DEFAULT_CAP_USD_DAY = 3000.0
DAYS = 86_400


@dataclass(frozen=True)
class PowChain:
    symbol: str
    name: str
    algorithm: str
    block_time_s: float
    block_reward: float          # coins per block, today
    fees_per_block: float        # coins per block, average
    network_hashrate: float      # H/s
    price_usd: float
    reward_cut_monthly: float = 0.0   # smooth emission decay (kaspa)
    halving_in_days: int | None = None  # next step halving, if any
    pools: tuple = ()

    def coins_per_day(self, day: int = 0) -> float:
        reward = self.block_reward * (1 - self.reward_cut_monthly) ** (day / 30)
        if self.halving_in_days is not None and day >= self.halving_in_days:
            reward /= 2
        return DAYS / self.block_time_s * (reward + self.fees_per_block)

    def usd_per_hash_day(self, day: int = 0) -> float:
        return self.coins_per_day(day) * self.price_usd / self.network_hashrate


@dataclass(frozen=True)
class Hardware:
    name: str
    algorithm: str
    hashrate: float      # H/s
    watts: float
    capex_usd: float


@dataclass(frozen=True)
class Target:
    """what one machine mines at once. merged targets credit every chain."""
    id: str
    name: str
    algorithm: str
    chains: tuple            # symbols mined together (merged mining)
    alternatives: tuple = () # symbols the same hardware can switch to


@dataclass(frozen=True)
class StakeChain:
    symbol: str
    name: str
    apr: float               # nominal, before any provider commission
    price_usd: float


T, G, K, P, E = 1e12, 1e9, 1e3, 1e15, 1e18

CHAINS = {c.symbol: c for c in [
    PowChain("BTC", "Bitcoin", "sha256", 600, 3.125, 0.0228, 148.36e12 * 2**32 / 600, 74_624,
             halving_in_days=505, pools=("stratum+tcp://mine.ocean.xyz:3334",
                                         "stratum+tcp://solo.ckpool.org:3333")),
    PowChain("BCH", "Bitcoin Cash", "sha256", 600, 3.125, 0.002, 3.0 * E, 200,
             halving_in_days=505, pools=("stratum+tcp://bch.viabtc.io:3333",)),
    PowChain("LTC", "Litecoin", "scrypt", 150, 6.25, 0.01, 2.8 * P, 52,
             halving_in_days=250, pools=("stratum+tcp://ltc.viabtc.io:3333",
                                         "stratum+tcp://litecoinpool.org:3333")),
    PowChain("DOGE", "Dogecoin", "scrypt", 60, 10_000, 1, 3.0 * P, 0.068),
    PowChain("KAS", "Kaspa", "kheavyhash", 0.1, 1.945, 0.0, 340 * P, 0.029,
             reward_cut_monthly=1 - 0.5 ** (1 / 12),
             pools=("stratum+tcp://kas.2miners.com:2020",)),
    PowChain("XMR", "Monero", "randomx", 120, 0.6, 0.0008, 7.05 * G, 600,
             pools=("stratum+tcp://pool.supportxmr.com:3333",)),
]}

HARDWARE = {h.algorithm: h for h in [
    Hardware("Antminer S21 (234 TH/s)", "sha256", 234 * T, 3510, 3000),
    Hardware("Antminer L9 (16 GH/s)", "scrypt", 16 * G, 3360, 4500),
    Hardware("Antminer KS7 (40 TH/s)", "kheavyhash", 40 * T, 3080, 2500),
    Hardware("Ryzen 9 7950X rig (22 kH/s)", "randomx", 22 * K, 180, 1500),
]}

TARGETS = {t.id: t for t in [
    Target("sha256", "Bitcoin (switching to BCH when it pays more)", "sha256", ("BTC",), ("BCH",)),
    Target("scrypt", "Litecoin + Dogecoin (merged)", "scrypt", ("LTC", "DOGE")),
    Target("kheavyhash", "Kaspa", "kheavyhash", ("KAS",)),
    Target("randomx", "Monero", "randomx", ("XMR",)),
]}

STAKING = {s.symbol: s for s in [
    StakeChain("ETH", "Ethereum", 0.028, 3_400),
    StakeChain("SOL", "Solana", 0.058, 160),
    StakeChain("AVAX", "Avalanche", 0.0668, 22),
    StakeChain("ADA", "Cardano", 0.0223, 0.55),
]}


# ---------------------------------------------------------------- per-machine maths

def machine_day(hw: Hardware, target: Target, chains: dict, tariff_usd_kwh: float,
                pool_fee: float = 0.02, day: int = 0) -> dict:
    """one machine, one day. picks the best chain for switchable algorithms."""
    if hw.algorithm != target.algorithm:
        raise ValueError(f"{hw.name} mines {hw.algorithm}, not {target.algorithm}")
    merged = sum(chains[s].usd_per_hash_day(day) for s in target.chains)
    best_symbols, best = target.chains, merged
    for alt in target.alternatives:
        v = chains[alt].usd_per_hash_day(day)
        if v > best:
            best_symbols, best = (alt,), v
    revenue = hw.hashrate * best * (1 - pool_fee)
    power = hw.watts * 24 / 1000 * tariff_usd_kwh
    return {"revenue_usd": revenue, "power_usd": power, "net_usd": revenue - power,
            "mining": list(best_symbols), "run": revenue > power}


def breakeven_tariff(hw, target, chains, pool_fee=0.02, day=0) -> float:
    rev = machine_day(hw, target, chains, 0.0, pool_fee, day)["revenue_usd"]
    return rev / (hw.watts * 24 / 1000)


# ---------------------------------------------------------------- fleet sizing

def plan_pow(target_id: str, tariff_usd_kwh: float, cap_usd_day: float = DEFAULT_CAP_USD_DAY,
             chains: dict | None = None, hardware: Hardware | None = None, pool_fee: float = 0.02,
             horizon_days: int = 365, difficulty_growth_monthly: float = 0.02) -> dict:
    """size a fleet so gross revenue reaches, but never exceeds, cap_usd_day."""
    chains = chains or CHAINS
    target = TARGETS[target_id]
    hw = hardware or HARDWARE[target.algorithm]
    today = machine_day(hw, target, chains, tariff_usd_kwh, pool_fee)
    units = int(cap_usd_day // today["revenue_usd"]) if today["revenue_usd"] > 0 else 0

    # horizon: scheduled emission cuts + difficulty growth; idle on losing days
    net_h, rev_h, days_on = 0.0, 0.0, 0
    for d in range(horizon_days):
        grown = {s: replace(c, network_hashrate=c.network_hashrate * (1 + difficulty_growth_monthly) ** (d / 30))
                 for s, c in chains.items()}
        m = machine_day(hw, target, grown, tariff_usd_kwh, pool_fee, d)
        if m["run"]:
            net_h += m["net_usd"]
            rev_h += m["revenue_usd"]
            days_on += 1
    capex = units * hw.capex_usd
    fleet_net_h = units * net_h
    profit = fleet_net_h - capex
    if today["net_usd"] <= 0:
        verdict = "do not build: loses money on power alone"
    elif profit <= 0:
        verdict = f"do not build: does not repay hardware within {horizon_days} days"
    else:
        verdict = "build: repays hardware within the horizon"
    return {
        "verdict": verdict,
        "target": target.name,
        "algorithm": target.algorithm,
        "hardware": hw.name,
        "mining_today": today["mining"],
        "units": units,
        "capex_usd": capex,
        "power_kw": units * hw.watts / 1000,
        "revenue_usd_day": units * today["revenue_usd"],
        "power_usd_day": units * today["power_usd"],
        "net_usd_day": units * today["net_usd"],
        "breakeven_tariff_usd_kwh": breakeven_tariff(hw, target, chains, pool_fee),
        "horizon_days": horizon_days,
        "horizon_days_running": days_on,
        "horizon_revenue_usd": units * rev_h,
        "horizon_net_usd": fleet_net_h,
        "horizon_profit_after_capex_usd": profit,
        "payback_days": (capex / (fleet_net_h / horizon_days)) if fleet_net_h > 0 else None,
        "cap_usd_day": cap_usd_day,
    }


def plan_stake(symbol: str, cap_usd_day: float = DEFAULT_CAP_USD_DAY, commission: float = 0.0,
               stakes: dict | None = None) -> dict:
    s = (stakes or STAKING)[symbol]
    net_apr = s.apr * (1 - commission)
    capital = cap_usd_day * 365 / net_apr if net_apr > 0 else float("inf")
    return {"chain": s.name, "symbol": symbol, "apr": s.apr, "net_apr": net_apr,
            "capital_usd": capital, "coins": capital / s.price_usd,
            "revenue_usd_day": cap_usd_day, "cap_usd_day": cap_usd_day,
            "note": "yield is paid in the staked coin; its price risk dwarfs the yield"}


def plan_all(tariff_usd_kwh: float = 0.06, cap_usd_day: float = DEFAULT_CAP_USD_DAY, **kw) -> dict:
    return {"tariff_usd_kwh": tariff_usd_kwh, "cap_usd_day": cap_usd_day,
            "pow": [plan_pow(t, tariff_usd_kwh, cap_usd_day, **kw) for t in TARGETS],
            "stake": [plan_stake(s, cap_usd_day) for s in STAKING]}
