"""Platform for sensor integration."""

# from __future__ import annotations
import logging
from datetime import datetime
from datetime import timedelta

import homeassistant.components.recorder.util as recorder_util

try:
    from homeassistant.components.recorder.const import (
        DATA_INSTANCE as RECORDER_DATA_INSTANCE,
    )
except ImportError:  # NEW Home Assistant 2024.08
    from homeassistant.helpers.recorder import (
        DATA_INSTANCE as RECORDER_DATA_INSTANCE,
    )
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.recorder.statistics import clear_statistics
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_STATE
from homeassistant.const import CONF_USERNAME
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.core import CoreState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.update_coordinator import TimestampDataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import AiguesApiClient
from .const import API_ERROR_TOKEN_REVOKED
from .const import ATTR_LAST_MEASURE
from .const import CONF_CONTRACT
from .const import CONF_VALUE
from .const import DEFAULT_SCAN_PERIOD
from .const import DOMAIN
from .const import CONF_2CAPTCHA_APIKEY

from typing import Optional

_LOGGER = logging.getLogger(__name__)


def get_db_instance(hass):
    """Workaround for older HA versions."""
    try:
        return recorder_util.get_instance(hass)
    except AttributeError:
        return hass


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up entry."""
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("calling async_setup_entry")

    username = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]
    twocaptcha_api_key = config_entry.data[CONF_2CAPTCHA_APIKEY]
    contracts = config_entry.data[CONF_CONTRACT]
    token = config_entry.data.get("token")

    contadores = list()

    for contract in contracts:
        coordinator = ContratoAgua(hass, username, password, twocaptcha_api_key, contract, token=token, entry_id=config_entry.entry_id)
        contadores.append(ContadorAgua(coordinator))

    # postpone first refresh to speed up startup
    @callback
    async def async_first_refresh(*args):
        for sensor in contadores:
            await sensor.coordinator.async_refresh()

    # ------

    if hass.state == CoreState.running:
        await async_first_refresh()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, async_first_refresh)

    _LOGGER.info("about to add entities")
    async_add_entities(contadores)

    return True


class ContratoAgua(TimestampDataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            username: str,
            password: str,
            twocaptcha_api_key: str,
            contract: str,
            token: str,
            entry_id: str,
            prev_data=None,
    ) -> None:
        """Initialize the data handler."""
        self.reset = prev_data is None

        self._import_in_progress = False

        self.contract = contract.upper()
        self.id = contract.lower()
        self.internal_sensor_id = f"sensor.contador_{self.id}"
        self.entry_id = entry_id

        if not hass.data[DOMAIN].get(self.contract):
            # init data shared store
            hass.data[DOMAIN][self.contract] = {}

        # create alias
        self._data = hass.data[DOMAIN][self.contract]

        # WARN define a pointer to this object
        hass.data[DOMAIN][self.contract]["coordinator"] = self

        # the api object
        self._api = AiguesApiClient(username, password, twocaptcha_api_key, contract)

        if token:
            self._api.set_token(token)

        super().__init__(
            hass,
            _LOGGER,
            name=self.id,
            update_interval=timedelta(seconds=DEFAULT_SCAN_PERIOD),
        )

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.contract}>"

    async def _ensure_token(self) -> None:
        """Ensure API token is valid; refresh and persist it if expired."""
        if not self._api.is_token_expired():
            return

        entry = self.hass.config_entries.async_get_entry(self.entry_id)

        # drop old token to force login
        self.hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != "token"}
        )

        await self.hass.async_add_executor_job(self._api.login)
        new_token = self._api.get_token()

        if new_token:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, "token": new_token}
            )

    async def _async_update_data(self):
        _LOGGER.info(f"Updating coordinator data for {self.contract}")
        TODAY = datetime.now()
        LAST_WEEK = TODAY - timedelta(days=7)
        LAST_TIME_DAYS = None

        # last_measurement = await self.get_last_measurement_stored()
        # _LOGGER.info("Last stored measurement: %s", last_measurement)

        try:
            previous = datetime.fromisoformat(self._data.get(CONF_STATE, ""))
            # FIX: TypeError: can't subtract offset-naive and offset-aware datetimes
            previous = previous.replace(tzinfo=None)
            if previous:
                LAST_TIME_DAYS = (TODAY - previous).days
        except ValueError:
            previous = None

        if previous and (TODAY - previous) <= timedelta(minutes=10):
            _LOGGER.warning("Skipping request update data - too early")
            return

        consumptions = None
        try:
            await self._ensure_token()

            consumptions = await self.hass.async_add_executor_job(
                self._api.consumptions, LAST_WEEK, TODAY + timedelta(days=1), self.contract
            )
        except ConfigEntryAuthFailed as exp:
            _LOGGER.error("Token has expired, cannot check consumptions.")
            raise ConfigEntryAuthFailed from exp
        except Exception as exp:
            self.async_set_update_error(exp)
            if API_ERROR_TOKEN_REVOKED in str(exp):
                raise ConfigEntryAuthFailed from exp

        if not consumptions:
            _LOGGER.error("No consumptions available")
            return False

        self._data["consumptions"] = consumptions

        # get last entry - most updated
        metric = consumptions[-1]
        self._data[CONF_VALUE] = metric["accumulatedConsumption"]
        self._data[CONF_STATE] = metric["datetime"]

        # await self._clear_statistics()
        try:
            await self._async_import_statistics(consumptions, fill_to_now=True)
        except Exception:
            _LOGGER.exception("Failed to import statistics")

        if LAST_TIME_DAYS and LAST_TIME_DAYS >= 7:
            await self.import_old_consumptions(days=LAST_TIME_DAYS)

        return True

    async def _clear_statistics(self) -> None:
        all_ids = await get_db_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass
        )
        to_clear = [
            x["statistic_id"]
            for x in all_ids
            if x["statistic_id"].startswith(self.internal_sensor_id)
        ]

        if to_clear:
            _LOGGER.warning(
                f"About to delete {len(to_clear)} entries from {self.contract}"
            )
            # NOTE: This does not seem to work?
            await get_db_instance(self.hass).async_add_executor_job(
                clear_statistics, self.hass.data[RECORDER_DATA_INSTANCE], to_clear
            )

    async def get_last_measurement_stored(self) -> Optional[datetime]:
        """Placeholder — not used. Implement DB query later if needed."""
        return None

        # last_stored = None
        #
        # all_ids = await get_db_instance(self.hass).async_add_executor_job(
        #     list_statistic_ids, self.hass
        # )
        #
        # for stat_id in all_ids:
        #     if stat_id["statistic_id"] == self.internal_sensor_id:
        #         if stat_id.get("sum") and stat_id["sum"] > last_stored["sum"]:
        #             last_stored = stat_id
        #
        # if last_stored:
        #     _LOGGER.debug(f"Found last stored value: {last_stored}")
        #     return datetime.fromtimestamp(last_stored.get("start_ts"))
        #
        # return None

    async def _async_import_statistics(self, consumptions, fill_to_now=False) -> None:
        if self._import_in_progress:
            _LOGGER.debug("Import already in progress — skipping")
            return

        self._import_in_progress = True
        try:
            # force sort by datetime
            consumptions = sorted(
                consumptions,
                key=lambda x: (
                    dt_util.as_utc(dt_util.parse_datetime(x["datetime"]))
                    if dt_util.parse_datetime(x["datetime"]) is not None
                    else datetime.min
                ),
            )

            # Deduplicate per hour: keep max accumulatedConsumption for each hour
            normalized = {}
            for metric in consumptions:
                dt = dt_util.parse_datetime(metric["datetime"])
                if dt is None:
                    continue
                start_ts = dt_util.as_utc(dt).replace(minute=0, second=0, microsecond=0)
                val = round(metric["accumulatedConsumption"], 4)
                current = normalized.get(start_ts)
                if current is None or val > current:
                    normalized[start_ts] = val

            items = sorted(normalized.items())  # list of (start_ts, state)

            stats = []
            last_state = None
            last_ts = None

            for start_ts, state in items:
                stats.append(
                    {
                        "start": start_ts,
                        "state": state,
                        "sum": state,
                    }
                )

                last_state = state
                last_ts = start_ts

            if fill_to_now == True and last_state is not None and last_ts is not None:
                now_utc = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
                fill_ts = last_ts + timedelta(hours=1)

                while fill_ts <= now_utc:
                    stats.append(
                        {
                            "start": fill_ts,
                            "state": last_state,
                            "sum": last_state,
                        }
                    )
                    _LOGGER.debug(
                        "Extending stats for %s at %s with last_state=%s",
                        self.contract,
                        fill_ts,
                        last_state,
                    )
                    fill_ts += timedelta(hours=1)

            if stats:
                metadata = {
                    "has_mean": False,
                    "has_sum": True,
                    "name": f"Contador {self.id}",
                    "source": "recorder",
                    "statistic_id": self.internal_sensor_id,
                    "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
                }
                _LOGGER.debug(f"Adding metric: {metadata} {stats}")
                async_import_statistics(self.hass, metadata, stats)

                _LOGGER.info("Imported %d points for %s", len(stats), self.contract)
        finally:
            self._import_in_progress = False

    async def clear_all_stored_data(self) -> None:
        await self._clear_statistics()

    async def import_old_consumptions(self, days: int = 365) -> None:
        today = datetime.now()
        one_year_ago = today - timedelta(days=days)

        await self._ensure_token()

        current_date = one_year_ago
        while current_date < today:
            consumptions = await self.hass.async_add_executor_job(
                self._api.consumptions_week, current_date, self.contract
            )

            if consumptions:
                await self._async_import_statistics(consumptions, fill_to_now=False)
            else:
                _LOGGER.warning(f"No data available for {current_date}")

            current_date += timedelta(weeks=1)


class ContadorAgua(CoordinatorEntity, SensorEntity):
    """Representation of a sensor."""

    def __init__(self, coordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = f"Contador {coordinator.id}"
        self._attr_unique_id = coordinator.id
        self._attr_icon = "mdi:water-pump"
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    @property
    def native_value(self):
        return self.coordinator._data.get(CONF_VALUE, None)

    @property
    def last_measurement(self):
        try:
            last_measure = datetime.fromisoformat(
                self.coordinator._data.get(CONF_STATE, "")
            )
        except ValueError:
            last_measure = None
        return last_measure

    @property
    def extra_state_attributes(self):
        attrs = {ATTR_LAST_MEASURE: self.last_measurement}
        return attrs
