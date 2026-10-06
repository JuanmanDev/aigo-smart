"""Unit tests for AigoSmart aquarium components (switch isolation, RGB light, select entities)."""
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "python_client")))

# Setup Home Assistant mock modules
sys.modules["voluptuous"] = MagicMock()
ha_mock = MagicMock()
ha_mock.__path__ = []
sys.modules["homeassistant"] = ha_mock
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.device_registry"] = MagicMock()
sys.modules["homeassistant.helpers.entity_platform"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.exceptions"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.light"] = MagicMock()
sys.modules["homeassistant.components.switch"] = MagicMock()
sys.modules["homeassistant.components.select"] = MagicMock()

from enum import IntFlag, StrEnum


class ColorMode(StrEnum):
    UNKNOWN = "unknown"
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    HS = "hs"


class LightEntityFeature(IntFlag):
    EFFECT = 4


sys.modules["homeassistant.components.light"].ColorMode = ColorMode
sys.modules["homeassistant.components.light"].LightEntityFeature = LightEntityFeature
sys.modules["homeassistant.components.light"].ATTR_BRIGHTNESS = "brightness"
sys.modules["homeassistant.components.light"].ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
sys.modules["homeassistant.components.light"].ATTR_HS_COLOR = "hs_color"
sys.modules["homeassistant.components.light"].ATTR_EFFECT = "effect"


class MockEntity:
    def __init__(self):
        pass

    def async_write_ha_state(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


class MockCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


sys.modules["homeassistant.components.light"].LightEntity = MockEntity
sys.modules["homeassistant.components.switch"].SwitchEntity = MockEntity
sys.modules["homeassistant.components.select"].SelectEntity = MockEntity
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = MockCoordinatorEntity

import importlib.util

# Load switch
switch_spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.switch",
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "custom_components",
            "aigosmart",
            "switch.py",
        )
    ),
)
switch_mod = importlib.util.module_from_spec(switch_spec)
switch_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.switch"] = switch_mod
switch_spec.loader.exec_module(switch_mod)

# Load light
light_spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.light",
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "custom_components",
            "aigosmart",
            "light.py",
        )
    ),
)
light_mod = importlib.util.module_from_spec(light_spec)
light_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.light"] = light_mod
light_spec.loader.exec_module(light_mod)

# Load select
select_spec = importlib.util.spec_from_file_location(
    "custom_components.aigosmart.select",
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "custom_components",
            "aigosmart",
            "select.py",
        )
    ),
)
select_mod = importlib.util.module_from_spec(select_spec)
select_mod.__package__ = "custom_components.aigosmart"
sys.modules["custom_components.aigosmart.select"] = select_mod
select_spec.loader.exec_module(select_mod)


class TestAquariumIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = MagicMock()
        self.coordinator.props = {
            "aquarium_iot_1": {
                "powerstate": 1,
                "LightSwitch": 0,
                "brightness": 100,
                "childLockOnOff": 0,
                "CueSound": 1,
                "IndMode": 1,
                "feedingFunction": 0,
                "feedCntRemind": 0,
                "DeviceRhythmEnable": 0,
                "waterPumpLevel": 1,
                "waterPumpStatus": 0,
                "LightSceneID": 3,
                "LightScene": {
                    "LightMode": 0,
                    "SceneMode": 0,
                    "ColorArr": '[{"Hue":0,"Saturation":0,"Value":0}]',
                },
            }
        }
        self.coordinator.devices = [
            {
                "iotId": "aquarium_iot_1",
                "productKey": "a191KWgv5BZ",
                "deviceName": "Aigo Aquarium",
                "categoryKey": "aquarium",
                "status": 1,
            }
        ]
        self.coordinator.client = MagicMock()
        self.coordinator.last_update_success = True
        self.coordinator.is_device_online = MagicMock(return_value=True)
        self.coordinator.async_set_optimistic_props = MagicMock()
        self.dev = self.coordinator.devices[0]

    async def test_aquarium_switches_strict_property_isolation(self):
        """Verify switches do NOT crosstalk or bind to LightSwitch."""
        power_sw = switch_mod.AigoSmartAquariumPower(self.coordinator, self.dev)
        buzzer_sw = switch_mod.AigoSmartAquariumBuzzer(self.coordinator, self.dev)
        lock_sw = switch_mod.AigoSmartAquariumChildLock(self.coordinator, self.dev)
        ind_sw = switch_mod.AigoSmartAquariumIndicator(self.coordinator, self.dev)
        feed_sw = switch_mod.AigoSmartAquariumFeedProtect(self.coordinator, self.dev)

        power_sw.hass = MagicMock()
        buzzer_sw.hass = MagicMock()
        lock_sw.hass = MagicMock()

        # Check initial states according to setup props:
        # powerstate=1 -> True
        # CueSound=1 -> True
        # childLockOnOff=0 -> False
        # IndMode=1 -> True
        # feedingFunction=0 -> False
        # Crucially: LightSwitch is 0, but power_sw and buzzer_sw must be ON!
        self.assertTrue(power_sw.is_on)
        self.assertTrue(buzzer_sw.is_on)
        self.assertFalse(lock_sw.is_on)
        self.assertTrue(ind_sw.is_on)
        self.assertFalse(feed_sw.is_on)

        # Verify property keys are preserved and NOT overridden to LightSwitch
        self.assertEqual(power_sw._prop, "powerstate")
        self.assertEqual(buzzer_sw._prop, "CueSound")
        self.assertEqual(lock_sw._prop, "childLockOnOff")
        self.assertEqual(ind_sw._prop, "IndMode")
        self.assertEqual(feed_sw._prop, "feedingFunction")

        # Turn off Buzzer: verify it commands ONLY CueSound: 0
        buzzer_sw.hass.async_add_executor_job = AsyncMock()
        await buzzer_sw.async_turn_off()
        self.assertFalse(buzzer_sw.is_on)
        self.coordinator.async_set_optimistic_props.assert_called_with(
            "aquarium_iot_1", {"CueSound": 0}, hold_duration=5.0
        )
        call_args = buzzer_sw.hass.async_add_executor_job.call_args[0]
        # client.set_properties called with iot_id and {"CueSound": 0}
        self.assertEqual(call_args[2], {"CueSound": 0})

        # Power switch must still be ON!
        self.assertTrue(power_sw.is_on)

    async def test_aquarium_light_rgb_and_scenes(self):
        """Verify aquarium light supports HS color and scene effects."""
        light = light_mod.AigoSmartAquariumLight(self.coordinator, self.dev)
        light.hass = MagicMock()
        light.hass.async_add_executor_job = AsyncMock()

        self.assertIn(ColorMode.HS, light.supported_color_modes)
        self.assertEqual(light.color_mode, ColorMode.HS)
        self.assertIn("Scene 1", light.effect_list)
        self.assertEqual(len(light.effect_list), 12)
        # Initial LightSceneID is 3 -> effect is Scene 3
        self.assertEqual(light.effect, "Scene 3")

        # 1. Turn on with HS color
        await light.async_turn_on(hs_color=(210.0, 75.0), brightness=200)
        self.assertTrue(light.is_on)
        self.assertEqual(light.hs_color, (210.0, 75.0))
        self.assertIsNone(light.effect)

        call_args = light.hass.async_add_executor_job.call_args[0]
        cmd = call_args[2]
        self.assertEqual(cmd["LightSwitch"], 1)
        self.assertEqual(cmd["brightness"], 78)  # 200/255*100
        self.assertIn("LightScene", cmd)
        scene = cmd["LightScene"]
        self.assertEqual(scene["LightMode"], 1)  # Color mode
        self.assertEqual(scene["SceneMode"], 0)  # Static
        color_arr = json.loads(scene["ColorArr"])
        self.assertEqual(color_arr[0]["Hue"], 210)
        self.assertEqual(color_arr[0]["Saturation"], 75)

        # 2. Turn on with effect (Scene 5)
        await light.async_turn_on(effect="Scene 5")
        self.assertEqual(light.effect, "Scene 5")
        call_args = light.hass.async_add_executor_job.call_args[0]
        cmd = call_args[2]
        self.assertEqual(cmd["LightSceneID"], 5)

    async def test_aquarium_pump_and_scene_select(self):
        """Verify water pump speed select and scene select entities."""
        pump_select = select_mod.AigoSmartAquariumPumpSelect(self.coordinator, self.dev)
        scene_select = select_mod.AigoSmartAquariumSceneSelect(self.coordinator, self.dev)

        pump_select.hass = MagicMock()
        pump_select.hass.async_add_executor_job = AsyncMock()
        scene_select.hass = MagicMock()
        scene_select.hass.async_add_executor_job = AsyncMock()

        # Initial pump option for waterPumpLevel=1
        self.assertEqual(pump_select.current_option, "Level 1")
        self.assertEqual(
            pump_select.options, ["Off", "Level 1", "Level 2", "Level 3", "Paused"]
        )

        # Select Level 3
        await pump_select.async_select_option("Level 3")
        self.assertEqual(pump_select.current_option, "Level 3")
        call_args = pump_select.hass.async_add_executor_job.call_args[0]
        self.assertEqual(call_args[2], {"waterPumpLevel": 3})

        # Select Off
        await pump_select.async_select_option("Off")
        self.assertEqual(pump_select.current_option, "Off")
        call_args = pump_select.hass.async_add_executor_job.call_args[0]
        self.assertEqual(call_args[2], {"waterPumpLevel": 0})

        # Select Paused
        await pump_select.async_select_option("Paused")
        self.assertEqual(pump_select.current_option, "Paused")
        call_args = pump_select.hass.async_add_executor_job.call_args[0]
        self.assertEqual(call_args[2], {"waterPumpLevel": 255})

        # Initial scene option for LightSceneID=3
        self.assertEqual(scene_select.current_option, "Scene 3")
        await scene_select.async_select_option("Scene 8")
        self.assertEqual(scene_select.current_option, "Scene 8")
        call_args = scene_select.hass.async_add_executor_job.call_args[0]
        self.assertEqual(call_args[2], {"LightSceneID": 8})


if __name__ == "__main__":
    unittest.main()
