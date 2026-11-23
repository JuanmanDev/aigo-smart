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


HSV_SPEC = ColorSpec(
    identifier="HSVColor",
    encoding=ENCODING_HSV,
    members={"hue": "Hue", "saturation": "Saturation", "value": "Value"},
    maxima={"hue": 360.0, "saturation": 100.0, "value": 100.0},
)
MESH16_SPEC = ColorSpec(
    identifier="colorData",
    encoding=ENCODING_HSV,
    members={"hue": "h", "saturation": "s", "value": "v"},
    maxima={"hue": 65535.0, "saturation": 65535.0, "value": 65535.0},
)


class TestRedAndPrimaries(unittest.TestCase):
    """#2: red must encode exactly like the app's red (Hue 0, Saturation 100)."""

    def test_red(self):
        self.assertEqual(HSV_SPEC.build(0, 100, 100),
                         {"Hue": 0, "Saturation": 100, "Value": 100})

    def test_hue_360_and_near_360_wrap_to_0(self):
        for hue in (360, 359.6, 720):
            # whole-degree range: 359.6 rounds to 360, which is red = 0
            self.assertEqual(HSV_SPEC.build(hue, 100, 100)["Hue"], 0, hue)
        for hue in (360, 720):
            self.assertEqual(MESH16_SPEC.build(hue, 100, 100)["h"], 0, hue)
        self.assertEqual(MESH16_SPEC.build(359.6, 100, 100)["h"], 65462)
        self.assertEqual(HSV_SPEC.build(359, 100, 100)["Hue"], 359)

    def test_primaries_roundtrip(self):
        for hue in (0, 60, 120, 180, 240, 300, 359):
            for spec in (HSV_SPEC, MESH16_SPEC):
                raw = spec.build(hue, 100, 100)
                h, s = spec.to_hs(raw)
                self.assertAlmostEqual(h, hue, delta=0.6, msg=(spec.identifier, hue))
                self.assertAlmostEqual(s, 100, delta=0.01)

    def test_shadow_quantisation_reads_back_close(self):
        # values the mesh lamp reports after a write (16-bit quantised)
        h, s = HSV_SPEC.to_hs({"Hue": 8.99794, "Saturation": 66.9993133, "Value": 100})
        self.assertAlmostEqual(h, 9, delta=0.01)
        self.assertAlmostEqual(s, 67, delta=0.01)

    def test_rgb_primaries(self):
        rgb = ColorSpec(identifier="RGBColor", encoding="rgb",
                        members={"red": "Red", "green": "Green", "blue": "Blue"})
        self.assertEqual(rgb.build(0, 100, 100), {"Red": 255, "Green": 0, "Blue": 0})
        self.assertEqual(rgb.build(120, 100, 100), {"Red": 0, "Green": 255, "Blue": 0})
        self.assertEqual(rgb.build(240, 100, 100), {"Red": 0, "Green": 0, "Blue": 255})
        self.assertEqual(rgb.to_hs({"Red": 255, "Green": 0, "Blue": 0}), (0.0, 100.0))


if __name__ == "__main__":
    unittest.main()
