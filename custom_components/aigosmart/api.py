"""Thin async-friendly wrapper around the vendored aigosmart client.

The heavy lifting lives in python_client/aigosmart (standalone-testable);
this integration vendors a copy at custom_components/aigosmart/lib so it
works when installed via HACS. Property reads/writes are LOCAL-FIRST:
they try ALCS (CoAP on the LAN) before hitting the cloud.
"""
from __future__ import annotations

import logging

from .lib import AigoApiError, AigoCloudClient, NeedSecurityCodeError  # noqa: F401
from .lib.local import AlcsDevice, discover_alcs_devices  # noqa: F401
from .local_first import LocalFirstMixin


class AigoSmartApiClient(LocalFirstMixin):
    """Wrapper that holds credentials and exposes sync methods for executor jobs."""

    def __init__(self, email: str, password: str, security_code: str = "",
                 iot_host: str = "eu-central-1.api-iot.aliyuncs.com") -> None:
        super().__init__()  # initializes LocalFirstMixin's _local_ok/_local_bad
        self.email = email
        self.password = password
        self.security_code = security_code
        self._client = AigoCloudClient(iot_host)

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        self._client.login(self.email, self.password, self.security_code)

    def ensure_token(self) -> str:
        return self._client.ensure_token()

    # -- devices ---------------------------------------------------------------

    def list_devices(self) -> list[dict]:
        return self._client.list_devices()

    def get_properties(self, iot_id: str) -> dict:
        return self._client.get_properties(iot_id)

    def get_status(self, iot_id: str) -> dict:
        return self._client.get_status(iot_id)

    def set_properties(self, iot_id: str, items: dict) -> dict:
        return self._client.set_properties(iot_id, items)

    def get_tsl(self, iot_id: str) -> dict:
        return self._client.get_tsl(iot_id)

    def invoke_service(self, iot_id: str, identifier: str, args: dict | None = None) -> dict:
        return self._client.invoke_service(iot_id, identifier, args)

    def get_alcs_access_info(self, iot_ids: list[str]) -> list[dict]:
        return self._client.get_alcs_access_info(iot_ids)

    # -- local ---------------------------------------------------------------

    @staticmethod
    def discover_local(timeout: float = 3.0) -> list:
        return discover_alcs_devices(timeout)
