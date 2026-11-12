"""payout address handling.

aureon does not hold or move coins. mining rewards are paid by the pool straight
to the address configured on the miner / pool account. this module validates that
address and builds the stratum settings to point hardware at it.
"""
import os

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3

DEFAULT_PAYOUT_ADDRESS = "bc1qwhu7xz88spa56mfvmg0zhtwlr4sny0qel2k2t8"


def _polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _convertbits(data, frombits, tobits):
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return out


def is_valid_mainnet_segwit(addr: str) -> bool:
    """bip173 / bip350 check for a bc1 mainnet address."""
    if not addr or addr.lower() != addr and addr.upper() != addr:
        return False
    addr = addr.lower()
    if not addr.startswith("bc1") or not 14 <= len(addr) <= 90:
        return False
    data = [_CHARSET.find(c) for c in addr[3:]]
    if -1 in data or len(data) < 7:
        return False
    hrp = "bc"
    const = _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data)
    version = data[0]
    if version == 0 and const != _BECH32_CONST:
        return False
    if version != 0 and const != _BECH32M_CONST:
        return False
    prog = _convertbits(data[1:-6], 5, 8)
    if prog is None or not 2 <= len(prog) <= 40 or version > 16:
        return False
    if version == 0 and len(prog) not in (20, 32):
        return False
    return True


def payout_address() -> str:
    addr = os.environ.get("PAYOUT_ADDRESS", DEFAULT_PAYOUT_ADDRESS).strip()
    if not is_valid_mainnet_segwit(addr):
        raise ValueError(f"PAYOUT_ADDRESS is not a valid bc1 mainnet address: {addr!r}")
    return addr


def stratum_config(worker: str = "aureon01") -> dict:
    """settings to enter on each asic. for address-as-username pools (ocean, solo
    pools such as ckpool) rewards go straight to the address. for account-based
    pools (braiins, f2pool, antpool...) set the same address as the payout address
    in the pool dashboard and use your account name as the stratum user."""
    addr = payout_address()
    return {
        "payout_address": addr,
        "address_as_username": {
            "stratum_user": f"{addr}.{worker}",
            "password": "x",
            "example_pools": {
                "ocean": "stratum+tcp://mine.ocean.xyz:3334",
                "ckpool_solo": "stratum+tcp://solo.ckpool.org:3333",
            },
        },
        "account_pools": "set payout address to %s in the pool dashboard" % addr,
    }
