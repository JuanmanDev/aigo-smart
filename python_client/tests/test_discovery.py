"""Unit tests for autodiscovery mapping (no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python_client"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# discovery.py imports nothing HA-specific at module level
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "discovery", os.path.join(os.path.dirname(__file__), "..", "..",
                              "custom_components", "aigosmart", "discovery.py"))
discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discovery)


class TestCatalog(unittest.TestCase):
    def test_load_pk_catalog(self):
        cat = discovery.load_pk_catalog()
        self.assertGreater(len(cat), 400)
        # sample entries from the CSV
        self.assertEqual(cat.get("a1lu9TUm34D", "").lower(), "curtainswitch")
        self.assertEqual(cat.get("a1L836QOWgx", "").lower(), "waterdispenser")

    def test_platform_for_lamp(self):
        cat = discovery.load_pk_catalog()
        dev = {"iotId": "x", "productKey": "a1JLKGWCSzO"}  # G45 BLE Mesh Lamp
        self.assertEqual(discovery.platform_for_device(dev, cat), "light")

    def test_platform_for_socket(self):
        cat = discovery.load_pk_catalog()
        dev = {"iotId": "x", "productKey": "a1EKtUmsBAD"}  # 意式墙壁插座
        self.assertEqual(discovery.platform_for_device(dev, cat), "switch")

    def test_platform_category_key_priority(self):
        dev = {"iotId": "x", "categoryKey": "AirConditioner", "productKey": "whatever"}
        self.assertEqual(discovery.platform_for_device(dev, {}), "climate")

    def test_registry_register(self):
        r = discovery.DeviceRegistry()
        r.register("light")
        r._known["light"].add("a")
        r.register("light")  # idempotent: keeps what is known
        self.assertEqual(r._known["light"], {"a"})


if __name__ == "__main__":
    unittest.main()
