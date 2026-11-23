"""Repair issues for unsupported devices: one per productKey, auto-removed."""
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.modules["voluptuous"] = MagicMock()
ha_mock = MagicMock()
ha_mock.__path__ = []
sys.modules["homeassistant"] = ha_mock
for _mod in (
    "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator", "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.service", "homeassistant.helpers.config_validation",
    "homeassistant.const", "homeassistant.exceptions", "homeassistant.components",
):
    sys.modules[_mod] = MagicMock()
sys.modules["homeassistant.core"].callback = lambda func: func


class FakeIssueRegistry:
    def __init__(self):
        self.issues = {}

    def async_get_issue(self, domain, issue_id):
        return self.issues.get((domain, issue_id))


REGISTRY = FakeIssueRegistry()
CALLS = []


def _create(hass, domain, issue_id, **kwargs):
    CALLS.append(("create", issue_id))
    REGISTRY.issues[(domain, issue_id)] = SimpleNamespace(**kwargs)


def _delete(hass, domain, issue_id):
    CALLS.append(("delete", issue_id))
    REGISTRY.issues.pop((domain, issue_id), None)


_ir = sys.modules["homeassistant.helpers.issue_registry"]
_ir.async_get = lambda hass: REGISTRY
_ir.async_create_issue = _create
_ir.async_delete_issue = _delete
_ir.IssueSeverity = SimpleNamespace(WARNING="warning")
sys.modules["homeassistant.helpers"].issue_registry = _ir

for _cached in [m for m in sys.modules if m.startswith("custom_components.aigosmart.")]:
    del sys.modules[_cached]

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                     "custom_components", "aigosmart"))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.aigosmart.{name}", os.path.join(_BASE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "custom_components.aigosmart"
    sys.modules[f"custom_components.aigosmart.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


unsupported = _load("unsupported")

VACUUM_1 = {"iotId": "v1", "productKey": "pkVac", "categoryKey": "SweepingMachine",
            "productName": "Robot vacuum ", "nickName": "Vacuum upstairs"}
VACUUM_2 = dict(VACUUM_1, iotId="v2", nickName="Vacuum downstairs")
CAMERA = {"iotId": "c1", "productKey": "pkCam", "categoryKey": "Camera", "nickName": "Door cam"}
PLUG = {"iotId": "p1", "productKey": "pkPlug", "categoryKey": "Socket", "nickName": "Plug"}
FOUNTAIN = {"iotId": "f1", "productKey": "a163VQp0e0e", "categoryKey": "petWaterFountain"}
KETTLE = {"iotId": "k1", "productKey": "pkK", "categoryKey": "Kettle"}


def _hass(*device_lists):
    states = {f"entry{i}": {"coordinator": SimpleNamespace(devices=list(devs)), "pk_catalog": {}}
              for i, devs in enumerate(device_lists)}
    return SimpleNamespace(data={"aigosmart": states})


def _issue_ids():
    return sorted(issue_id for (_d, issue_id) in REGISTRY.issues)


class TestUnsupportedIssues(unittest.TestCase):
    def setUp(self):
        REGISTRY.issues.clear()
        CALLS.clear()

    def test_supported_devices_get_no_issue(self):
        for dev in (PLUG, FOUNTAIN, KETTLE):
            self.assertFalse(unsupported.is_unsupported(dev, {}), dev)
        unsupported.async_update_unsupported_issues(_hass([PLUG, FOUNTAIN, KETTLE]))
        self.assertEqual(_issue_ids(), [])

    def test_one_issue_per_product_key(self):
        unsupported.async_update_unsupported_issues(_hass([VACUUM_1, VACUUM_2, CAMERA, PLUG]))
        self.assertEqual(_issue_ids(), ["unsupported_device_pkCam", "unsupported_device_pkVac"])
        vac = REGISTRY.issues[("aigosmart", "unsupported_device_pkVac")]
        self.assertEqual(vac.translation_key, "unsupported_device")
        self.assertEqual(vac.translation_placeholders["name"], "Vacuum upstairs, Vacuum downstairs")
        self.assertEqual(vac.translation_placeholders["category"], "SweepingMachine")
        self.assertEqual(vac.translation_placeholders["product"], "Robot vacuum")
        self.assertIn("device_support.yml", vac.learn_more_url)
        self.assertFalse(vac.is_fixable)

    def test_devices_across_entries(self):
        unsupported.async_update_unsupported_issues(_hass([VACUUM_1], [CAMERA]))
        self.assertEqual(len(_issue_ids()), 2)

    def test_unchanged_issues_are_not_recreated(self):
        hass = _hass([VACUUM_1])
        unsupported.async_update_unsupported_issues(hass)
        unsupported.async_update_unsupported_issues(hass)
        self.assertEqual(CALLS.count(("create", "unsupported_device_pkVac")), 1)

    def test_removed_device_deletes_issue(self):
        unsupported.async_update_unsupported_issues(_hass([VACUUM_1, CAMERA]))
        unsupported.async_update_unsupported_issues(_hass([CAMERA]))
        self.assertEqual(_issue_ids(), ["unsupported_device_pkCam"])

    def test_device_becoming_supported_deletes_issue(self):
        hass = _hass([CAMERA])
        unsupported.async_update_unsupported_issues(hass)
        # a newer version maps the category (simulated by a catalog override)
        hass.data["aigosmart"]["entry0"]["coordinator"].devices = [dict(CAMERA, categoryKey="Socket")]
        unsupported.async_update_unsupported_issues(hass)
        self.assertEqual(_issue_ids(), [])

    def test_other_domains_untouched(self):
        REGISTRY.issues[("other", "unsupported_device_x")] = SimpleNamespace()
        REGISTRY.issues[("aigosmart", "something_else")] = SimpleNamespace()
        unsupported.async_update_unsupported_issues(_hass([]))
        unsupported.async_delete_all_unsupported_issues(_hass([]))
        self.assertIn(("other", "unsupported_device_x"), REGISTRY.issues)
        self.assertIn(("aigosmart", "something_else"), REGISTRY.issues)

    def test_delete_all(self):
        unsupported.async_update_unsupported_issues(_hass([VACUUM_1, CAMERA]))
        unsupported.async_delete_all_unsupported_issues(_hass([]))
        self.assertEqual(_issue_ids(), [])


if __name__ == "__main__":
    unittest.main()
