"""Light platform for BTicino Door Entry v1."""

# pyright: reportMissingImports=false, reportExplicitAny=false
from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COORDINATOR_KEY, DEVICE_TYPE_LIGHT, DOMAIN
from .coordinator import BticinoV1Coordinator
from .entity import BticinoV1Entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BticinoV1Coordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_KEY]

    # With light_as_lock the same modules are exposed as momentary locks by the
    # lock platform instead, so this platform adds nothing.
    if entry.options.get("light_as_lock", False):
        _LOGGER.debug("light_as_lock is enabled; light modules are handled by the lock platform")
        async_add_entities([])
        return

    entities = [
        BticinoV1Light(coordinator, entry, module_id)
        for module_id, module in coordinator.data.get("modules", {}).items()
        if module.get("device") == DEVICE_TYPE_LIGHT
    ]
    _LOGGER.debug("Adding %d light entities: %s", len(entities), [entity.name for entity in entities])
    async_add_entities(entities)


class BticinoV1Light(BticinoV1Entity, LightEntity):
    # A relay is on/off only. HA refuses to add a LightEntity that declares no
    # colour mode, which previously dropped this entity at startup.
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.ONOFF}

    def __init__(self, coordinator: BticinoV1Coordinator, entry: ConfigEntry, module_id: str) -> None:
        super().__init__(coordinator, entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_light_{module_id}"
        module = coordinator.data.get("modules", {}).get(module_id, {})
        self._attr_name = module.get("name", "Light")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        module = self.coordinator.data.get("modules", {}).get(self._module_id, {})
        return module.get("status") == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning on light module %s", self._module_id)
        await self.coordinator.async_set_light(self._module_id, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning off light module %s", self._module_id)
        await self.coordinator.async_set_light(self._module_id, False)
        self.async_write_ha_state()
