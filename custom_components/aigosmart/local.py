"""Local ALCS helpers for the HA integration."""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


async def async_discover_local(hass):
    """Run ALCS multicast discovery in an executor. Returns DiscoveredDevice list."""
    from .api import AigoSmartApiClient

    return await hass.async_add_executor_job(AigoSmartApiClient.discover_local, 3.0)
