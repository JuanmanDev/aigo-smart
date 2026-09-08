"""Vendored pure-Python AigoSmart client (self-contained copy).

Mirrors python_client/aigosmart so the HA integration keeps working when
installed via HACS, which only ships the custom_components folder.
"""
from .cloud import AigoApiError, AigoCloudClient, NeedSecurityCodeError
from .local import AlcsDevice, discover_alcs_devices

__all__ = [
    "AigoCloudClient",
    "AigoApiError",
    "NeedSecurityCodeError",
    "AlcsDevice",
    "discover_alcs_devices",
]
