import logging
from .const import DOMAIN

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_reset_and_refresh_data(call: ServiceCall) -> None:
        contract = next(iter(hass.data[DOMAIN]), None)
        if not contract:
            _LOGGER.error("No contracts available")
            return

        coordinator = hass.data[DOMAIN].get(contract).get("coordinator")
        if not coordinator:
            _LOGGER.error(f"Contract coordinator for {contract} not found")
            return

        _LOGGER.warning(f"Performing reset and refresh for {contract}")

        # Clear existing statistics first
        try:
            await clear_stored_data(hass, coordinator)
        except Exception as e:
            _LOGGER.error(f"Failed to clear statistics for {contract}: {e}")

        # Re-import historical data after clearing statistics
        # This ensures we have complete historical data without conflicts
        try:
            await fetch_historic_data(hass, coordinator)
        except Exception as e:
            _LOGGER.error(f"Failed to fetch historic data for {contract}: {e}")

        # Trigger a normal update to get the latest data
        try:
            await coordinator.async_refresh()
            _LOGGER.info(f"Current data refresh completed for {contract}")
        except Exception as e:
            _LOGGER.error(f"Failed to refresh current data for {contract}: {e}")

    hass.services.async_register(
        DOMAIN, "reset_and_refresh_data", handle_reset_and_refresh_data
    )
    return True


async def clear_stored_data(hass: HomeAssistant, coordinator) -> None:
    _LOGGER.info("Clearing stored statistics...")
    await coordinator._clear_statistics()
    _LOGGER.info("Statistics cleared successfully")


async def fetch_historic_data(hass: HomeAssistant, coordinator) -> None:
    _LOGGER.info("Fetching historic consumption data...")
    await coordinator.import_old_consumptions(days=365)
    _LOGGER.info("Historic data import completed")

    # Also trigger a normal update to get the latest data
    _LOGGER.info("Fetching current data...")
    await coordinator.async_refresh()
    _LOGGER.info("Current data refresh completed")
