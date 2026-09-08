"""Voluptuous schema for the AigoSmart integration (no yaml config required)."""
from __future__ import annotations

import voluptuous as vol

from .const import DOMAIN

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)


def config_schema() -> vol.Schema:
    """Return integration configuration schema."""
    return CONFIG_SCHEMA

