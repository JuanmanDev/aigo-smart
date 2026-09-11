"""Regression tests for device status / availability mapping.

The app's BaseDeviceStatus class (classes11.dex) defines:
  NOT_ACTIVATED=0, ONLINE=1, OFFLINE=3, DISABLED=8

listBindingByAccount's "status" field is NOT always a live online flag —
DEPLOYED EVIDENCE (DEBUG_REPORT.md): an online panel light returned
status: 3 there while answering property polls fine. The app itself
verifies via /thing/status/get (apiVer 1.0.5) which returns
{"status": 1} for online devices.

Coordinator policy tested here:
  * status 1 from the list -> online, no verification call
  * ambiguous status (3 etc.) -> verified via /thing/status/get
  * verified online stays cached for the cooldown window
"""
import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Mock homeassistant modules before importing coordinator
ha_mock_ns = {}
for mod in ("homeassistant", "homeassistant.config_entries", "homeassistant.core",
            "homeassistant.helpers", "homeassistant.helpers.update_coordinator"):
    sys.modules.setdefault(mod, mock.MagicMock())


class MockDataUpdateCoordinator:
    def __init__(self, hass, logger, name, update_interval, config_entry=None):
        self.hass = hass
        self.data = {}

    def async_update_listeners(self):
        pass


sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = MockDataUpdateCoordinator
sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = Exception

import importlib.util
spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.const",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                 "custom_components", "aigosmart", "const.py"))
)
const_mod = importlib.util.module_from_spec(spec)
sys.modules["custom_components.aigosmart.const"] = const_mod
spec.loader.exec_module(const_mod)

spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.coordinator",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                 "custom_components", "aigosmart", "coordinator.py"))
)
coord_mod = importlib.util.module_from_spec(spec)
coord_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.coordinator"] = coord_mod
api_mock = mock.MagicMock()
sys.modules["custom_components.aigosmart.api"] = api_mock
spec.loader.exec_module(coord_mod)

AigoDataUpdateCoordinator = coord_mod.AigoDataUpdateCoordinator
is_status_online = const_mod.is_status_online


class TestStatusMapping(unittest.TestCase):
    def test_status_values_from_apk(self):
        # from BaseDeviceStatus: NOT_ACTIVATED=0, ONLINE=1, OFFLINE=3, DISABLED=8
        self.assertEqual(const_mod.STATUS_ONLINE, 1)
        self.assertEqual(const_mod.STATUS_OFFLINE, 3)

    def test_is_status_online(self):
        self.assertTrue(is_status_online(1))
        self.assertTrue(is_status_online("1"))
        self.assertFalse(is_status_online(3))
        self.assertFalse(is_status_online(0))
        self.assertFalse(is_status_online(8))
        self.assertFalse(is_status_online(None))
        self.assertFalse(is_status_online("weird"))


class TestCoordinatorOnlineResolution(unittest.TestCase):
    def setUp(self):
        self.coordinator = AigoDataUpdateCoordinator(mock.MagicMock(), mock.MagicMock())
        self.dev = {"iotId": "iot1", "status": 3}

    def test_status_one_is_online_without_verification(self):
        dev = {"iotId": "iot1", "status": 1}
        self.assertTrue(self.coordinator._resolve_online(dev))
        # no /thing/status/get call needed
        self.coordinator.client.get_status.assert_not_called()

    def test_ambiguous_status_verified_online(self):
        """The reported bug: online device with listBinding status=3."""
        self.coordinator.client.get_status.return_value = {"status": 1}
        self.assertTrue(self.coordinator._resolve_online(self.dev))
        self.coordinator.client.get_status.assert_called_once_with("iot1")

    def test_ambiguous_status_verified_offline(self):
        self.coordinator.client.get_status.return_value = {"status": 3}
        self.assertFalse(self.coordinator._resolve_online(self.dev))

    def test_verified_state_cached_within_cooldown(self):
        self.coordinator.client.get_status.return_value = {"status": 1}
        self.assertTrue(self.coordinator._resolve_online(self.dev))
        # second resolve within cooldown -> no extra API call
        self.assertTrue(self.coordinator.is_device_online("iot1"))
        self.assertEqual(self.coordinator.client.get_status.call_count, 1)

    def test_unknown_device_defaults_available_until_resolved(self):
        # Before the first poll resolves a device, it must not flash
        # unavailable (entity was just created from the device list)
        self.assertTrue(self.coordinator.is_device_online("never_seen"))


class TestLocalFirstInitRegression(unittest.TestCase):
    """The live-production crash: every write failed with
    `'AigoSmartApiClient' object has no attribute '_local_ok'` because
    LocalFirstMixin.__init__ was never called (missing super().__init__()).
    """

    def test_client_has_local_first_state(self):
        """Regression for the live crash: 'AigoSmartApiClient' object has no
        attribute '_local_ok' — LocalFirstMixin.__init__ was never invoked."""
        import importlib.util

        def load(name, relpath):
            spec = importlib.util.spec_from_file_location(
                name,
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                             "custom_components", "aigosmart", relpath))
            )
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = name.rsplit(".", 1)[0]
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod

        base = "custom_components.aigosmart"
        # Register a real (but empty) package module so relative imports
        # resolve without executing the HA-heavy __init__.py.
        pkg = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location(
                base, os.path.abspath(os.path.join(
                    os.path.dirname(__file__), "..", "..",
                    "custom_components", "aigosmart", "__init__.py"))
            )
        )
        # neutralize the HA-heavy imports inside __init__.py
        ha = mock.MagicMock()
        ha.__path__ = []
        prev_ha = sys.modules.get("homeassistant")
        sys.modules["homeassistant"] = ha
        for mod in ("homeassistant.const", "homeassistant.config_entries",
                    "homeassistant.core", "homeassistant.helpers",
                    "homeassistant.helpers.device_registry",
                    "homeassistant.helpers.entity_platform",
                    "homeassistant.helpers.update_coordinator",
                    "homeassistant.exceptions", "homeassistant.components",
                    "homeassistant.components.bluetooth", "voluptuous"):
            sys.modules.setdefault(mod, mock.MagicMock())
        try:
            # Execute the real __init__ so .lib/.local_first resolve normally
            spec = importlib.util.spec_from_file_location(
                base, os.path.abspath(os.path.join(
                    os.path.dirname(__file__), "..", "..",
                    "custom_components", "aigosmart", "__init__.py"))
            )
            pkg = importlib.util.module_from_spec(spec)
            pkg.__path__ = [os.path.abspath(os.path.join(
                os.path.dirname(__file__), "..", "..",
                "custom_components", "aigosmart"))]
            # drop mocks that other test modules may have registered
            for stale in ("custom_components.aigosmart",
                          "custom_components.aigosmart.api",
                          "custom_components.aigosmart.coordinator"):
                sys.modules.pop(stale, None)
            sys.modules[base] = pkg
            # real parent package so `from . import config_validation` resolves
            parent = importlib.util.module_from_spec(
                importlib.util.spec_from_loader("custom_components", None))
            parent.__path__ = [os.path.abspath(os.path.join(
                os.path.dirname(__file__), "..", "..", "custom_components"))]
            sys.modules["custom_components"] = parent
            spec.loader.exec_module(pkg)
            api_mod = sys.modules[f"{base}.api"]
            client = api_mod.AigoSmartApiClient("user@example.com", "pw")
            self.assertTrue(hasattr(client, "_local_ok"))
            self.assertTrue(hasattr(client, "_local_bad"))
            self.assertEqual(client._local_ok, {})
            self.assertEqual(client._local_bad, {})
            # the exact paths that crashed in production:
            # set_properties_prefer_local touches _local_ok on line 1
            try:
                client.set_properties_prefer_local("iot1", {"LightSwitch": 1})
            except Exception as exc:
                # must NOT be AttributeError('_local_ok') — not logged in is fine
                self.assertNotIsInstance(exc, AttributeError)
        finally:
            if prev_ha is not None:
                sys.modules["homeassistant"] = prev_ha


if __name__ == "__main__":
    unittest.main()
