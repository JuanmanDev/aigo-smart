"""aigosmart — standalone client for the AigoSmart (Aigostar) smart home backend.

Pure-Python, zero third-party dependencies for the cloud API
(cryptography only for AES password encryption; a pure-stdlib AES
fallback is included).

Supports:
  * Full 5-step login (email + password [+ security code])
  * Device list, TSL model, property get/set, service invoke
  * Token refresh
  * Local ALCS (CoAP) discovery + control (experimental)
  * BLE (Breeze) provisioning of new devices

See REVERSE_ENGINEERING.md for the full protocol documentation.
"""

__version__ = "0.1.0"

from .cloud import AigoApiError, AigoCloudClient, NeedSecurityCodeError
from .local import AlcsDevice, discover_alcs_devices

__all__ = [
    "AigoCloudClient",
    "AigoApiError",
    "NeedSecurityCodeError",
    "AlcsDevice",
    "discover_alcs_devices",
]
