"""Tests for the colour model (adapted from hass-aigosmart's engine)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "custom_components", "aigosmart"))

from color_model import (  # noqa: E402
    ENCODING_HSV,
    ColorSpec,
    ModeSpec,
    as_struct,
    color_spec_from_props,
    color_spec_from_tsl,
)


class TestColorModel(unittest.TestCase):
    def test_hsv_roundtrip(self):
        spec = ColorSpec(
            identifier="HSVColor",
            encoding=ENCODING_HSV,
            members={"hue": "Hue", "saturation": "Saturation", "value": "Value"},
            maxima={"hue": 360.0, "saturation": 100.0, "value": 100.0},
        )
        raw = spec.build(120.0, 50.0, 80.0)
        self.assertEqual(raw["Hue"], 120)
        self.assertEqual(raw["Saturation"], 50)
        self.assertEqual(raw["Value"], 80)
        hs = spec.to_hs(raw)
        self.assertAlmostEqual(hs[0], 120.0, places=0)
        self.assertAlmostEqual(hs[1], 50.0, places=0)

    def test_16bit_mesh_ranges(self):
        """BLE mesh devices use 0-65535 for hue/sat — must rescale."""
        spec = ColorSpec(
            identifier="colorData",
            encoding=ENCODING_HSV,
            members={"hue": "h", "saturation": "s", "value": "v"},
            maxima={"hue": 65535.0, "saturation": 65535.0, "value": 65535.0},
        )
        raw = spec.build(180.0, 100.0, 100.0)
        self.assertEqual(raw["h"], 32768)  # 180/360 * 65535
        hs = spec.to_hs(raw)
        self.assertAlmostEqual(hs[0], 180.0, delta=1.0)
        self.assertAlmostEqual(hs[1], 100.0, delta=1.0)

    def test_spec_from_tsl(self):
        tsl = {
            "properties": [
                {"identifier": "Brightness", "dataType": {"type": "int", "specs": {"max": 100}}},
                {"identifier": "HSVColor", "dataType": {
                    "type": "struct",
                    "specs": [
                        {"identifier": "Hue", "dataType": {"type": "int", "specs": {"max": 360}}},
                        {"identifier": "Saturation", "dataType": {"type": "int", "specs": {"max": 100}}},
                        {"identifier": "Value", "dataType": {"type": "int", "specs": {"max": 100}}},
                    ],
                }},
            ]
        }
        spec = color_spec_from_tsl(tsl)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.identifier, "HSVColor")
        self.assertEqual(spec.max_for("hue"), 360.0)

    def test_spec_from_props_fallback(self):
        props = {"HSVColor": {"Hue": 200, "Saturation": 60, "Value": 90}}
        spec = color_spec_from_props(props)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.identifier, "HSVColor")
        hs = spec.to_hs(props["HSVColor"])
        self.assertAlmostEqual(hs[0], 200.0, delta=1.0)

    def test_as_struct_json_string(self):
        self.assertEqual(as_struct('{"h": 1}'), {"h": 1})
        self.assertIsNone(as_struct("not json"))
        self.assertIsNone(as_struct(42))

    def test_mode_spec_fallback(self):
        ms = ModeSpec(identifier="LightMode")
        self.assertEqual(ms.white_value, 0)
        self.assertEqual(ms.color_value, 1)


if __name__ == "__main__":
    unittest.main()
