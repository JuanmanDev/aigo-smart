"""Tests for the Breeze provisioning protocol (TLV + scan record parsing)."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aigosmart.breeze_provision import (  # noqa: E402
    CMD_SYS,
    _breeze_message,
    _parse_breeze_response,
    parse_scan_record,
    tlv_encode,
    tlv_parse,
    TLV_DEVICE_NAME,
    TLV_PRODUCT_KEY,
    TLV_RANDOM,
    TLV_SIGN,
)


class TestTLV(unittest.TestCase):
    def test_roundtrip(self):
        elements = [
            (TLV_PRODUCT_KEY, b"a1mevewbgC3"),
            (TLV_DEVICE_NAME, b"testDev1"),
            (TLV_RANDOM, b"\x01\x02\x03\x04"),
            (TLV_SIGN, b"\xaa\xbb"),
        ]
        encoded = tlv_encode(elements)
        decoded = tlv_parse(encoded)
        self.assertEqual(decoded[TLV_PRODUCT_KEY], b"a1mevewbgC3")
        self.assertEqual(decoded[TLV_DEVICE_NAME], b"testDev1")
        self.assertEqual(decoded[TLV_RANDOM], b"\x01\x02\x03\x04")
        self.assertEqual(decoded[TLV_SIGN], b"\xaa\xbb")

    def test_empty_values(self):
        """get_device_info sends TLVs with empty values (length 0)."""
        encoded = tlv_encode([(TLV_PRODUCT_KEY, b""), (TLV_DEVICE_NAME, b"")])
        self.assertEqual(encoded, bytes([TLV_PRODUCT_KEY, 0, TLV_DEVICE_NAME, 0]))
        decoded = tlv_parse(encoded)
        self.assertEqual(decoded, {TLV_PRODUCT_KEY: b"", TLV_DEVICE_NAME: b""})


class TestBreezeMessage(unittest.TestCase):
    def test_single_pdu_framing(self):
        payload = tlv_encode([(TLV_PRODUCT_KEY, b"a1abc")])
        msg = _breeze_message(CMD_SYS, payload)
        # cmd | flags | len(BE16) | payload
        self.assertEqual(msg[0], CMD_SYS)
        self.assertEqual(msg[1], 0)
        self.assertEqual(struct.unpack(">H", msg[2:4])[0], len(payload))
        self.assertEqual(msg[4:], payload)

    def test_response_parse(self):
        payload = tlv_encode([(TLV_PRODUCT_KEY, b"a1test1234")])
        raw = bytes([CMD_SYS, 0]) + struct.pack(">H", len(payload)) + payload
        parsed = _parse_breeze_response(raw)
        self.assertEqual(parsed[0], CMD_SYS)
        self.assertEqual(tlv_parse(parsed[1])[TLV_PRODUCT_KEY], b"a1test1234")

    def test_oversize_rejected(self):
        with self.assertRaises(ValueError):
            _breeze_message(CMD_SYS, b"x" * 20)


class TestScanRecord(unittest.TestCase):
    def _build(self, version=3, fmsk=0x20, mid=b"\x34\x12", sign=b"\x01\x02\x03\x04",
               seq=b"\x78\x56\x34\x12"):
        return bytes([version | (0 << 4), fmsk]) + mid + \
            bytes.fromhex("112233445566") + sign + seq

    def test_parse_secure_broadcast(self):
        raw = self._build()
        rec = parse_scan_record(raw)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["version"], 3)
        self.assertTrue(rec["secure"])
        self.assertEqual(rec["mid"], 0x1234)
        self.assertEqual(rec["mid_hex"], "1234")
        self.assertEqual(rec["mac"], "11:22:33:44:55:66")
        self.assertEqual(rec["sign"], "01020304")
        self.assertEqual(rec["seq"], 0x12345678)

    def test_parse_version4_4byte_mid(self):
        raw = bytes([4, 0x20]) + struct.pack("<I", 0xAABBCCDD) + \
            bytes.fromhex("112233445566") + b"\x00" * 8
        rec = parse_scan_record(raw)
        self.assertEqual(rec["mid"], 0xAABBCCDD)

    def test_parse_insecure_short(self):
        raw = bytes([3, 0x00]) + b"\x34\x12" + bytes.fromhex("112233445566")
        rec = parse_scan_record(raw)
        self.assertIsNotNone(rec)
        self.assertFalse(rec["secure"])
        self.assertEqual(rec["sign"], "")

    def test_reject_short_garbage(self):
        self.assertIsNone(parse_scan_record(b"\x03\x20\x01"))
        self.assertIsNone(parse_scan_record(None))
        self.assertIsNone(parse_scan_record(bytes([1])))  # version < 3


if __name__ == "__main__":
    unittest.main()
