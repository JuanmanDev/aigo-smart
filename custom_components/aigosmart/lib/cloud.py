"""Cloud API client for AigoSmart (Alibaba IoT platform, EU region).

Reverse-engineered from the AigoSmart Android APK (v2.15.6) and validated
against the live endpoints. Login flow:

  1. POST uc.aigostar.com/v1.0/connect/token        -> access_token
  2. GET  uc.aigostar.com/v1.0/connect/authorize   -> authCode
  3. POST api.link.aliyun.com/living/account/region/get -> OA host
  4. POST {oaHost}/api/prd/loginbyoauth.json        -> sid
  5. POST {iotHost}/account/createSessionByAuthCode -> iotToken

All IoT gateway calls use Alibaba API Gateway x-ca-signature (HMAC-SHA1).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from . import const
from .util import encrypt_password, http_date

_UUID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class NeedSecurityCodeError(Exception):
    """Server requires an email verification code (UC code UC/NEED_SECURITY_CODE)."""


class AigoApiError(Exception):
    """Generic API error with the raw response attached."""

    def __init__(self, message: str, response: dict | None = None):
        super().__init__(message)
        self.response = response


# ---------------------------------------------------------------------------
# UC signature helpers
# ---------------------------------------------------------------------------

def _uc_md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def _uc_sign(method: str, url: str, timestamp: str) -> str:
    """Signature = MD5(AppKey + AESKey + timestamp + METHOD + path [+ sortedParams])."""
    base_url, sorted_params = url, ""
    if "?" in url:
        base_url, qs = url.split("?", 1)
        pairs: dict[str, str] = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k] = v
        sorted_params = ",".join(f"{k}{v}" for k, v in sorted(pairs.items()))
    sign_key = const.UC_APP_KEY + const.AES_KEY + timestamp + method.upper() + base_url
    if sorted_params:
        sign_key += sorted_params
    return _uc_md5_upper(sign_key)


def _uc_headers(method: str, url: str) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "AppKey": const.UC_APP_KEY,
        "Timestamp": ts,
        "TenantId": const.UC_TENANT_ID,
        "Signature": _uc_sign(method, url, ts),
    }


# ---------------------------------------------------------------------------
# x-ca-signature helpers (Alibaba API Gateway)
# ---------------------------------------------------------------------------

def _content_md5(body: bytes) -> str:
    return base64.b64encode(hashlib.md5(body).digest()).decode()


def _sign(secret: str, canonical: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), canonical.encode(), hashlib.sha1).digest()
    ).decode()


class AigoCloudClient:
    """High-level synchronous cloud client for one AigoSmart account."""

    def __init__(self, iot_host: str = const.DEFAULT_IOT_HOST) -> None:
        self.iot_host = iot_host
        self.iot_base = f"https://{iot_host}"
        self.iot_token: str | None = None
        self.refresh_token: str | None = None
        self.identity_id: str | None = None
        self.token_expire: int = 7200
        self.token_created: float = 0.0

    # ------------------------------------------------------------------
    # Generic IoT gateway call
    # ------------------------------------------------------------------

    def _call(
        self,
        path: str,
        params: dict,
        api_ver: str = "1.0.0",
        iot_token: str | None = None,
        base_url: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()).upper(),
            "version": "1.0",
            "request": {
                "language": "en-US",
                "appKey": const.APP_KEY,
                "apiVer": api_ver,
            },
            "params": params,
        }
        if iot_token:
            body["request"]["iotToken"] = iot_token

        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        content_md5 = _content_md5(body_bytes)
        timestamp = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4()).upper()
        date = http_date()

        sign_headers = {
            "x-ca-key": const.APP_KEY,
            "x-ca-nonce": nonce,
            "x-ca-stage": "RELEASE",
            "x-ca-timestamp": timestamp,
            "x-ca-version": "1",
        }
        canonical = (
            f"POST\n{const.ACCEPT}\n{content_md5}\n{const.CONTENT_TYPE}\n{date}\n"
            + "\n".join(f"{k}:{sign_headers[k]}" for k in sorted(sign_headers))
            + f"\n{path}"
        )
        signature = _sign(const.APP_SECRET, canonical)

        headers = {
            "Content-Type": const.CONTENT_TYPE,
            "Accept": const.ACCEPT,
            "Content-MD5": content_md5,
            "Date": date,
            "X-Ca-Key": const.APP_KEY,
            "X-Ca-Nonce": nonce,
            "X-Ca-Timestamp": timestamp,
            "X-Ca-Stage": "RELEASE",
            "X-Ca-Version": "1",
            "X-Ca-Signature-Headers": ",".join(sorted(sign_headers)),
            "X-Ca-Signature-Method": "HmacSHA1",
            "X-Ca-Signature": signature,
        }

        url = (base_url or self.iot_base) + path
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                result = json.loads(raw)
            except Exception:
                raise AigoApiError(f"HTTP {exc.code}: {raw[:300]!r}") from exc

        if result.get("code", -1) != 200:
            raise AigoApiError(f"API error {path}: {result}", response=result)
        return result

    # ------------------------------------------------------------------
    # Steps 1+2: UC login + authorize
    # ------------------------------------------------------------------

    def uc_login(self, email: str, password: str, security_code: str = "") -> dict:
        account_type = "email" if "@" in email else "phone_number"
        form: dict[str, str] = {
            "account_type": account_type,
            "username": email.strip(),
            "password": encrypt_password(password),
            "grant_type": "password",
            "client_id": const.CLIENT_ID,
            "client_secret": const.CLIENT_SECRET,
            "cuid": uuid.uuid5(_UUID_NS, "aigosmart-python").hex,
        }
        if security_code:
            form["security_code"] = security_code

        login_url = const.UC_BASE + const.UC_LOGIN_PATH
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers.update(_uc_headers("POST", login_url))
        req = urllib.request.Request(
            login_url,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                err = json.loads(raw)
            except Exception:
                raise AigoApiError(f"UC login HTTP {exc.code}: {raw[:300]!r}") from exc
            if err.get("code") == "UC/NEED_SECURITY_CODE":
                raise NeedSecurityCodeError(err.get("message", "security code required"))
            raise AigoApiError(
                f"UC login failed: {err.get('error_description', err)}", response=err
            ) from exc

        if "access_token" not in result:
            raise AigoApiError(f"UC login: no access_token in response: {result}", response=result)
        return result

    def uc_authorize(self, access_token: str) -> str:
        params = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": const.SMARTAPP_ID,
                "redirect_uri": "none",
                "scope": "openid profile",
                "response_mode": "json",
            }
        )
        authorize_url = const.UC_BASE + const.UC_AUTHORIZE_PATH + "?" + params
        headers = {"Authorization": f"Bearer {access_token}"}
        headers.update(_uc_headers("GET", authorize_url))
        req = urllib.request.Request(authorize_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        code = result.get("code", "")
        if not code:
            raise AigoApiError(f"Authorize failed: {result}", response=result)
        return code

    # ------------------------------------------------------------------
    # Steps 3+4+5: region -> OA login -> IoT session
    # ------------------------------------------------------------------

    def resolve_oa_host(self, auth_code: str) -> str:
        try:
            result = self._call(
                const.REGION_API_PATH,
                {"type": "THIRD_AUTHCODE", "authCode": auth_code},
                api_ver="1.0.2",
                base_url=const.REGION_API_HOST,
            )
            host = result.get("data", {}).get("oaApiGatewayEndpoint", "")
            if host:
                return host
        except AigoApiError:
            pass
        return const.OA_HOST_FALLBACK

    def oa_login(self, auth_code: str, oa_host: str) -> str:
        oauth_map = {
            "oauthPlateform": 23,  # integer on purpose (API quirk)
            "accessToken": None,
            "openId": None,
            "oauthAppKey": const.APP_KEY,
            "tokenType": None,
            "authCode": auth_code,
            "userData": None,
        }
        form = {
            "loginByOauthRequest": json.dumps(oauth_map, separators=(",", ":")),
        }
        body = urllib.parse.urlencode(form).encode("utf-8")

        content_type = "application/x-www-form-urlencoded; charset=UTF-8"
        timestamp = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4()).upper()
        date = http_date()
        sign_headers = {
            "x-ca-key": const.APP_KEY,
            "x-ca-nonce": nonce,
            "x-ca-stage": "RELEASE",
            "x-ca-timestamp": timestamp,
            "x-ca-version": "1",
        }
        resource = (
            const.OA_LOGIN_PATH
            + "?"
            + "&".join(f"{k}={v}" for k, v in sorted(form.items()))
        )
        canonical = (
            f"POST\n{const.ACCEPT}\n\n{content_type}\n{date}\n"
            + "\n".join(f"{k}:{sign_headers[k]}" for k in sorted(sign_headers))
            + f"\n{resource}"
        )
        signature = _sign(const.APP_SECRET, canonical)

        headers = {
            "Content-Type": content_type,
            "Accept": const.ACCEPT,
            "Date": date,
            "X-Ca-Key": const.APP_KEY,
            "X-Ca-Nonce": nonce,
            "X-Ca-Timestamp": timestamp,
            "X-Ca-Stage": "RELEASE",
            "X-Ca-Version": "1",
            "X-Ca-Signature-Headers": ",".join(sorted(sign_headers)),
            "X-Ca-Signature-Method": "HmacSHA1",
            "X-Ca-Signature": signature,
        }
        url = f"https://{oa_host}{const.OA_LOGIN_PATH}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise AigoApiError(f"OA login HTTP {exc.code}: {raw[:300]!r}") from exc

        data = result.get("data", {})
        sid = data.get("data", {}).get("loginSuccessResult", {}).get("sid", "")
        if not sid:
            raise AigoApiError(
                f"OA login failed: code={data.get('code')}, message={data.get('message')}",
                response=result,
            )
        return sid

    def create_session(self, sid: str) -> dict:
        result = self._call(
            const.PATH_CREATE_SESSION,
            {"request": {"authCode": sid, "appKey": const.APP_KEY, "accountType": "OA_SESSION"}},
            api_ver="1.0.4",
        )
        data = result.get("data", {})
        if "iotToken" not in data:
            raise AigoApiError(f"createSession: no iotToken: {result}", response=result)
        return data

    # ------------------------------------------------------------------
    # Full login
    # ------------------------------------------------------------------

    def login(self, email: str, password: str, security_code: str = "") -> None:
        uc = self.uc_login(email, password, security_code)
        auth_code = self.uc_authorize(uc["access_token"])
        oa_host = self.resolve_oa_host(auth_code)
        sid = self.oa_login(auth_code, oa_host)
        session = self.create_session(sid)
        self.iot_token = session["iotToken"]
        self.refresh_token = session.get("refreshToken", "")
        self.identity_id = session.get("identityId", "")
        self.token_expire = int(session.get("iotTokenExpire", 7200))
        self.token_created = time.time()

    def refresh(self) -> None:
        if not (self.refresh_token and self.identity_id):
            raise AigoApiError("No refresh token available; full login required")
        result = self._call(
            const.PATH_REFRESH_SESSION,
            {"refreshToken": self.refresh_token, "identityId": self.identity_id},
            api_ver="1.0.4",
        )
        data = result.get("data", {})
        if "iotToken" not in data:
            raise AigoApiError(f"Refresh failed: {result}", response=result)
        self.iot_token = data["iotToken"]
        self.refresh_token = data.get("refreshToken", self.refresh_token)
        self.identity_id = data.get("identityId", self.identity_id)
        self.token_expire = int(data.get("iotTokenExpire", 7200))
        self.token_created = time.time()

    def ensure_token(self) -> str:
        if self.iot_token is None:
            raise AigoApiError("Not logged in")
        if time.time() - self.token_created > self.token_expire - 600:
            self.refresh()
        assert self.iot_token is not None
        return self.iot_token

    # ------------------------------------------------------------------
    # Devices / properties / TSL / services
    # ------------------------------------------------------------------

    def list_devices(self) -> list[dict]:
        result = self._call(
            const.PATH_LIST_DEVICES,
            {"pageNo": 1, "pageSize": 200},
            api_ver="1.0.8",
            iot_token=self.ensure_token(),
        )
        data = result.get("data", {})
        items = data.get("data", []) if isinstance(data, dict) else data
        return items or []

    def get_device_by_id(self, iot_id: str) -> dict:
        result = self._call(
            const.PATH_GET_BY_ACCOUNT_AND_DEV,
            {"iotId": iot_id},
            api_ver="1.0.2",
            iot_token=self.ensure_token(),
        )
        return result.get("data", {})

    def get_properties(self, iot_id: str) -> dict:
        result = self._call(
            const.PATH_PROPS_GET,
            {"iotId": iot_id},
            iot_token=self.ensure_token(),
        )
        data = result.get("data", {})
        return {k: v.get("value") for k, v in data.items() if isinstance(v, dict)}

    def set_properties(self, iot_id: str, items: dict) -> dict:
        return self._call(
            const.PATH_PROPS_SET,
            {"iotId": iot_id, "items": items},
            iot_token=self.ensure_token(),
        )

    def get_tsl(self, iot_id: str) -> dict:
        result = self._call(
            const.PATH_TSL_GET,
            {"iotId": iot_id},
            iot_token=self.ensure_token(),
        )
        return result.get("data", {})

    def get_status(self, iot_id: str) -> dict:
        result = self._call(
            const.PATH_STATUS_GET,
            {"iotId": iot_id},
            api_ver="1.0.5",
            iot_token=self.ensure_token(),
        )
        return result.get("data", {})

    def invoke_service(self, iot_id: str, identifier: str, args: dict | None = None) -> dict:
        return self._call(
            const.PATH_SERVICE_INVOKE,
            {"iotId": iot_id, "identifier": identifier, "args": args or {}},
            iot_token=self.ensure_token(),
        )

    # ------------------------------------------------------------------
    # Local-control key provisioning (ALCS accessKey/accessToken)
    # ------------------------------------------------------------------

    def get_alcs_access_info(self, iot_ids: list[str]) -> list[dict]:
        """Fetch per-device ALCS credentials (accessKey/accessToken) for local control.

        Uses the same gateway endpoint as the Android app's AlcsAuthHttpRequest.
        """
        result = self._call(
            const.PATH_ALCS_ACCESS_INFO,
            {"iotIdList": iot_ids},
            iot_token=self.ensure_token(),
        )
        data = result.get("data", {})
        # Response contains alcsDeviceDTOList
        return data.get("alcsDeviceDTOList", []) if isinstance(data, dict) else []

    # ------------------------------------------------------------------
    # Enrollee discovery (devices in pairing mode) + bind/unbind
    # ------------------------------------------------------------------

    def list_enrollees(self, product_keys: list[str] | None = None) -> list[dict]:
        """List devices in pairing mode reported by bound devices on the LAN.

        Implements the registrar side of the AWSS enrollee mechanism (the
        bound devices report unprovisioned neighbours via MQTT; the app
        queries the aggregated list through this API).
        """
        params: dict = {}
        if product_keys:
            params["productKeys"] = product_keys
        result = self._call(
            const.PATH_AWSS_ENROLLEE_LIST,
            params,
            iot_token=self.ensure_token(),
        )
        data = result.get("data", {})
        if isinstance(data, list):
            return data
        return data.get("data", data.get("list", [])) or []

    def filter_enrollee_products(self, enrollees: list[dict]) -> list[dict]:
        """Ask the cloud which enrollees are supported products for this app."""
        result = self._call(
            const.PATH_AWSS_PRODUCT_FILTER,
            {"enrolleeList": enrollees},
            iot_token=self.ensure_token(),
        )
        return result.get("data", {}).get("list", []) or []

    def bind_ble_device(
        self,
        product_key: str,
        device_name: str,
        token: str | None = None,
        cipher: dict | None = None,
    ) -> dict:
        """Bind a BLE-provisioned device to the account (cloud registration)."""
        params: dict = {"productKey": product_key, "deviceName": device_name}
        if token:
            params["token"] = token
        if cipher:
            params.update(cipher)
        result = self._call(
            const.PATH_AWSS_BLE_USER_BIND,
            params,
            iot_token=self.ensure_token(),
        )
        return result.get("data", {})

    def unbind_device(self, iot_id: str) -> dict:
        """Unbind a subdevice from the account."""
        result = self._call(
            const.PATH_AWSS_SUBDEVICE_UNBIND,
            {"iotId": iot_id},
            iot_token=self.ensure_token(),
        )
        return result.get("data", {})
