"""Small shared utilities."""
from __future__ import annotations

import base64
import os
import time


def http_date(ts: float | None = None) -> str:
    """RFC 1123 date string in GMT, matching Java's toGMTString-ish API gateway expectations."""
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ts))


def encrypt_password(password: str, key: str = "tCx8BA0yKVr+NbBChH928URAV90=0000") -> str:
    """AES-256-CBC with zero IV — matches AigoSmart app password encryption.

    Uses `cryptography` if available; falls back to a tiny pure-Python AES
    implementation (slow but dependency-free, good enough for one block).
    """
    key_bytes = key.encode("utf-8")  # 32 bytes
    if len(key_bytes) != 32:
        raise ValueError("AES key must be 32 bytes")
    data = password.encode("utf-8")
    # PKCS7 pad to 16-byte blocks
    pad = 16 - (len(data) % 16)
    data += bytes([pad]) * pad

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(b"\x00" * 16))
        enc = cipher.encryptor()
        ct = enc.update(data) + enc.finalize()
    except ImportError:
        ct = _aes_cbc_zeroiv_pure(key_bytes, data)

    return base64.b64encode(ct).decode("ascii")


# --- pure-python AES (fallback) ----------------------------------------

_SBOX = None


def _sbox() -> list[int]:
    global _SBOX
    if _SBOX is None:
        # Standard AES S-box (computed table is error-prone; use canonical values)
        sbox = [
            0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
            0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
            0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
            0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
            0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
            0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
            0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
            0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
            0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
            0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
            0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
            0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
            0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
            0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
            0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
            0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
        ]
        _SBOX = sbox
    return _SBOX


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _expand_key(key: bytes) -> list[list[int]]:
    sbox = _sbox()
    nk = 8  # AES-256
    w = [list(key[4 * i: 4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * 15):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [sbox[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            t = [sbox[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [w[4 * r: 4 * r + 4] for r in range(15)]


def _aes_encrypt_block(rk: list[list[int]], block: bytes) -> bytes:
    sbox = _sbox()
    s = [list(block[i * 4:i * 4 + 4]) for i in range(4)]
    s = [[s[r][c] for c in range(4)] for r in range(4)]  # col-major -> row-major grid
    # state[r][c] = block[4c + r] in AES spec; keep simple: build state as 4x4 column arrays
    state = [[block[4 * c + r] for c in range(4)] for r in range(4)]

    def add_round_key(state, keys):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= keys[c][r]

    def sub_bytes(state):
        for r in range(4):
            for c in range(4):
                state[r][c] = sbox[state[r][c]]

    def shift_rows(state):
        for r in range(1, 4):
            state[r] = state[r][r:] + state[r][:r]

    def mix_columns(state):
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
            state[1][c] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
            state[2][c] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
            state[3][c] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)

    add_round_key(state, rk[0])
    for rnd in range(1, 14):
        sub_bytes(state)
        shift_rows(state)
        mix_columns(state)
        add_round_key(state, rk[rnd])
    sub_bytes(state)
    shift_rows(state)
    add_round_key(state, rk[14])

    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = state[r][c]
    return bytes(out)


def _aes_cbc_zeroiv_pure(key: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    out = bytearray()
    prev = b"\x00" * 16
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = _aes_encrypt_block(rk, block)
        out += prev
    return bytes(out)


def random_token(n: int = 16) -> str:
    return os.urandom(n).hex()
