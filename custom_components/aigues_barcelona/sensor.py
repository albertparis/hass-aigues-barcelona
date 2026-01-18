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
from homeassistant.components.recorder.statistics import statistics_during_period

try:
    from homeassistant.components.recorder.models.statistics import StatisticMeanType
except ImportError:
    # Fallback for older HA versions
    StatisticMeanType = None
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

from typing import Dict, List, Optional, Set, Tuple

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
        coordinator = ContratoAgua(
            hass,
            username,
            password,
            twocaptcha_api_key,
            contract,
            token=token,
            entry_id=config_entry.entry_id,
        )
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
                self._api.consumptions,
                LAST_WEEK,
                TODAY + timedelta(days=1),
                self.contract,
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
            await self._async_import_statistics(consumptions, fill_to_now=False)
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

    async def _get_existing_statistics(self, lookback_days: int = 7) -> Set[datetime]:
        """Query existing statistics timestamps to avoid duplicates.

        Returns a set of datetime objects representing hours that already have statistics.
        This prevents conflicts with Home Assistant's hourly statistics compilation.
        """
        existing_timestamps: Set[datetime] = set()
        try:
            start_time = dt_util.utcnow() - timedelta(days=lookback_days)
            existing_stats = await get_db_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                None,
                {self.internal_sensor_id},
                "hour",
                None,  # units
                {"sum"},  # types - we only need sum for our sensor
            )

            if existing_stats and self.internal_sensor_id in existing_stats:
                for stat in existing_stats[self.internal_sensor_id]:
                    if stat.get("start_ts") is not None:
                        existing_ts = dt_util.utc_from_timestamp(stat["start_ts"])
                        existing_ts = existing_ts.replace(
                            minute=0, second=0, microsecond=0
                        )
                        existing_timestamps.add(existing_ts)
                _LOGGER.debug(
                    "Found %d existing statistics for %s (last %d days)",
                    len(existing_timestamps),
                    self.contract,
                    lookback_days,
                )
        except Exception as e:
            _LOGGER.warning(
                "Failed to query existing statistics for %s: %s. Continuing without duplicate check.",
                self.contract,
                e,
            )
        return existing_timestamps

    def _normalize_consumptions(
        self, consumptions: List[Dict]
    ) -> List[Tuple[datetime, float]]:
        """Normalize consumption data to hourly buckets.

        Returns a sorted list of (timestamp, value) tuples, keeping the max value per hour.
        """
        # Sort consumptions by datetime
        consumptions = sorted(
            consumptions,
            key=lambda x: (
                dt_util.as_utc(dt_util.parse_datetime(x["datetime"]))
                if dt_util.parse_datetime(x["datetime"]) is not None
                else datetime.min
            ),
        )

        # Deduplicate per hour: keep max accumulatedConsumption for each hour
        normalized: Dict[datetime, float] = {}
        for metric in consumptions:
            dt = dt_util.parse_datetime(metric["datetime"])
            if dt is None:
                continue
            start_ts = dt_util.as_utc(dt).replace(minute=0, second=0, microsecond=0)
            val = round(metric["accumulatedConsumption"], 4)
            current = normalized.get(start_ts)
            if current is None or val > current:
                normalized[start_ts] = val

        return sorted(normalized.items())

    def _get_statistics_metadata(self) -> Dict:
        """Return metadata for statistics import."""
        metadata = {
            "has_sum": True,
            "name": f"Contador {self.id}",
            "source": "recorder",
            "statistic_id": self.internal_sensor_id,
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
            "unit_class": "volume",  # Required from HA 2026.11
        }
        # Add mean_type for newer HA versions (required from 2026.11)
        if StatisticMeanType is not None:
            metadata["mean_type"] = StatisticMeanType.NONE
        return metadata

    async def _async_import_statistics(self, consumptions, fill_to_now=False) -> None:
        if self._import_in_progress:
            _LOGGER.debug("Import already in progress — skipping")
            return

        self._import_in_progress = True
        try:
            # Query existing statistics to avoid duplicates
            existing_timestamps = await self._get_existing_statistics(lookback_days=7)

            # Normalize to hourly buckets
            items = self._normalize_consumptions(consumptions)
            if not items:
                _LOGGER.debug("No valid consumptions to process for %s", self.contract)
                return

            # Track the most recent data point for fill_to_now (before filtering)
            most_recent_ts, most_recent_state = items[-1]

            # Build stats list, filtering out duplicates
            stats = []
            for start_ts, state in items:
                if start_ts in existing_timestamps:
                    continue
                stats.append({"start": start_ts, "state": state, "sum": state})

            # Fill gaps up to current time (minus 1 hour to avoid conflicts with HA)
            if fill_to_now:
                now_utc = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
                max_fill_ts = now_utc - timedelta(hours=1)
                fill_ts = most_recent_ts + timedelta(hours=1)

                while fill_ts <= max_fill_ts:
                    if fill_ts not in existing_timestamps:
                        stats.append(
                            {
                                "start": fill_ts,
                                "state": most_recent_state,
                                "sum": most_recent_state,
                            }
                        )
                    fill_ts += timedelta(hours=1)

            if stats:
                async_import_statistics(
                    self.hass, self._get_statistics_metadata(), stats
                )
                _LOGGER.info("Imported %d points for %s", len(stats), self.contract)
            else:
                _LOGGER.debug("No new statistics to import for %s", self.contract)
        finally:
            self._import_in_progress = False

    async def clear_all_stored_data(self) -> None:
        await self._clear_statistics()

    async def import_old_consumptions(self, days: int = 365) -> None:
        """Import historical consumption data.

        Fetches consumption data week by week going back the specified number of days.
        Uses a larger lookback window for duplicate checking since we're importing
        historical data.
        """
        today = datetime.now()
        start_date = today - timedelta(days=days)

        await self._ensure_token()

        # Pre-fetch existing statistics for the entire period to avoid duplicates
        existing_timestamps = await self._get_existing_statistics(
            lookback_days=days + 7
        )

        current_date = start_date
        imported_count = 0
        while current_date < today:
            consumptions = await self.hass.async_add_executor_job(
                self._api.consumptions_week, current_date, self.contract
            )

            if consumptions:
                await self._async_import_statistics_with_existing(
                    consumptions, existing_timestamps, fill_to_now=False
                )
                imported_count += 1
            else:
                _LOGGER.debug("No data available for week of %s", current_date)

            current_date += timedelta(weeks=1)

        _LOGGER.info(
            "Completed importing %d weeks of historical data for %s",
            imported_count,
            self.contract,
        )

    async def _async_import_statistics_with_existing(
        self,
        consumptions,
        existing_timestamps: Set[datetime],
        fill_to_now: bool = False,
    ) -> None:
        """Import statistics using pre-fetched existing timestamps.

        This is used for bulk historical imports where we want to check duplicates
        against a pre-fetched set of existing statistics.
        """
        if self._import_in_progress:
            _LOGGER.debug("Import already in progress — skipping")
            return

        self._import_in_progress = True
        try:
            items = self._normalize_consumptions(consumptions)
            if not items:
                return

            # Build stats list, filtering out duplicates
            stats = []
            for start_ts, state in items:
                if start_ts in existing_timestamps:
                    continue
                stats.append({"start": start_ts, "state": state, "sum": state})
                # Add to existing set to prevent duplicates within this import session
                existing_timestamps.add(start_ts)

            if stats:
                async_import_statistics(
                    self.hass, self._get_statistics_metadata(), stats
                )
                _LOGGER.debug(
                    "Imported %d historical points for %s", len(stats), self.contract
                )
        finally:
            self._import_in_progress = False


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
        self._attr_state_class = SensorStateClass.TOTAL
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
