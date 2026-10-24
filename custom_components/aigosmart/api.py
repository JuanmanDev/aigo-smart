"""Thin async-friendly wrapper around the vendored aigosmart client.

The heavy lifting lives in python_client/aigosmart (standalone-testable);
this integration vendors a copy at custom_components/aigosmart/lib so it
works when installed via HACS. Property reads/writes are LOCAL-FIRST:
they try ALCS (CoAP on the LAN) before hitting the cloud.

Session self-healing:
  The cloud allows one session per account — logging in elsewhere (the
  phone app, a script) invalidates ours, and refresh tokens can be revoked.
  Every cloud call that fails with a session error triggers a full re-login
  and is retried once, so polls, status checks and commands all recover on
  the next call instead of waiting for a periodic device-list refresh.
"""
from __future__ import annotations

import functools
import logging
import threading
import time

from .lib import AigoApiError, AigoCloudClient, NeedSecurityCodeError  # noqa: F401
from .lib.local import AlcsDevice, discover_alcs_devices  # noqa: F401
from .local_first import LocalFirstMixin

_LOGGER = logging.getLogger(__name__)

# Aliyun IoT gateway codes meaning the iotToken/identity is no longer valid
SESSION_ERROR_CODES = {401, 403, 2401, 29003}
SESSION_ERROR_KEYWORDS = ("session", "token", "expired", "unauthorized",
                          "identityid", "not logged in", "auth error")
# Backoff after a FAILED re-login (doubles per failure, capped)
RELOGIN_BACKOFF_MIN = 30
RELOGIN_BACKOFF_MAX = 600
# Never re-login more often than this, even after successes
RELOGIN_MIN_INTERVAL = 10


def is_session_error(exc: Exception) -> bool:
    """True when an API error means the session must be re-established."""
    rsp = getattr(exc, "response", None)
    if isinstance(rsp, dict):
        try:
            if int(rsp.get("code", 0)) in SESSION_ERROR_CODES:
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc).lower()
    return any(k in msg for k in SESSION_ERROR_KEYWORDS)


def _relogin_on_session_error(func):
    """Re-login and retry once when a cloud call fails with a session error."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        token = self._client.iot_token
        try:
            return func(self, *args, **kwargs)
        except NeedSecurityCodeError:
            raise
        except Exception as exc:
            if not is_session_error(exc):
                raise
            self._relogin(token, exc)
            return func(self, *args, **kwargs)
    return wrapper


class AigoSmartApiClient(LocalFirstMixin):
    """Wrapper that holds credentials and exposes sync methods for executor jobs."""

    def __init__(self, email: str, password: str, security_code: str = "",
                 iot_host: str = "eu-central-1.api-iot.aliyuncs.com") -> None:
        super().__init__()  # initializes LocalFirstMixin's _local_ok/_local_bad
        self.email = email
        self.password = password
        self.security_code = security_code
        self._client = AigoCloudClient(iot_host)
        self._login_lock = threading.Lock()
        self._relogin_backoff = 0.0
        self._relogin_blocked_until = 0.0
        self._last_relogin = float("-inf")

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        self._client.login(self.email, self.password, self.security_code)

    def _relogin(self, stale_token: str | None, cause: Exception) -> None:
        """Full re-login after a session error (thread-safe, backoff on failure).

        If another thread already replaced the token that failed, the call is
        simply retried with the new one instead of logging in again.
        """
        with self._login_lock:
            if self._client.iot_token != stale_token:
                return
            now = time.monotonic()
            if (now < self._relogin_blocked_until
                    or now - self._last_relogin < RELOGIN_MIN_INTERVAL):
                raise cause
            self._last_relogin = now
            _LOGGER.warning("AigoSmart session invalid (%s) — re-logging in", cause)
            try:
                self.login()
            except Exception:
                self._relogin_backoff = min(
                    max(self._relogin_backoff * 2, RELOGIN_BACKOFF_MIN),
                    RELOGIN_BACKOFF_MAX)
                self._relogin_blocked_until = now + self._relogin_backoff
                _LOGGER.error("AigoSmart re-login failed; next attempt in %ds",
                              self._relogin_backoff)
                raise
            self._relogin_backoff = 0.0
            self._relogin_blocked_until = 0.0
            _LOGGER.info("AigoSmart re-login succeeded")

    @_relogin_on_session_error
    def ensure_token(self) -> str:
        return self._client.ensure_token()

    # -- devices ---------------------------------------------------------------

    @_relogin_on_session_error
    def list_devices(self) -> list[dict]:
        return self._client.list_devices()

    @_relogin_on_session_error
    def get_properties(self, iot_id: str) -> dict:
        return self._client.get_properties(iot_id)

    @_relogin_on_session_error
    def get_status(self, iot_id: str) -> dict:
        return self._client.get_status(iot_id)

    @_relogin_on_session_error
    def set_properties(self, iot_id: str, items: dict) -> dict:
        return self._client.set_properties(iot_id, items)

    @_relogin_on_session_error
    def get_tsl(self, iot_id: str) -> dict:
        return self._client.get_tsl(iot_id)

    @_relogin_on_session_error
    def invoke_service(self, iot_id: str, identifier: str, args: dict | None = None) -> dict:
        return self._client.invoke_service(iot_id, identifier, args)

    @_relogin_on_session_error
    def get_alcs_access_info(self, iot_ids: list[str]) -> list[dict]:
        return self._client.get_alcs_access_info(iot_ids)

    # -- local ---------------------------------------------------------------

    @staticmethod
    def discover_local(timeout: float = 3.0) -> list:
        return discover_alcs_devices(timeout)
