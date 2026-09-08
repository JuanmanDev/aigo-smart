"""Unit tests for the pure-python helpers (no network)."""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aigosmart.util import encrypt_password, _aes_cbc_zeroiv_pure
from aigosmart.local import build_coap, parse_coap, COAP_CONFIRMABLE, CODE_POST


class TestUtil(unittest.TestCase):
    def test_encrypt_password_length(self):
        ct = encrypt_password("hunter2secret")
        raw = base64.b64decode(ct)
        self.assertEqual(len(raw) % 16, 0)

    def test_pure_aes_matches_cryptography(self):
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            self.skipTest("cryptography not installed")
        key = b"tCx8BA0yKVr+NbBChH928URAV90=0000"
        data = b"0123456789abcdef"
        cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))
        enc = cipher.encryptor()
        ref = enc.update(data) + enc.finalize()
        pure = _aes_cbc_zeroiv_pure(key, data)
        self.assertEqual(ref, pure)

    def test_encrypt_password_matches_reference(self):
        """Cross-check pure fallback against cryptography library result."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding

        key = b"tCx8BA0yKVr+NbBChH928URAV90=0000"
        pw = "secret123"
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(pw.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))
        enc = cipher.encryptor()
        ref = enc.update(padded) + enc.finalize()
        self.assertEqual(base64.b64decode(encrypt_password(pw)), ref)


class TestCoap(unittest.TestCase):
    def test_roundtrip(self):
        payload = b'{"id":1,"method":"core.service.dev","params":{}}'
        pkt = build_coap(COAP_CONFIRMABLE, CODE_POST, 0x1234, bytes(4),
                         "/dev/core/service/dev", payload)
        msg = parse_coap(pkt)
        self.assertEqual(msg["code"], CODE_POST)
        self.assertEqual(msg["msg_id"], 0x1234)
        self.assertEqual(msg["payload"], payload)
        uris = [v for n, v in msg["options"] if n == 11]
        self.assertEqual(list(uris), [b"dev", b"core", b"service", b"dev"])


if __name__ == "__main__":
    unittest.main()
