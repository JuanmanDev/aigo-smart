"""
Vendor colour-property handling for Aigostar bulbs.

Colour is always exposed as a single struct TSL property, but its identifier,
its member names and its value ranges differ per product family:

  Wi-Fi (TG7100C)   ``HSVColor``  {Hue 0-360, Saturation 0-100, Value 0-100}
  Bluetooth Mesh    product specific — Alibaba's mesh model mirrors the
                    Bluetooth SIG Light HSL/CTL models, so identifiers are
                    lower-camelCase and the ranges are frequently 0-65535

Hard-coding one identifier per transport therefore does not generalise. The
colour property is instead recognised by its *shape*: any struct carrying hue
and saturation members (or red/green/blue) is the colour property, and its
ranges are read from the device's own TSL model whenever it is available.
"""
from __future__ import annotations

import colorsys
import json
import logging
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Canonical member roles
ROLE_HUE = "hue"
ROLE_SATURATION = "saturation"
ROLE_VALUE = "value"
ROLE_LIGHTNESS = "lightness"
ROLE_RED = "red"
ROLE_GREEN = "green"
ROLE_BLUE = "blue"

# Struct member identifier (lower-cased) -> canonical role
_MEMBER_ALIASES: dict[str, str] = {
    "hue": ROLE_HUE,
    "h": ROLE_HUE,
    "saturation": ROLE_SATURATION,
    "sat": ROLE_SATURATION,
    "s": ROLE_SATURATION,
    "value": ROLE_VALUE,
    "brightness": ROLE_VALUE,
    "v": ROLE_VALUE,
    "lightness": ROLE_LIGHTNESS,
    "l": ROLE_LIGHTNESS,
    "red": ROLE_RED,
    "r": ROLE_RED,
    "green": ROLE_GREEN,
    "g": ROLE_GREEN,
    "blue": ROLE_BLUE,
    "b": ROLE_BLUE,
}

# Fallback maxima when the TSL does not declare a range
_DEFAULT_MAXIMA: dict[str, float] = {
    ROLE_HUE: 360.0,
    ROLE_SATURATION: 100.0,
    ROLE_VALUE: 100.0,
    ROLE_LIGHTNESS: 100.0,
    ROLE_RED: 255.0,
    ROLE_GREEN: 255.0,
    ROLE_BLUE: 255.0,
}

ENCODING_HSV = "hsv"
ENCODING_HSL = "hsl"
ENCODING_RGB = "rgb"

# Enum labels that identify the "colour" position of a light-mode property
_COLOR_MODE_LABELS = ("colour", "color", "rgb", "hsv", "hsl", "彩光", "彩色")
_WHITE_MODE_LABELS = ("white", "mono", "cct", "cool", "warm", "白光", "白色")

# A white/colour switch is a small enum. Products also expose a scene selector
# whose identifier contains "mode" but which lists dozens of effects (spring,
# rainbow, candlelight, strobe...); writing to that would start an animation.
_MAX_LIGHT_MODE_ENUM_SIZE = 6


def as_struct(value: Any) -> dict[str, Any] | None:
    """Struct property values arrive either as objects or as JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return None
        if isinstance(decoded, dict):
            return decoded
    return None


@dataclass(frozen=True)
class ColorSpec:
    """How one product encodes colour in a single struct property."""

    identifier: str
    encoding: str
    # canonical role -> actual struct member identifier as the device spells it
    members: dict[str, str]
    # canonical role -> maximum raw value
    maxima: dict[str, float] = field(default_factory=dict)

    def max_for(self, role: str) -> float:
        return self.maxima.get(role) or _DEFAULT_MAXIMA[role]

    # ------------------------------------------------------------------
    # Device value -> Home Assistant value
    # ------------------------------------------------------------------

    def to_hs(self, raw: dict[str, Any]) -> tuple[float, float] | None:
        """Convert a raw struct into HA's (hue 0-360, saturation 0-100)."""
        values = self._read(raw)
        if values is None:
            return None

        if self.encoding == ENCODING_RGB:
            r, g, b = (
                values[ROLE_RED] / self.max_for(ROLE_RED),
                values[ROLE_GREEN] / self.max_for(ROLE_GREEN),
                values[ROLE_BLUE] / self.max_for(ROLE_BLUE),
            )
            hue, sat, _ = colorsys.rgb_to_hsv(r, g, b)
            return (hue * 360.0, sat * 100.0)

        hue = values[ROLE_HUE] / self.max_for(ROLE_HUE) * 360.0
        sat = values[ROLE_SATURATION] / self.max_for(ROLE_SATURATION) * 100.0
        return (max(0.0, min(360.0, hue)), max(0.0, min(100.0, sat)))

    def to_brightness_pct(self, raw: dict[str, Any]) -> float | None:
        """Extract the brightness carried by the colour struct, 0-100, if any."""
        values = self._read(raw)
        if values is None:
            return None

        if self.encoding == ENCODING_RGB:
            _, _, val = colorsys.rgb_to_hsv(
                values[ROLE_RED] / self.max_for(ROLE_RED),
                values[ROLE_GREEN] / self.max_for(ROLE_GREEN),
                values[ROLE_BLUE] / self.max_for(ROLE_BLUE),
            )
            return val * 100.0

        role = ROLE_LIGHTNESS if self.encoding == ENCODING_HSL else ROLE_VALUE
        if role not in values:
            return None
        return max(0.0, min(100.0, values[role] / self.max_for(role) * 100.0))

    # ------------------------------------------------------------------
    # Home Assistant value -> device value
    # ------------------------------------------------------------------

    def build(self, hue: float, saturation: float, brightness_pct: float) -> dict[str, Any]:
        """Build the raw struct the device expects from HA hue/sat/brightness."""
        hue = max(0.0, min(360.0, float(hue)))
        saturation = max(0.0, min(100.0, float(saturation)))
        brightness_pct = max(0.0, min(100.0, float(brightness_pct)))

        if self.encoding == ENCODING_RGB:
            r, g, b = colorsys.hsv_to_rgb(hue / 360.0, saturation / 100.0, brightness_pct / 100.0)
            return {
                self.members[ROLE_RED]: round(r * self.max_for(ROLE_RED)),
                self.members[ROLE_GREEN]: round(g * self.max_for(ROLE_GREEN)),
                self.members[ROLE_BLUE]: round(b * self.max_for(ROLE_BLUE)),
            }

        out: dict[str, Any] = {
            self.members[ROLE_HUE]: round(hue / 360.0 * self.max_for(ROLE_HUE)),
            self.members[ROLE_SATURATION]: round(saturation / 100.0 * self.max_for(ROLE_SATURATION)),
        }
        role = ROLE_LIGHTNESS if self.encoding == ENCODING_HSL else ROLE_VALUE
        if role in self.members:
            out[self.members[role]] = round(brightness_pct / 100.0 * self.max_for(role))
        return out

    # ------------------------------------------------------------------

    def _read(self, raw: Any) -> dict[str, float] | None:
        """Pull the known members out of a raw struct, keyed by canonical role."""
        raw = as_struct(raw)
        if raw is None:
            return None

        # Device responses occasionally differ in case from the TSL declaration.
        lowered = {str(k).lower(): v for k, v in raw.items()}
        values: dict[str, float] = {}
        for role, member in self.members.items():
            candidate = raw.get(member, lowered.get(member.lower()))
            if candidate is None:
                continue
            try:
                values[role] = float(candidate)
            except (TypeError, ValueError):
                continue

        required = (
            (ROLE_RED, ROLE_GREEN, ROLE_BLUE)
            if self.encoding == ENCODING_RGB
            else (ROLE_HUE, ROLE_SATURATION)
        )
        if any(role not in values for role in required):
            return None
        return values


@dataclass(frozen=True)
class ModeSpec:
    """The light-mode enum property and the values meaning white / colour."""

    identifier: str
    white_value: int = 0
    color_value: int = 1


# ======================================================================
#  Confirmed products
# ======================================================================

@dataclass(frozen=True)
class ProductProfile:
    """A colour + mode pairing confirmed on real hardware."""

    color: ColorSpec
    mode: ModeSpec


# Keyed by productKey. An entry here short-circuits TSL detection entirely, so
# the product keeps working even when /thing/tsl/get is unavailable or the
# account's token has expired at setup time.
#
# To add a product: call the `aigosmart.dump_tsl` service and copy the snippet
# from the "resolved colour profile" line in home-assistant.log.
KNOWN_PRODUCT_PROFILES: dict[str, ProductProfile] = {
    # Aigostar "Downlight RGB CCT" (Bluetooth Mesh, behind the Smart Mesh Gw).
    # Confirmed against /thing/tsl/get on real hardware.
    #
    # Note the hybrid naming: the switch, brightness and colour temperature use
    # the mesh identifiers (powerstate / brightness / colorTemperature), but
    # colour and the white/colour switch use the Wi-Fi ones (HSVColor /
    # LightMode). The lower-case "mode" property on this product is a scene
    # selector, not the white/colour switch — see PROP_MESH_SCENE in const.py.
    "a1uh0UxUu3Z": ProductProfile(
        color=ColorSpec(
            identifier="HSVColor",
            encoding=ENCODING_HSV,
            members={"hue": "Hue", "saturation": "Saturation", "value": "Value"},
            maxima={"hue": 360.0, "saturation": 100.0, "value": 100.0},
        ),
        mode=ModeSpec(identifier="LightMode", white_value=0, color_value=1),
    ),
}


def known_profile(product_key: str) -> ProductProfile | None:
    return KNOWN_PRODUCT_PROFILES.get(product_key) if product_key else None


def as_source_snippet(product_key: str, color: ColorSpec, mode: ModeSpec) -> str:
    """Render a resolved profile as a KNOWN_PRODUCT_PROFILES entry."""
    return (
        f'    "{product_key}": ProductProfile(\n'
        f"        color=ColorSpec(\n"
        f'            identifier="{color.identifier}",\n'
        f'            encoding="{color.encoding}",\n'
        f"            members={color.members!r},\n"
        f"            maxima={color.maxima!r},\n"
        f"        ),\n"
        f"        mode=ModeSpec(\n"
        f'            identifier="{mode.identifier}",\n'
        f"            white_value={mode.white_value},\n"
        f"            color_value={mode.color_value},\n"
        f"        ),\n"
        f"    ),"
    )


# ======================================================================
#  TSL parsing
# ======================================================================

def normalize_tsl(payload: Any) -> dict[str, Any]:
    """Unwrap the several shapes /thing/tsl/get returns into a plain TSL dict."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    if not isinstance(payload, dict):
        return {}
    if "properties" in payload:
        return payload
    for key in ("tsl", "tslStr", "data", "abilities"):
        if key in payload:
            nested = normalize_tsl(payload[key])
            if nested:
                return nested
    return {}


def _spec_range_max(data_type: Any) -> float | None:
    """Read the declared maximum of a scalar member's dataType."""
    if not isinstance(data_type, dict):
        return None
    specs = data_type.get("specs")
    if not isinstance(specs, dict):
        return None
    raw_max = specs.get("max")
    if raw_max is None:
        return None
    try:
        return float(raw_max)
    except (TypeError, ValueError):
        return None


def _classify(members: dict[str, str]) -> str | None:
    """Decide the encoding from the canonical roles a struct exposes."""
    if all(role in members for role in (ROLE_RED, ROLE_GREEN, ROLE_BLUE)):
        return ENCODING_RGB
    if ROLE_HUE in members and ROLE_SATURATION in members:
        if ROLE_LIGHTNESS in members and ROLE_VALUE not in members:
            return ENCODING_HSL
        return ENCODING_HSV
    return None


def color_spec_from_tsl(tsl: dict[str, Any]) -> ColorSpec | None:
    """Find the colour struct in a TSL model, with ranges taken from the TSL."""
    tsl = normalize_tsl(tsl)
    for prop in tsl.get("properties", []) or []:
        if not isinstance(prop, dict):
            continue
        data_type = prop.get("dataType") or {}
        if str(data_type.get("type", "")).lower() != "struct":
            continue

        members: dict[str, str] = {}
        maxima: dict[str, float] = {}
        for member in data_type.get("specs") or []:
            if not isinstance(member, dict):
                continue
            identifier = member.get("identifier")
            if not identifier:
                continue
            role = _MEMBER_ALIASES.get(str(identifier).lower())
            if role is None or role in members:
                continue
            members[role] = identifier
            declared_max = _spec_range_max(member.get("dataType"))
            if declared_max:
                maxima[role] = declared_max

        encoding = _classify(members)
        if encoding is None:
            continue

        spec = ColorSpec(
            identifier=prop["identifier"],
            encoding=encoding,
            members=members,
            maxima=maxima,
        )
        _LOGGER.debug("Aigosmart: colour property found in TSL: %s", spec)
        return spec
    return None


def color_spec_from_props(props: dict[str, Any]) -> ColorSpec | None:
    """
    Fallback detection from a live property snapshot, used when the TSL is not
    reachable. Ranges are unknown here, so the defaults apply — except that a
    member above 100 implies the 16-bit range the mesh models use.
    """
    for identifier, raw_value in (props or {}).items():
        value = as_struct(raw_value)
        if value is None:
            continue

        members: dict[str, str] = {}
        maxima: dict[str, float] = {}
        for key, raw in value.items():
            role = _MEMBER_ALIASES.get(str(key).lower())
            if role is None or role in members:
                continue
            members[role] = key
            try:
                numeric = float(raw)
            except (TypeError, ValueError):
                continue
            if role in (ROLE_SATURATION, ROLE_VALUE, ROLE_LIGHTNESS) and numeric > 100:
                maxima[role] = 65535.0
            elif role == ROLE_HUE and numeric > 360:
                maxima[role] = 65535.0

        encoding = _classify(members)
        if encoding is None:
            continue

        spec = ColorSpec(
            identifier=identifier,
            encoding=encoding,
            members=members,
            maxima=maxima,
        )
        _LOGGER.debug("Aigosmart: colour property inferred from live properties: %s", spec)
        return spec
    return None


def mode_spec_from_tsl(tsl: dict[str, Any], fallback_identifier: str) -> ModeSpec:
    """
    Locate the white/colour mode enum and the values it uses. Falls back to the
    conventional 0=white / 1=colour on the given identifier.
    """
    tsl = normalize_tsl(tsl)
    for prop in tsl.get("properties", []) or []:
        if not isinstance(prop, dict):
            continue
        identifier = prop.get("identifier")
        if not identifier or "mode" not in str(identifier).lower():
            continue
        data_type = prop.get("dataType") or {}
        if str(data_type.get("type", "")).lower() != "enum":
            continue
        specs = data_type.get("specs")
        if not isinstance(specs, dict):
            continue
        if len(specs) > _MAX_LIGHT_MODE_ENUM_SIZE:
            _LOGGER.debug(
                "Aigosmart: ignoring '%s' as light mode — %d enum values, "
                "this is a scene selector",
                identifier, len(specs),
            )
            continue

        white_value: int | None = None
        color_value: int | None = None
        for key, label in specs.items():
            try:
                numeric = int(key)
            except (TypeError, ValueError):
                continue
            text = str(label).lower()
            if color_value is None and any(word in text for word in _COLOR_MODE_LABELS):
                color_value = numeric
            elif white_value is None and any(word in text for word in _WHITE_MODE_LABELS):
                white_value = numeric

        if color_value is not None:
            spec = ModeSpec(
                identifier=identifier,
                white_value=white_value if white_value is not None else 0,
                color_value=color_value,
            )
            _LOGGER.debug("Aigosmart: light-mode property found in TSL: %s", spec)
            return spec

    return ModeSpec(identifier=fallback_identifier)
