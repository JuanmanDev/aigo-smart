"""Add-device flow for AigoSmart: BLE provisioning via HA bluetooth.

Uses the Breeze provisioner (python_client/aigosmart/breeze_provision.py)
which runs over bleak — HA's bluetooth integration exposes ESPHome
bluetooth_proxy adapters transparently, so this works even when HA
itself has no local BLE adapter.

Flow (config flow steps):
  1. scan      — list pairing-mode devices (FEB3 + Breeze scan record)
  2. wifi      — ask for SSID/password for the selected device
  3. provision — BLE provision + cloud bind, then reload the coordinator

Account login (user / reauth / reconfigure) shares one path: when the login
server answers UC/NEED_SECURITY_CODE a code email is sent and the `code`
step asks for it (with a "resend" option). The code is single-use and is
never stored — once verified, the server trusts this client's fixed cuid.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AigoSmartApiClient, InvalidCredentialsError, NeedSecurityCodeError
from .const import CONF_SECURITY_CODE, CONF_SSID, CONF_WIFI_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_RESEND = "resend"
# Sentinel from _async_try_login: the server wants an email code
NEED_CODE = "need_code"

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_EMAIL_SELECTOR = TextSelector(TextSelectorConfig(autocomplete="username"))

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): _EMAIL_SELECTOR,
    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
})

STEP_CODE_SCHEMA = vol.Schema({
    vol.Optional(CONF_SECURITY_CODE): str,
    vol.Optional(CONF_RESEND, default=False): bool,
})

# Blank password keeps the stored one
STEP_REAUTH_SCHEMA = vol.Schema({
    vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
})

STEP_WIFI_SCHEMA = vol.Schema({
    vol.Required(CONF_SSID): str,
    vol.Required(CONF_WIFI_PASSWORD): str,
})

BREEZE_SERVICE = "0000feb3-0000-1000-8000-00805f9b34fb"


class AigoSmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._selected: dict | None = None
        # "user" | "reauth" | "reconfigure" — decides how _finish stores the result
        self._mode: str = "user"
        self._code_sent: bool = False

    # ------------------------------------------------------------------
    # Account login: initial setup
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._mode = "user"
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            result = await self._async_login_step(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_EMAIL: self._email}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Account login: re-authentication (started by HA on ConfigEntryAuthFailed)
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._mode = "reauth"
        self._email = entry_data[CONF_EMAIL]
        self._password = entry_data[CONF_PASSWORD]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_PASSWORD):
                self._password = user_input[CONF_PASSWORD]
            result = await self._async_login_step(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    # ------------------------------------------------------------------
    # Account login: reconfigure (change email/password from the UI)
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._mode = "reconfigure"
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
            result = await self._async_login_step(errors)
            if result is not None:
                return result
        schema = vol.Schema({
            vol.Required(CONF_EMAIL): _EMAIL_SELECTOR,
            vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
        })
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_EMAIL: self._email or entry.data[CONF_EMAIL]}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Account login: email verification code
    # ------------------------------------------------------------------

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = "".join((user_input.get(CONF_SECURITY_CODE) or "").split())
            if user_input.get(CONF_RESEND):
                self._code_sent = await self._request_security_code()
            elif not code:
                errors["base"] = "invalid_code"
            else:
                error = await self._async_try_login(code)
                if error is None:
                    return await self._finish()
                errors["base"] = "invalid_code" if error == NEED_CODE else error
        if not self._code_sent and "base" not in errors:
            errors["base"] = "code_send_failed"
        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    # ------------------------------------------------------------------
    # Add Device: step 1 — scan
    # ------------------------------------------------------------------

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        discovered: list[dict] = []

        # --- BLE pairing-mode devices (FEB3) via HA bluetooth (works with
        #     ESPHome bluetooth_proxy since it exposes remote adapters) ---
        try:
            from .ble_discovery import async_discover_aigo_ble
            discovered.extend(async_discover_aigo_ble(self.hass))
        except Exception as exc:
            _LOGGER.debug("BLE discovery unavailable: %s", exc)

        # --- WiFi enrollee list (devices reported by bound devices) ---
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if entries:
            state = self.hass.data[DOMAIN][entries[0].entry_id]
            try:
                enrollees = await self.hass.async_add_executor_job(
                    state["client"].list_enrollees)
                for e in enrollees:
                    discovered.append({
                        "product_key": e.get("productKey", ""),
                        "device_name": e.get("deviceName", ""),
                        "pairing": True,
                        "source": "cloud_enrollee",
                    })
            except Exception as exc:
                _LOGGER.debug("Enrollee list failed: %s", exc)

        if user_input is not None and "discovered" in user_input:
            sel_key = user_input["discovered"]
            self._selected = next(
                (d for d in discovered
                 if d.get("address") == sel_key or d.get("product_key") == sel_key), None)
            if self._selected is None:
                return self.async_show_form(
                    step_id="add_device",
                    data_schema=vol.Schema({}),
                    errors={"base": "unknown_device"},
                )
            if self._selected.get("pairing") and self._selected.get("address"):
                return await self.async_step_wifi()
            # cloud enrollee: binding is cloud-side only
            return self.async_create_entry(
                title=self._selected.get("name") or "AigoSmart device",
                data={"discovered_device": self._selected},
            )

        names: dict[str, str] = {}
        for d in discovered:
            key = d.get("address") or d.get("product_key")
            if key:
                if d.get("rssi") is not None:
                    names[key] = f"{d.get('name') or d.get('product_key','?')} ({d['rssi']}dBm)"
                else:
                    names[key] = f"{d.get('product_key','?')}/{d.get('device_name','?')} (cloud)"
        if not names:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema({vol.Required("discovered"): vol.In(names)}),
        )

    # ------------------------------------------------------------------
    # Add Device: step 2 — WiFi credentials
    # ------------------------------------------------------------------

    async def async_step_wifi(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            ssid = user_input[CONF_SSID]
            wifi_pw = user_input[CONF_WIFI_PASSWORD]
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if not entries:
                return self.async_abort(reason="not_loaded")
            state = self.hass.data[DOMAIN][entries[0].entry_id]
            client: AigoSmartApiClient = state["client"]
            address = self._selected["address"]

            from .lib.breeze_provision import BreezeProvisioner

            async def _cloud_bind(info: dict) -> dict:
                return await self.hass.async_add_executor_job(
                    client.bind_ble_device, info["productKey"], info["deviceName"])

            provisioner = BreezeProvisioner(address, _cloud_bind)
            try:
                await self.hass.async_add_executor_job(provisioner._connect)
                result = await provisioner.provision(ssid, wifi_pw)
            except Exception as exc:
                _LOGGER.warning("BLE provisioning failed: %s", exc)
                errors["base"] = "provision_failed"
            else:
                # force an immediate coordinator refresh so the new device
                # is picked up by autodiscovery right away
                try:
                    await state["coordinator"].async_refresh()
                except Exception as exc:
                    _LOGGER.debug("Post-provision refresh failed: %s", exc)
                return self.async_create_entry(
                    title=result["device"].get("deviceName") or "AigoSmart device",
                    data={"provisioned": result["device"], "iot_id": None},
                )
        return self.async_show_form(step_id="wifi", data_schema=STEP_WIFI_SCHEMA, errors=errors)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _async_try_login(self, code: str = "") -> str | None:
        """Log in with the flow's credentials; None on success, else an error key."""
        client = AigoSmartApiClient(self._email, self._password, security_code=code)
        try:
            await self.hass.async_add_executor_job(client.login)
        except NeedSecurityCodeError:
            return NEED_CODE
        except InvalidCredentialsError as exc:
            _LOGGER.info("AigoSmart login rejected: %s", exc)
            return "invalid_code" if code and exc.is_code_error else "invalid_auth"
        except Exception as exc:
            _LOGGER.warning("AigoSmart login failed: %s", exc)
            return "cannot_connect"
        return None

    async def _async_login_step(self, errors: dict[str, str]) -> FlowResult | None:
        """Shared submit handler: finish, go to the code step, or fill errors."""
        error = await self._async_try_login()
        if error is None:
            return await self._finish()
        if error == NEED_CODE:
            self._code_sent = await self._request_security_code()
            return await self.async_step_code()
        errors["base"] = error
        return None

    async def _request_security_code(self) -> bool:
        """Ask the server to email a login code. True when the request was accepted."""
        import json
        import urllib.request

        from .lib import const
        from .lib.cloud import _uc_headers

        body = {
            "send_to": self._email.strip(),
            "account_type": "email" if "@" in self._email else "phone_number",
            "action": "LoginSecurity",
            "re_send_count": 0,
            "captcha_token": "",
        }
        url = const.SMART_API_BASE + const.PATH_SEND_CODE
        headers = {"Content-Type": const.CONTENT_TYPE}
        headers.update(_uc_headers("POST", url))

        def _send() -> bool:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15):
                    return True
            except Exception as exc:
                # the code step is still shown, with a "resend" option
                _LOGGER.warning("Sending the AigoSmart verification code failed: %s", exc)
                return False

        return await self.hass.async_add_executor_job(_send)

    async def _finish(self) -> FlowResult:
        # The verification code is single-use: never persist it (a stale code
        # sent on a later re-login could make the server reject that login).
        data = {CONF_EMAIL: self._email, CONF_PASSWORD: self._password}
        title = f"AigoSmart ({self._email})"
        if self._mode == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data)
        if self._mode == "reconfigure":
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), title=title, data=data)
        await self.async_set_unique_id("aigosmart_account")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AigoSmartOptionsFlow()


class AigoSmartOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return self.async_create_entry(title="", data={})
