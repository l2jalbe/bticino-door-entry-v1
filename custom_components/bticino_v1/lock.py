"""Lock platform for BTicino Door Entry v1."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    COORDINATOR_KEY,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_LOCK,
    DOMAIN,
    LOCK_RELOCK_DELAY,
)
from .coordinator import BticinoV1Coordinator
from .entity import BticinoV1Entity

_LOGGER = logging.getLogger(__name__)


def build_display_names(modules: dict[str, dict], default: str) -> dict[str, str]:
    """Map module_id -> display name, disambiguating duplicates.

    The API happily returns several modules sharing one name (three locks all
    called "Serratura", for instance). Without a suffix the resulting entities
    are indistinguishable in the UI.
    """
    counts = Counter((module.get("name") or default) for module in modules.values())
    names: dict[str, str] = {}
    for module_id, module in modules.items():
        base = module.get("name") or default
        names[module_id] = f"{base} ({module_id[-4:]})" if counts[base] > 1 else base
    return names


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BticinoV1Coordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_KEY]
    modules = coordinator.data.get("modules", {})

    lock_modules = {mid: m for mid, m in modules.items() if m.get("device") == DEVICE_TYPE_LOCK}
    lock_names = build_display_names(lock_modules, "Lock")
    entities: list[LockEntity] = [
        BticinoV1Lock(coordinator, entry, module_id, lock_names[module_id]) for module_id in lock_modules
    ]
    entity_log: list[tuple[str, str, str]] = [
        ("BticinoV1Lock", module_id, lock_names[module_id]) for module_id in lock_modules
    ]

    # A light module wired to a gate is still a momentary opener, so it belongs
    # on the lock platform -- registering a LockEntity through the light
    # platform makes HA call light.turn_on on it, which it cannot answer.
    if entry.options.get("light_as_lock", False):
        light_modules = {mid: m for mid, m in modules.items() if m.get("device") == DEVICE_TYPE_LIGHT}
        light_names = build_display_names(light_modules, "Light")
        entities.extend(
            BticinoV1LightAsLock(coordinator, entry, module_id, f"{light_names[module_id]} Lock")
            for module_id in light_modules
        )
        entity_log.extend(
            ("BticinoV1LightAsLock", module_id, f"{light_names[module_id]} Lock") for module_id in light_modules
        )

    # Log from the names computed above: reading entity.name before the entity
    # is added to hass can raise, which would fail the whole platform setup.
    _LOGGER.debug("Adding %d lock entities: %s", len(entities), entity_log)
    async_add_entities(entities)


class BticinoV1MomentaryLock(BticinoV1Entity, LockEntity):
    """Lock that pulses open and re-locks itself after LOCK_RELOCK_DELAY."""

    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(
        self,
        coordinator: BticinoV1Coordinator,
        entry: ConfigEntry,
        module_id: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, entry, module_id)
        self._attr_name = name
        self._is_locked = True
        self._relock_cancel: Callable | None = None

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def _async_send_open(self) -> None:
        """Send the open command. Implemented by subclasses."""
        raise NotImplementedError

    async def async_open(self, **kwargs: Any) -> None:
        """Open the door.

        The entity advertises LockEntityFeature.OPEN, so HA routes lock.open
        here. Without this the base class raises NotImplementedError.
        """
        await self.async_unlock(**kwargs)

    async def async_unlock(self, **kwargs: Any) -> None:
        if self._relock_cancel:
            self._relock_cancel()
            self._relock_cancel = None
        self._is_locked = False
        self.async_write_ha_state()
        self._relock_cancel = async_call_later(self.hass, LOCK_RELOCK_DELAY, self._relock_cb)
        _LOGGER.debug("Sending open command for module %s (%s)", self._module_id, self._attr_name)
        try:
            await self._async_send_open()
        except Exception as err:
            _LOGGER.error("Failed to unlock %s: %s", self._module_id, err)
            if self._relock_cancel:
                self._relock_cancel()
                self._relock_cancel = None
            self._is_locked = True
            self.async_write_ha_state()
        else:
            _LOGGER.debug("Open command accepted for module %s", self._module_id)

    @callback
    def _relock_cb(self, *args: Any) -> None:
        self._relock_cancel = None
        self._is_locked = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._relock_cancel:
            self._relock_cancel()
            self._relock_cancel = None
        await super().async_will_remove_from_hass()


class BticinoV1Lock(BticinoV1MomentaryLock):
    """A module the gateway reports as a lock."""

    def __init__(
        self,
        coordinator: BticinoV1Coordinator,
        entry: ConfigEntry,
        module_id: str,
        name: str | None = None,
    ) -> None:
        if name is None:
            module = coordinator.data.get("modules", {}).get(module_id, {})
            name = module.get("name", "Lock")
        super().__init__(coordinator, entry, module_id, name)
        self._attr_unique_id = f"{entry.entry_id}_lock_{module_id}"

    async def _async_send_open(self) -> None:
        await self.coordinator.async_open_lock(self._module_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._relock_cancel is None:
            module = self.coordinator.data.get("modules", {}).get(self._module_id, {})
            self._is_locked = bool(module.get("lock", True))
        self.async_write_ha_state()


class BticinoV1LightAsLock(BticinoV1MomentaryLock):
    """A light module driven as a momentary opener (gate on the light relay)."""

    def __init__(
        self,
        coordinator: BticinoV1Coordinator,
        entry: ConfigEntry,
        module_id: str,
        name: str | None = None,
    ) -> None:
        if name is None:
            module = coordinator.data.get("modules", {}).get(module_id, {})
            name = f"{module.get('name', 'Light')} Lock"
        super().__init__(coordinator, entry, module_id, name)
        self._attr_unique_id = f"{entry.entry_id}_light_lock_{module_id}"

    async def _async_send_open(self) -> None:
        await self.coordinator.async_set_light(self._module_id, True)
