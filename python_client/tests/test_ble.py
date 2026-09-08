"""Tests for the BLE identification rules."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aigosmart.ble_scanner import is_aigo_device


class FakeAdv:
    def __init__(self, uuids=(), local_name=None, manufacturer_data=None, rssi=-60):
        self.service_uuids = uuids
        self.local_name = local_name
        self.manufacturer_data = manufacturer_data or {}
        self.rssi = rssi


class FakeDev:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name=None):
        self.address = address
        self.name = name


class TestBleScanner(unittest.TestCase):
    def test_breeze_pairing_device_detected(self):
        dev = FakeDev()
        adv = FakeAdv(uuids=["0000feb3-0000-1000-8000-00805f9b34fb"],
                      local_name="AigoLight")
        info = is_aigo_device(dev, adv)
        self.assertIsNotNone(info)
        self.assertTrue(info["pairing"])
        self.assertEqual(info["address"], "AA:BB:CC:DD:EE:FF")

    def test_product_key_in_name_detected(self):
        dev = FakeDev(name="aigo a1JLKGWCSzO")
        adv = FakeAdv(local_name="aigo a1JLKGWCSzO")
        info = is_aigo_device(dev, adv)
        self.assertIsNotNone(info)
        self.assertEqual(info["product_key"], "a1JLKGWCSzO")
        self.assertFalse(info["pairing"])

    def test_product_key_in_manufacturer_data(self):
        dev = FakeDev()
        adv = FakeAdv(manufacturer_data={0x0819: b"a1EKtUmsBAD"})
        info = is_aigo_device(dev, adv)
        self.assertIsNotNone(info)
        self.assertEqual(info["product_key"], "a1EKtUmsBAD")

    def test_unrelated_device_ignored(self):
        dev = FakeDev(name="Kitchen TV")
        adv = FakeAdv(uuids=["0000fe9f-0000-1000-8000-00805f9b34fb"],
                      local_name="Kitchen TV")
        self.assertIsNone(is_aigo_device(dev, adv))

    def test_short_int_uuid_breeze(self):
        dev = FakeDev()
        adv = FakeAdv(uuids=[0xFEB3], local_name="x")
        info = is_aigo_device(dev, adv)
        self.assertIsNotNone(info)
        self.assertTrue(info["pairing"])


if __name__ == "__main__":
    unittest.main()
