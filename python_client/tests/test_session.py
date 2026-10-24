"""Session self-healing in the HA API wrapper (custom_components/aigosmart/api.py).

The cloud allows one session per account: a login from the phone app or a
script invalidates Home Assistant's iotToken. DEPLOYED EVIDENCE (2026-10-23):
HA only re-logged in on the 5-minute device-list refresh (plus a 300 s
cooldown), so every entity stayed unavailable for minutes; status calls failed
with {'code': 29003, 'localizedMsg': 'identityId is blank'}.

Policy tested here:
  * any wrapped cloud call that fails with a session error re-logs in and
    retries once
  * non-session errors propagate untouched (no login)
  * concurrent failures on the same stale token log in only once
  * failed logins back off; successful ones reset the backoff
"""
import importlib.util
import os
import sys
import types
import unittest
import unittest.mock as mock

_PKG = "aigo_api_under_test"
_API_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "custom_components", "aigosmart", "api.py"))


class AigoApiError(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


class NeedSecurityCodeError(Exception):
    pass


class FakeCloud:
    """Stands in for AigoCloudClient: login() issues a new token."""

    def __init__(self, iot_host=""):
        self.iot_token = "t0"
        self.logins = 0
        self.get_status = mock.MagicMock()
        self.list_devices = mock.MagicMock(return_value=[])

    def login(self, email, password, code=""):
        self.logins += 1
        self.iot_token = f"t{self.logins}"


def _load_api():
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = []
    lib = types.ModuleType(f"{_PKG}.lib")
    lib.AigoApiError = AigoApiError
    lib.AigoCloudClient = FakeCloud
    lib.NeedSecurityCodeError = NeedSecurityCodeError
    lib.AlcsDevice = object
    lib.discover_alcs_devices = lambda *a, **k: []
    lib_local = types.ModuleType(f"{_PKG}.lib.local")
    lib_local.AlcsDevice = object
    lib_local.discover_alcs_devices = lib.discover_alcs_devices
    local_first = types.ModuleType(f"{_PKG}.local_first")

    class LocalFirstMixin:
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

    local_first.LocalFirstMixin = LocalFirstMixin
    for name, mod in ((_PKG, pkg), (f"{_PKG}.lib", lib),
                      (f"{_PKG}.lib.local", lib_local),
                      (f"{_PKG}.local_first", local_first)):
        sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location(f"{_PKG}.api", _API_PATH)
    api = importlib.util.module_from_spec(spec)
    api.__package__ = _PKG
    spec.loader.exec_module(api)
    return api


api = _load_api()

SESSION_ERR = AigoApiError(
    "API error /thing/status/get: identityId is blank",
    response={"code": 29003, "localizedMsg": "identityId is blank"})


class TestIsSessionError(unittest.TestCase):
    def test_identity_blank_code(self):
        self.assertTrue(api.is_session_error(SESSION_ERR))

    def test_401_code(self):
        self.assertTrue(api.is_session_error(
            AigoApiError("API error", response={"code": 401, "message": "request auth error"})))

    def test_token_keyword(self):
        self.assertTrue(api.is_session_error(AigoApiError("iotToken is invalid")))

    def test_device_offline_is_not_session(self):
        self.assertFalse(api.is_session_error(AigoApiError(
            "API error /thing/properties/set: device offline",
            response={"code": 9201, "message": "device offline"})))

    def test_network_error_is_not_session(self):
        self.assertFalse(api.is_session_error(OSError("[Errno 113] Host is unreachable")))


class TestRelogin(unittest.TestCase):
    def setUp(self):
        self.client = api.AigoSmartApiClient("user@example.com", "pw")
        self.cloud = self.client._client

    def test_session_error_relogs_and_retries(self):
        self.cloud.get_status.side_effect = [SESSION_ERR, {"status": 1}]
        self.assertEqual(self.client.get_status("iot1"), {"status": 1})
        self.assertEqual(self.cloud.logins, 1)
        self.assertEqual(self.cloud.get_status.call_count, 2)

    def test_other_errors_propagate_without_login(self):
        self.cloud.get_status.side_effect = OSError("timed out")
        with self.assertRaises(OSError):
            self.client.get_status("iot1")
        self.assertEqual(self.cloud.logins, 0)

    def test_already_relogged_token_just_retries(self):
        """Another thread re-logged in while this call was failing."""
        def fail_then_rotate(_iot):
            self.cloud.iot_token = "fresh-from-other-thread"
            raise SESSION_ERR
        calls = iter([fail_then_rotate, lambda _iot: {"status": 1}])
        self.cloud.get_status.side_effect = lambda iot: next(calls)(iot)
        self.assertEqual(self.client.get_status("iot1"), {"status": 1})
        self.assertEqual(self.cloud.logins, 0)

    def test_retry_failing_again_does_not_loop(self):
        self.cloud.get_status.side_effect = SESSION_ERR
        with self.assertRaises(AigoApiError):
            self.client.get_status("iot1")
        self.assertEqual(self.cloud.logins, 1)
        self.assertEqual(self.cloud.get_status.call_count, 2)

    def test_failed_login_backs_off(self):
        self.cloud.get_status.side_effect = SESSION_ERR
        with mock.patch.object(self.cloud, "login", side_effect=OSError("down")) as login:
            with self.assertRaises(OSError):
                self.client.get_status("iot1")
            self.assertEqual(self.client._relogin_backoff, api.RELOGIN_BACKOFF_MIN)
            # within backoff: original session error, no new login attempt
            with self.assertRaises(AigoApiError):
                self.client.get_status("iot1")
            self.assertEqual(login.call_count, 1)

    def test_successful_login_resets_backoff(self):
        self.client._relogin_backoff = 120
        self.cloud.get_status.side_effect = [SESSION_ERR, {"status": 1}]
        self.client.get_status("iot1")
        self.assertEqual(self.client._relogin_backoff, 0.0)

    def test_min_interval_between_logins(self):
        self.cloud.get_status.side_effect = [SESSION_ERR, {"status": 1}, SESSION_ERR]
        self.client.get_status("iot1")
        # kicked again immediately: no second login inside RELOGIN_MIN_INTERVAL
        with self.assertRaises(AigoApiError):
            self.client.get_status("iot1")
        self.assertEqual(self.cloud.logins, 1)

    def test_security_code_error_not_retried(self):
        self.cloud.list_devices.side_effect = NeedSecurityCodeError("code needed")
        with self.assertRaises(NeedSecurityCodeError):
            self.client.list_devices()
        self.assertEqual(self.cloud.logins, 0)


if __name__ == "__main__":
    unittest.main()
