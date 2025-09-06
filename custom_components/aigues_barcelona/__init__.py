"""Integration for Aigues de Barcelona."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.const import CONF_TOKEN
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import AiguesApiClient
from .const import DOMAIN
from .const import CONF_2CAPTCHA_APIKEY
from .service import async_setup as setup_service

from homeassistant.exceptions import ConfigEntryNotReady

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = AiguesApiClient(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], entry.data.get(CONF_2CAPTCHA_APIKEY, ""))

    if token := entry.data.get(CONF_TOKEN):
        api.set_token(token)

    if api.is_token_expired():
        try:
            hass.config_entries.async_update_entry(
                entry,
                data={k: v for k, v in entry.data.items() if k != "token"}
            )

            await hass.async_add_executor_job(api.login)
            new_token = api.get_token()

            if new_token:
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, "token": new_token}
                )
        except:
            raise ConfigEntryNotReady

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await setup_service(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if entry.entry_id in hass.data[DOMAIN].keys():
            hass.data[DOMAIN].pop(entry.entry_id)
    if not hass.data[DOMAIN]:
        del hass.data[DOMAIN]

    return unload_ok
