"""checksum validation for payout addresses on every supported chain.

a typo in a payout address sends rewards somewhere nobody can spend them, so
the server refuses to start with an invalid one. each chain gets its own env
var; there is deliberately no fallback from one chain to another.

  BTC  bech32/bech32m (bc1...)             PAYOUT_ADDRESS (or PAYOUT_ADDRESS_BTC)
  BCH  cashaddr (bitcoincash:q...)         PAYOUT_ADDRESS_BCH
  LTC  bech32 (ltc1...) or base58check     PAYOUT_ADDRESS_LTC
  DOGE base58check (D... / A... / 9...)    PAYOUT_ADDRESS_DOGE
  KAS  kaspa cashaddr (kaspa:q... / p...)  PAYOUT_ADDRESS_KAS
  XMR  monero base58 + keccak checksum     PAYOUT_ADDRESS_XMR
"""
import hashlib
import os

from payout import DEFAULT_PAYOUT_ADDRESS, is_valid_mainnet_segwit

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


# ---------------------------------------------------------------- bech32 (generic hrp)

def _bech32_polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _segwit_ok(addr, hrp):
    if addr.lower() != addr and addr.upper() != addr:
        return False
    addr = addr.lower()
    if not addr.startswith(hrp + "1") or len(addr) > 90:
        return False
    data = [_B32.find(c) for c in addr[len(hrp) + 1:]]
    if -1 in data or len(data) < 7:
        return False
    const = _bech32_polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data)
    ver = data[0]
    if const != (1 if ver == 0 else 0x2BC830A3) or ver > 16:
        return False
    nbytes = (len(data) - 7) * 5 // 8
    return (nbytes in (20, 32)) if ver == 0 else 2 <= nbytes <= 40


# ---------------------------------------------------------------- base58check

def _b58decode(s):
    n = 0
    for c in s:
        i = _B58.find(c)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def _base58check_version(s):
    raw = _b58decode(s)
    if raw is None or len(raw) != 25:
        return None
    body, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        return None
    return body[0]


def b58check_encode(version: int, payload: bytes) -> str:
    body = bytes([version]) + payload
    raw = body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


# ---------------------------------------------------------------- cashaddr (bch, kaspa)

def _cash_polymod(values):
    gen = (0x98F2BC8E61, 0x79B76D99E2, 0xF33E5FB3C4, 0xAE2EABE2A8, 0x1E4F43E470)
    c = 1
    for d in values:
        c0 = c >> 35
        c = ((c & 0x07FFFFFFFF) << 5) ^ d
        for i in range(5):
            if (c0 >> i) & 1:
                c ^= gen[i]
    return c ^ 1


def _cashaddr_ok(addr, prefix, payload_len_ok):
    if addr.lower() != addr and addr.upper() != addr:
        return False
    addr = addr.lower()
    if ":" not in addr:
        addr = f"{prefix}:{addr}"
    pre, _, payload = addr.partition(":")
    if pre != prefix or len(payload) < 9:
        return False
    data = [_B32.find(c) for c in payload]
    if -1 in data:
        return False
    if _cash_polymod([ord(c) & 31 for c in pre] + [0] + data) != 0:
        return False
    return payload_len_ok(len(data) - 8)


# ---------------------------------------------------------------- monero (keccak-256)

_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _keccak_f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M64) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                r = _ROT[x][y]
                b[y][(2 * x + 3 * y) % 5] = ((a[x][y] << r) | (a[x][y] >> (64 - r))) & _M64 if r else a[x][y]
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    """original keccak (monero), not nist sha3: padding byte 0x01."""
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\0" * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


_XMR_BLOCK = {0: 0, 2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 9: 6, 10: 7, 11: 8}


def _xmr_decode(s):
    out = bytearray()
    for i in range(0, len(s), 11):
        chunk = s[i:i + 11]
        nbytes = _XMR_BLOCK.get(len(chunk)) if len(chunk) < 11 else 8
        if nbytes is None:
            return None
        n = 0
        for c in chunk:
            j = _B58.find(c)
            if j < 0:
                return None
            n = n * 58 + j
        if n >> (8 * nbytes):
            return None
        out += n.to_bytes(nbytes, "big")
    return bytes(out)


def _monero_ok(addr):
    if len(addr) not in (95, 106) or addr[0] not in "48":
        return False
    raw = _xmr_decode(addr)
    if raw is None or len(raw) not in (69, 77):
        return False
    if raw[0] not in (18, 19, 42):   # standard, integrated, subaddress (mainnet)
        return False
    return keccak256(raw[:-4])[:4] == raw[-4:]


# ---------------------------------------------------------------- public api

def _ltc_ok(a):
    return _segwit_ok(a, "ltc") or _base58check_version(a) in (0x30, 0x32, 0x05)


def _doge_ok(a):
    return _base58check_version(a) in (0x1E, 0x16)


VALIDATORS = {
    "BTC": is_valid_mainnet_segwit,
    "BCH": lambda a: _cashaddr_ok(a, "bitcoincash", lambda n: n in (34, 53)),
    "LTC": _ltc_ok,
    "DOGE": _doge_ok,
    "KAS": lambda a: _cashaddr_ok(a, "kaspa", lambda n: n in (53, 55)),
    "XMR": _monero_ok,
}


def is_valid(symbol: str, addr: str) -> bool:
    fn = VALIDATORS.get(symbol)
    return bool(fn and isinstance(addr, str) and addr and fn(addr.strip()))


def configured_addresses() -> dict:
    """symbol -> address for every chain with a payout address set. raises on invalid ones."""
    out = {}
    for sym in VALIDATORS:
        val = os.environ.get(f"PAYOUT_ADDRESS_{sym}")
        if sym == "BTC" and not val:
            val = os.environ.get("PAYOUT_ADDRESS", DEFAULT_PAYOUT_ADDRESS)
        if not val:
            continue
        val = val.strip()
        if not is_valid(sym, val):
            raise ValueError(f"PAYOUT_ADDRESS_{sym} is not a valid {sym} address: {val!r}")
        out[sym] = val
    return out
