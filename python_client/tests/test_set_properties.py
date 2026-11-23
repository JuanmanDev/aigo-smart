"""aigosmart.set_properties: validation, device resolution, admin registration."""
import asyncio
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

for _mod in (
    "voluptuous", "homeassistant", "homeassistant.config_entries", "homeassistant.core",
    "homeassistant.helpers", "homeassistant.helpers.device_registry",
    "homeassistant.helpers.config_validation", "homeassistant.helpers.service",
    "homeassistant.helpers.entity_platform", "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.issue_registry", "homeassistant.const",
    "homeassistant.exceptions", "homeassistant.components",
):
    sys.modules[_mod] = MagicMock()
sys.modules["homeassistant"].__path__ = []


class Invalid(Exception):
    pass


class ServiceValidationError(Exception):
    pass


class HomeAssistantError(Exception):
    pass


sys.modules["voluptuous"].Invalid = Invalid
sys.modules["homeassistant.exceptions"].ServiceValidationError = ServiceValidationError
sys.modules["homeassistant.exceptions"].HomeAssistantError = HomeAssistantError

for _cached in [m for m in sys.modules if m.startswith("custom_components.aigosmart.")]:
    del sys.modules[_cached]

_spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.set_properties",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                 "custom_components", "aigosmart", "set_properties.py")))
sp = importlib.util.module_from_spec(_spec)
sp.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.set_properties"] = sp
_spec.loader.exec_module(sp)


class TestValidation(unittest.TestCase):
    def test_valid_payloads(self):
        for value in ({"WaterGear": 3}, {"Switch": True},
                      {"HSVColor": {"Hue": 0, "Saturation": 100, "Value": 60}, "LightMode": 1},
                      {"feedingPlan": "7F06000401"}, {"Arr": [1, 2.5, "x"]}):
            self.assertEqual(sp.validate_properties(value), value)

    def test_invalid_payloads(self):
        for value in ({}, [], "x", None, {"": 1}, {"  ": 1}, {1: 2}, {"X": None},
                      {"X": {"a": None}}, {f"p{i}": 1 for i in range(sp.MAX_PROPERTIES + 1)},
                      {"X": [[[[[[1]]]]]]}):
            with self.assertRaises(Invalid, msg=repr(value)):
                sp.validate_properties(value)


def _hass(device, devices):
    registry = MagicMock()
    registry.async_get.side_effect = lambda device_id: device if device_id == "dev1" else None
    sys.modules["homeassistant.helpers.device_registry"].async_get = lambda hass: registry
    sp.dr = sys.modules["homeassistant.helpers.device_registry"]
    client = MagicMock()
    coordinator = MagicMock()
    coordinator.devices = devices
    coordinator.async_refresh_device = AsyncMock()
    hass = MagicMock()
    hass.data = {"aigosmart": {"entry": {"client": client, "coordinator": coordinator}}}

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job.side_effect = _executor
    return hass, client, coordinator


DEVICE = SimpleNamespace(identifiers={("aigosmart", "iot-1")}, name="Fountain", name_by_user=None)


class TestHandler(unittest.TestCase):
    def call(self, hass, device_id="dev1", props=None):
        data = {"device_id": device_id, "properties": props or {"WaterGear": 3}}
        return asyncio.run(sp.async_handle_set_properties(hass, SimpleNamespace(data=data)))

    def test_writes_and_refreshes(self):
        hass, client, coordinator = _hass(DEVICE, [{"iotId": "iot-1"}])
        self.call(hass)
        client.set_properties.assert_called_once_with("iot-1", {"WaterGear": 3})
        coordinator.async_refresh_device.assert_awaited_once_with("iot-1")

    def test_unknown_or_foreign_device(self):
        hass, client, _ = _hass(DEVICE, [{"iotId": "iot-1"}])
        with self.assertRaises(ServiceValidationError):
            self.call(hass, device_id="other")
        foreign = SimpleNamespace(identifiers={("hue", "x")}, name="Lamp", name_by_user=None)
        hass, client, _ = _hass(foreign, [])
        with self.assertRaises(ServiceValidationError):
            self.call(hass)
        client.set_properties.assert_not_called()

    def test_device_not_in_loaded_account(self):
        hass, client, _ = _hass(DEVICE, [{"iotId": "someone-else"}])
        with self.assertRaises(ServiceValidationError):
            self.call(hass)

    def test_cloud_error_is_reported(self):
        hass, client, _ = _hass(DEVICE, [{"iotId": "iot-1"}])
        client.set_properties.side_effect = RuntimeError("code 5092 property not found")
        with self.assertRaises(HomeAssistantError):
            self.call(hass)


class TestRegistration(unittest.TestCase):
    def test_admin_only_and_idempotent(self):
        register = MagicMock()
        sp.async_register_admin_service = register
        hass = MagicMock()
        hass.services.has_service.return_value = False
        sp.async_register(hass)
        args, kwargs = register.call_args
        self.assertEqual(args[1:3], ("aigosmart", "set_properties"))
        self.assertIs(kwargs["schema"], sp.SCHEMA)
        hass.services.has_service.return_value = True
        sp.async_register(hass)
        self.assertEqual(register.call_count, 1)


if __name__ == "__main__":
    unittest.main()
