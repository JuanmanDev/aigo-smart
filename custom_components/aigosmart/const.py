"""Constants for the AigoSmart integration."""
from __future__ import annotations

import logging
from typing import Any

DOMAIN = "aigosmart"
_LOGGER = logging.getLogger(__name__)

# Config entry keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_SECURITY_CODE = "security_code"
CONF_REGION_HOST = "iot_host"
CONF_SSID = "ssid"                # BLE provisioning: WiFi network
CONF_WIFI_PASSWORD = "wifi_password"

# Defaults
DEFAULT_SCAN_INTERVAL = 30
TOKEN_REFRESH_MARGIN = 600

# Alibaba IoT device status codes (from the app's BaseDeviceStatus class,
# classes11.dex: NOT_ACTIVATED=0, ONLINE=1, OFFLINE=3, DISABLED=8).
# NOTE: listBindingByAccount's "status" field is NOT a reliable online flag
# for all accounts — an online device can report 3 there. When it does, the
# real online state must be confirmed via /thing/status/get (apiVer 1.0.5).
STATUS_NOT_ACTIVATED = 0
STATUS_ONLINE = 1
STATUS_OFFLINE = 3
STATUS_DISABLED = 8

def is_status_online(status: Any) -> bool:
    """True only for the ONLINE code (int or numeric string)."""
    try:
        return int(status) == STATUS_ONLINE
    except (TypeError, ValueError):
        return False

# Network types returned by list_devices (netType)
NET_TYPE_WIFI = "NET_WIFI"
NET_TYPE_BT = "NET_BT"          # BLE mesh light (via gateway/app)
NET_TYPE_ZIGBEE = "NET_ZIGBEE"
NET_TYPE_LORA = "NET_LORA"

# --- Light TSL properties --------------------------------------------------
# Wi-Fi bulbs (TG7100C family)
PROP_LIGHT_SWITCH = "LightSwitch"
PROP_LIGHT_BRIGHTNESS = "Brightness"
PROP_LIGHT_COLOR_TEMP = "ColorTemperature"
PROP_LIGHT_MODE = "LightMode"
PROP_LIGHT_HSV = "HSVColor"

# BLE mesh lights use different identifiers (from hass-aigosmart / mesh model)
PROP_MESH_SWITCH = "powerstate"
PROP_MESH_BRIGHTNESS = "brightness"
PROP_MESH_COLOR_TEMP = "colorTemperature"
PROP_MESH_HSV = "colorData"
PROP_MESH_LIGHT_MODE = "work_mode"

PROP_SWITCH_CANDIDATES = (PROP_LIGHT_SWITCH, PROP_MESH_SWITCH)
PROP_BRIGHTNESS_CANDIDATES = (PROP_LIGHT_BRIGHTNESS, PROP_MESH_BRIGHTNESS)
PROP_COLOR_TEMP_CANDIDATES = (PROP_LIGHT_COLOR_TEMP, PROP_MESH_COLOR_TEMP)
PROP_HSV_CANDIDATES = (PROP_LIGHT_HSV, PROP_MESH_HSV)

# --- Fan TSL properties ------------------------------------------------------
PROP_FAN_POWER = "powerstate"
PROP_FAN_SPEED = "windspeed"               # enum 1|2|3
PROP_FAN_MODE = "mode"                     # enum 0=normal 1=natural 2=sleep
PROP_FAN_OSCILLATE = "angleAutoLROnOff"     # bool left/right auto swing
PROP_FAN_TIMER = "appointmentClosingTime"  # int 0-24 h auto-off
PROP_FAN_BUZZER = "buzzerSwitch"           # bool key beep
FAN_PRESET_MODES = {0: "Normal", 1: "Natural", 2: "Sleep"}

# --- Kettle / water heater TSL -----------------------------------------------
PROP_KETTLE_SWITCH = "HeatingSwitch"
PROP_KETTLE_TARGET = "Target_temperature"
PROP_KETTLE_TEMP = "temperature"
PROP_KETTLE_KEEP_WARM = "heatpreservation"

# --- Sensor TSL ---------------------------------------------------------------
PROP_CURRENT_TEMP = "CuTemperature"
PROP_HUMIDITY = "humidity"

KELVIN_WARM = 2700
KELVIN_COOL = 6500
