"""aigosmart.set_feeding_plan: replace a pet feeder's whole schedule.

The per-meal entities (switch / time / portions, see pet.py) edit the meals
that already exist; this action adds or removes meals, like the app. Every
meal runs every day (the weekday bits of feedingPlan are not decoded yet).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .helpers import is_pet_feeder_device
from .pet import (
    MANUAL_FEED_DEFAULT_RANGE,
    MAX_PLAN_MEALS,
    PROP_MANUAL_FEEDING,
    build_feeding_plan,
    tsl_int_range,
)
from .set_properties import ATTR_DEVICE_ID, resolve_device

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_FEEDING_PLAN = "set_feeding_plan"
ATTR_MEALS = "meals"

MEAL_SCHEMA = vol.Schema({
    vol.Required("time"): cv.time,
    vol.Required("portions"): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
    vol.Optional("enabled", default=True): cv.boolean,
})


def _unique_times(meals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    times = [(m["time"].hour, m["time"].minute) for m in meals]
    if len(times) != len(set(times)):
        raise vol.Invalid("two meals have the same time")
    return meals


SCHEMA = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): cv.string,
    vol.Required(ATTR_MEALS): vol.All(
        cv.ensure_list, [MEAL_SCHEMA], vol.Length(min=1, max=MAX_PLAN_MEALS),
        _unique_times),
})


async def async_handle_set_feeding_plan(hass: HomeAssistant, call: ServiceCall) -> None:
    iot_id, state, name = resolve_device(hass, call.data[ATTR_DEVICE_ID])
    coordinator = state["coordinator"]
    dev = next((d for d in coordinator.devices if d.get("iotId") == iot_id), {})
    if not is_pet_feeder_device(dev, state.get("pk_catalog", {})):
        raise ServiceValidationError(f"{name} is not a pet feeder")
    meals = call.data[ATTR_MEALS]
    tsl = coordinator.tsl_models.get(dev.get("productKey", "")) or {}
    low, high = tsl_int_range(tsl, PROP_MANUAL_FEEDING, MANUAL_FEED_DEFAULT_RANGE)
    for meal in meals:
        if not low <= meal["portions"] <= high:
            raise ServiceValidationError(
                f"portions must be between {low} and {high} on {name}")
    plan = build_feeding_plan(meals)
    _LOGGER.info("AigoSmart feeding plan for %s: %s", name, plan)
    try:
        await hass.async_add_executor_job(
            state["client"].set_properties, iot_id, {"feedingPlan": plan})
    except Exception as exc:
        raise HomeAssistantError(f"AigoSmart rejected the schedule for {name}: {exc}") from exc
    coordinator.async_set_optimistic_props(iot_id, {"feedingPlan": plan}, hold_duration=5.0)
    await coordinator.async_refresh_device(iot_id)


def async_register(hass: HomeAssistant) -> None:
    """Register the action (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_FEEDING_PLAN):
        return

    async def _handle(call: ServiceCall) -> None:
        await async_handle_set_feeding_plan(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_SET_FEEDING_PLAN, _handle, schema=SCHEMA)
