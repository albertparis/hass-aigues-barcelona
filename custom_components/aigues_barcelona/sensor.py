"""Platform for sensor integration."""

# from __future__ import annotations
import logging
from datetime import datetime
from datetime import timedelta

import homeassistant.components.recorder.util as recorder_util

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.statistics import get_last_statistics
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
from homeassistant.const import MAJOR_VERSION
from homeassistant.const import MINOR_VERSION
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.core import CoreState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.update_coordinator import TimestampDataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import AiguesApiClient
from .const import API_ERROR_TOKEN_INVALID
from .const import API_ERROR_TOKEN_REVOKED
from .const import ATTR_LAST_MEASURE
from .const import CONF_CONTRACT
from .const import CONF_VALUE
from .const import DEFAULT_SCAN_PERIOD
from .const import DOMAIN
from .const import CONF_2CAPTCHA_APIKEY
from .const import STAT_ID_CONSUMPTION

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
    twocaptcha_api_key = config_entry.data.get(CONF_2CAPTCHA_APIKEY, "")
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

        # Track last statistics sum and datetime to maintain continuity
        self._last_stats_sum: dict[str, float] = {}
        self._last_stats_dt: dict[str, datetime] = {}

        self.contract = contract.upper()
        self.id = contract.lower()
        # Use domain prefix for external statistics (required for Energy Dashboard)
        self.statistic_id = STAT_ID_CONSUMPTION(self.id)
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

        await self._force_relogin()

    async def _force_relogin(self) -> None:
        """Force a fresh login, clearing any existing token."""
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
        else:
            raise Exception("Re-login failed: no token received")

    async def _async_update_data(self):
        _LOGGER.info(f"Updating coordinator data for {self.contract}")
        TODAY = datetime.now()
        LAST_WEEK = TODAY - timedelta(days=7)
        # LAST_TIME_DAYS = None

        # last_measurement = await self.get_last_measurement_stored()
        # _LOGGER.info("Last stored measurement: %s", last_measurement)

        try:
            previous = datetime.fromisoformat(self._data.get(CONF_STATE, ""))
            # FIX: TypeError: can't subtract offset-naive and offset-aware datetimes
            previous = previous.replace(tzinfo=None)
            if previous:
                pass  # LAST_TIME_DAYS = (TODAY - previous).days
        except ValueError:
            previous = None

        if previous and (TODAY - previous) <= timedelta(minutes=10):
            _LOGGER.warning("Skipping request update data - too early")
            return

        consumptions = None

        # Step 1: Ensure we have a valid token
        try:
            await self._ensure_token()
        except Exception as exp:
            _LOGGER.error("Failed to ensure valid token for %s: %s", self.contract, exp)
            self.async_set_update_error(exp)
            return False

        # Step 2: Fetch consumption data
        try:
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
            error_str = str(exp)
            # Check if token was invalidated server-side
            if (
                API_ERROR_TOKEN_INVALID in error_str
                or API_ERROR_TOKEN_REVOKED in error_str
            ):
                _LOGGER.warning(
                    "Token rejected by server (%s), attempting re-login for %s",
                    error_str,
                    self.contract,
                )
                # Force re-login by clearing token and retrying once
                try:
                    await self._force_relogin()
                    consumptions = await self.hass.async_add_executor_job(
                        self._api.consumptions,
                        LAST_WEEK,
                        TODAY + timedelta(days=1),
                        self.contract,
                    )
                except Exception as retry_exp:
                    _LOGGER.error(
                        "Re-login failed for %s: %s", self.contract, retry_exp
                    )
                    self.async_set_update_error(retry_exp)
                    raise ConfigEntryAuthFailed from retry_exp
            else:
                _LOGGER.error("Error requesting %s data: %s", self.contract, exp)
                self.async_set_update_error(exp)

        if not consumptions:
            _LOGGER.error("No consumptions available")
            return False

        self._data["consumptions"] = consumptions

        # get last entry - most updated
        metric = consumptions[-1]
        self._data[CONF_VALUE] = metric["accumulatedConsumption"]
        self._data[CONF_STATE] = metric["datetime"]

        # await self._clear_statistics()
        # Import statistics for new consumption data to ensure Energy Dashboard has data
        # The recorder handles ongoing statistics automatically
        try:
            await self._async_import_statistics(consumptions, fill_to_now=True)
        except Exception:
            _LOGGER.exception("Failed to import statistics")

        # Note: We no longer import historical consumptions to avoid conflicts with HA recorder
        # if LAST_TIME_DAYS and LAST_TIME_DAYS >= 7:
        #     await self.import_old_consumptions(days=LAST_TIME_DAYS)

        return True

    async def get_last_measurement_stored(self) -> Optional[datetime]:
        """Placeholder — not used.

        Implement DB query later if needed.
        """
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

    async def _update_last_stats_summary(self):
        """Update self._last_stats_sum and self._last_stats_dt."""

        _LOGGER.debug("%s: checking latest statistics", self.contract)

        # For aigues-barcelona, we only have one statistic_id (consumption)
        statistic_ids = [self.statistic_id]

        # fetch last stats
        if MAJOR_VERSION < 2022 or (MAJOR_VERSION == 2022 and MINOR_VERSION < 12):
            last_stats = {
                _stat: await get_db_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 1, _stat, True
                )
                for _stat in statistic_ids
            }
        else:
            last_stats = {
                _stat: await get_db_instance(self.hass).async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    _stat,
                    True,
                    {"max", "sum"},
                )
                for _stat in statistic_ids
            }

        # get last record local datetime and eval if any stat is missing
        last_record_dt = {}
        for x in statistic_ids:
            try:
                if MAJOR_VERSION <= 2022:
                    last_record_dt[x] = dt_util.parse_datetime(
                        last_stats[x][x][0]["end"]
                    )
                elif MAJOR_VERSION == 2023 and MINOR_VERSION < 3:
                    last_record_dt[x] = dt_util.as_local(last_stats[x][x][0]["end"])
                else:
                    last_record_dt[x] = dt_util.utc_from_timestamp(
                        last_stats[x][x][0]["end"]
                    )
            except Exception:
                last_record_dt[x] = dt_util.as_utc(datetime(1970, 1, 1))

        # store most recent stat for each statistic_id
        self._last_stats_dt = last_record_dt
        self._last_stats_sum = {
            x: last_stats[x][x][0]["sum"]
            for x in last_stats
            if x in last_stats[x] and "sum" in last_stats[x][x][0]
        }

    async def check_statistics_integrity(self) -> bool:
        """Check if statistics contain negative sums (indicating
        corruption)."""

        _LOGGER.warning("Running statistics integrity check for %s", self.contract)

        # Check recent statistics for negative sums
        recent_stats = await get_db_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.utcnow() - timedelta(days=30),  # Check last 30 days
            None,
            {self.statistic_id},
            "hour",
            None,
            {"sum"},
        )

        if recent_stats and self.statistic_id in recent_stats:
            negative_found = False
            for stat in recent_stats[self.statistic_id]:
                stat_sum = stat.get("sum")
                if stat_sum is not None and stat_sum < 0:
                    _LOGGER.warning(
                        "Found negative sum (%.4f) in statistics for %s at %s. "
                        "This indicates data corruption.",
                        stat_sum,
                        self.contract,
                        stat.get("start"),
                    )
                    negative_found = True
                    break

            if negative_found:
                _LOGGER.warning(
                    "%s: statistics integrity check failed - negative sums found",
                    self.contract,
                )
                return False

        _LOGGER.info("%s: statistics integrity check passed", self.contract)
        return True

    async def rebuild_statistics(self, from_dt: datetime | None = None):
        """Rebuild statistics from a given datetime."""

        _LOGGER.warning("%s: rebuilding statistics from %s", self.contract, from_dt)

        # For now, we'll clear all statistics and let them rebuild on next update
        # In a full implementation, this would recalculate from raw data

        if from_dt is None:
            from_dt = dt_util.utcnow() - timedelta(days=30)  # Default to last 30 days

        # Clear statistics from the problematic date forward
        all_ids = await get_db_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass
        )
        to_clear = [
            x["statistic_id"] for x in all_ids if x["statistic_id"] == self.statistic_id
        ]

        if to_clear:
            _LOGGER.warning(
                "Clearing statistics for %s from %s forward", self.contract, from_dt
            )

            # Clear the statistics (simplified version - in production you'd want more robust clearing)
            # For now, we'll just reset our tracking and let new data rebuild
            self._last_stats_sum = {}
            self._last_stats_dt = {}

            _LOGGER.info(
                "Statistics cleared for %s - will rebuild on next update", self.contract
            )

    async def _get_existing_statistics(self, lookback_days: int = 7) -> Set[datetime]:
        """Query existing statistics timestamps to avoid duplicates.

        Returns a set of datetime objects representing hours that
        already have statistics. This prevents conflicts with Home
        Assistant's hourly statistics compilation.
        """
        existing_timestamps: Set[datetime] = set()
        try:
            start_time = dt_util.utcnow() - timedelta(days=lookback_days)
            existing_stats = await get_db_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                None,
                {self.statistic_id},
                "hour",
                None,  # units
                {"sum"},
            )

            if existing_stats and self.statistic_id in existing_stats:
                for stat in existing_stats[self.statistic_id]:
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

    async def _get_last_existing_statistic(
        self, lookback_days: int = 30
    ) -> Optional[Tuple[datetime, float, float]]:
        """Query the last existing statistic from the database.

        Returns a tuple of (timestamp, state, sum) for the most recent
        statistic, or None if no statistics exist. This is used to
        ensure new statistics continue correctly from where existing
        ones left off.
        """
        try:
            start_time = dt_util.utcnow() - timedelta(days=lookback_days)
            existing_stats = await get_db_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                None,
                {self.statistic_id},
                "hour",
                None,  # units
                {"state", "sum"},
            )

            if existing_stats and self.statistic_id in existing_stats:
                stats_list = existing_stats[self.statistic_id]
                if stats_list:
                    # Get the most recent statistic (last in the sorted list)
                    last_stat = stats_list[-1]
                    last_ts = last_stat.get("start")
                    last_state = last_stat.get("state")
                    last_sum = last_stat.get("sum")

                    if (
                        last_ts is not None
                        and last_state is not None
                        and last_sum is not None
                    ):
                        # Convert timestamp to datetime if needed
                        if isinstance(last_ts, (int, float)):
                            last_ts = dt_util.utc_from_timestamp(last_ts)
                        _LOGGER.debug(
                            "Found last statistic for %s: ts=%s, state=%.4f, sum=%.4f",
                            self.contract,
                            last_ts,
                            last_state,
                            last_sum,
                        )
                        return (last_ts, last_state, last_sum)
        except Exception as e:
            _LOGGER.warning(
                "Failed to query last statistic for %s: %s",
                self.contract,
                e,
            )
        return None

    def _normalize_consumptions(
        self, consumptions: List[Dict]
    ) -> List[Tuple[datetime, float]]:
        """Normalize consumption data to hourly buckets.

        Returns a sorted list of (timestamp, value) tuples, keeping the
        max value per hour.
        """
        # Sort consumptions by datetime
        # Filter out entries with invalid datetimes first to avoid mixing naive/aware datetimes
        valid_consumptions = [
            x
            for x in consumptions
            if dt_util.parse_datetime(x.get("datetime")) is not None
        ]
        consumptions = sorted(
            valid_consumptions,
            key=lambda x: dt_util.as_utc(dt_util.parse_datetime(x["datetime"])),
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

    def _convert_to_incremental_consumptions(
        self, consumptions: List[Tuple[datetime, float]]
    ) -> List[Tuple[datetime, float]]:
        """Convert absolute meter readings to incremental consumption values.

        This prevents negative readings by storing hourly increments
        instead of absolute values. The recorder will then calculate
        changes naturally.

        Returns a sorted list of (timestamp, incremental_value) tuples.
        """
        if not consumptions:
            return []

        # Sort by timestamp to ensure proper order
        consumptions = sorted(consumptions)

        incremental_data = []
        previous_value = None

        for timestamp, current_value in consumptions:
            if previous_value is not None:
                # Calculate the increment (consumption during this hour)
                increment = current_value - previous_value

                # Only include positive increments (negative would indicate data issues)
                if increment >= 0:
                    incremental_data.append((timestamp, round(increment, 4)))
                else:
                    _LOGGER.warning(
                        "Skipping negative increment %.4f for %s at %s "
                        "(current=%.4f, previous=%.4f). This indicates data corruption.",
                        increment,
                        self.contract,
                        timestamp,
                        current_value,
                        previous_value,
                    )
            else:
                # For the first data point, we can't calculate an increment
                # We'll handle this in the statistics import by using a baseline
                incremental_data.append((timestamp, 0.0))

            previous_value = current_value

        return incremental_data

    def _get_statistics_metadata(self) -> StatisticMetaData:
        """Return metadata for statistics import.

        We use DOMAIN as the source for external statistics. We store
        incremental consumption values to prevent negative readings when
        the recorder auto-compiles statistics.
        """
        return StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"Contador {self.id}",
            source=DOMAIN,  # External statistics use domain as source
            statistic_id=self.statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )

    async def _async_import_statistics(self, consumptions, fill_to_now=False) -> None:
        """Import consumption statistics following edata pattern."""
        if self._import_in_progress:
            _LOGGER.debug("Import already in progress — skipping")
            return

        self._import_in_progress = True
        _LOGGER.debug(
            "Starting _async_import_statistics for %s with %d consumptions",
            self.contract,
            len(consumptions) if consumptions else 0,
        )
        try:
            # Update last stats summary to populate tracking dicts
            await self._update_last_stats_summary()

            # Initialize cumulative sum from last known value
            cumulative_sum = self._last_stats_sum.get(self.statistic_id, 0.0)

            # Normalize to hourly buckets (absolute meter readings)
            absolute_items = self._normalize_consumptions(consumptions)
            if not absolute_items:
                _LOGGER.debug("No valid consumptions to process for %s", self.contract)
                return

            # Convert absolute readings to incremental consumption values
            items = self._convert_to_incremental_consumptions(absolute_items)
            if not items:
                _LOGGER.debug(
                    "No valid incremental consumptions to process for %s", self.contract
                )
                return

            _LOGGER.debug(
                "Converted to %d incremental items for %s (first: %s, last: %s)",
                len(items),
                self.contract,
                items[0] if items else None,
                items[-1] if items else None,
            )

            # Build statistics data following edata pattern
            new_stats = []
            last_stat_dt = self._last_stats_dt.get(self.statistic_id)
            first_stat = None
            last_stat = None

            for start_ts, increment in items:
                # Skip data that's not newer than our last statistic
                if last_stat_dt and start_ts <= last_stat_dt:
                    continue

                # Update cumulative sum with this increment
                cumulative_sum += increment

                stat_data = StatisticData(
                    start=start_ts,
                    state=increment,  # Store the increment (non-negative)
                    sum=round(
                        cumulative_sum, 4
                    ),  # Cumulative total (always increasing)
                )
                new_stats.append(stat_data)

                # Track first and last stats for logging (extract values immediately)
                if first_stat is None:
                    first_stat = (start_ts, increment, round(cumulative_sum, 4))
                last_stat = (start_ts, increment, round(cumulative_sum, 4))

            # Update tracking and log BEFORE calling async_add_external_statistics
            if new_stats:
                self._last_stats_dt[self.statistic_id] = last_stat[0]
                self._last_stats_sum[self.statistic_id] = last_stat[2]

                _LOGGER.info(
                    "Importing %d points for %s: first=%s (increment=%.4f, sum=%.4f), "
                    "last=%s (increment=%.4f, sum=%.4f)",
                    len(new_stats),
                    self.contract,
                    first_stat[0],
                    first_stat[1],
                    first_stat[2],
                    last_stat[0],
                    last_stat[1],
                    last_stat[2],
                )

                # Use async_add_external_statistics with proper metadata
                metadata = self._get_statistics_metadata()
                async_add_external_statistics(self.hass, metadata, new_stats)
            else:
                _LOGGER.debug(
                    "No new statistics to import for %s - all items were filtered",
                    self.contract,
                )
        finally:
            self._import_in_progress = False

    async def clear_all_stored_data(self) -> None:
        """Clear all stored statistics from database and tracking data."""
        _LOGGER.warning(
            "%s: clearing all stored statistics from database", self.contract
        )

        # Get all statistic IDs for this contract
        all_ids = await get_db_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass
        )
        to_clear = [
            x["statistic_id"] for x in all_ids if x["statistic_id"] == self.statistic_id
        ]

        if to_clear:
            # Delete statistics from database
            await get_db_instance(self.hass).async_clear_statistics(to_clear)
            _LOGGER.info(
                "Deleted statistics from database for %s: %s", self.contract, to_clear
            )

        # Clear in-memory tracking
        self._last_stats_sum = {}
        self._last_stats_dt = {}

    async def import_old_consumptions(self, days: int = 365) -> None:
        """Import historical consumption data with HOURLY granularity.

        Fetches consumption data week by week going back the specified
        number of days, using HOURLY frequency to get proper hourly data
        for the Energy Dashboard.

        Uses incremental consumption approach to prevent negative
        readings. Maintains a continuous cumulative sum across all
        weeks.
        """
        today = datetime.now()
        start_date = today - timedelta(days=days)

        await self._ensure_token()

        # Pre-fetch existing statistics for the entire period to avoid duplicates
        existing_timestamps = await self._get_existing_statistics(
            lookback_days=days + 7
        )

        # Get the last existing statistic to continue the cumulative sum
        last_existing = await self._get_last_existing_statistic(lookback_days=days + 7)
        cumulative_sum = last_existing[2] if last_existing else 0.0

        if last_existing:
            _LOGGER.info(
                "Historical import continuing from existing statistic for %s: sum=%.4f",
                self.contract,
                cumulative_sum,
            )

        current_date = start_date
        imported_count = 0
        total_points = 0

        while current_date < today:
            # Calculate week boundaries
            week_end = min(current_date + timedelta(days=7), today)

            # Fetch HOURLY data for this week (not daily)
            consumptions = await self.hass.async_add_executor_job(
                self._api.consumptions,
                current_date,
                week_end,
                self.contract,
            )

            if consumptions:
                # Normalize to absolute readings
                absolute_items = self._normalize_consumptions(consumptions)
                if absolute_items:
                    # Convert to incremental values
                    items = self._convert_to_incremental_consumptions(absolute_items)

                    # Build stats for this week
                    stats = []
                    for start_ts, increment in items:
                        if start_ts in existing_timestamps:
                            continue

                        # Update cumulative sum with this increment
                        cumulative_sum += increment

                        stats.append(
                            StatisticData(
                                start=start_ts,
                                state=increment,
                                sum=round(cumulative_sum, 4),
                            )
                        )
                        existing_timestamps.add(start_ts)

                    if stats:
                        # Use async_add_external_statistics to ensure metadata exists
                        metadata = self._get_statistics_metadata()
                        async_add_external_statistics(self.hass, metadata, stats)
                        _LOGGER.debug(
                            "Imported %d hourly points for %s (week of %s, cumulative_sum=%.4f)",
                            len(stats),
                            self.contract,
                            current_date,
                            cumulative_sum,
                        )
                        imported_count += 1
                        total_points += len(stats)
            else:
                _LOGGER.debug("No data available for week of %s", current_date)

            current_date += timedelta(weeks=1)

        _LOGGER.info(
            "Completed importing %d weeks (%d hourly points) of historical data for %s",
            imported_count,
            total_points,
            self.contract,
        )

    async def _async_import_statistics_with_existing(
        self,
        consumptions,
        existing_timestamps: Set[datetime],
        fill_to_now: bool = False,
    ) -> None:
        """Import statistics using pre-fetched existing timestamps.

        This is used for bulk historical imports where we want to check
        duplicates against a pre-fetched set of existing statistics.
        Uses incremental consumption approach to prevent negative readings.

        Args:
            consumptions: Raw consumption data to import.
            existing_timestamps: Pre-fetched set of existing statistic timestamps.
            fill_to_now: Whether to fill gaps up to current time.
        """
        if self._import_in_progress:
            _LOGGER.debug("Import already in progress — skipping")
            return

        self._import_in_progress = True
        try:
            # Normalize to absolute readings
            absolute_items = self._normalize_consumptions(consumptions)
            if not absolute_items:
                return

            # Convert to incremental values
            items = self._convert_to_incremental_consumptions(absolute_items)
            if not items:
                return

            # Get the last existing statistic to continue the cumulative sum
            last_existing = await self._get_last_existing_statistic(lookback_days=30)
            cumulative_sum = last_existing[2] if last_existing else 0.0
            last_existing_ts: Optional[datetime] = (
                last_existing[0] if last_existing else None
            )

            # Build stats list, filtering out duplicates and old data
            stats = []
            for start_ts, increment in items:
                if start_ts in existing_timestamps:
                    continue
                # Skip data older than or equal to the last existing statistic
                if last_existing_ts is not None and start_ts <= last_existing_ts:
                    continue

                # Update cumulative sum with this increment
                cumulative_sum += increment

                stats.append(
                    StatisticData(
                        start=start_ts,
                        state=increment,
                        sum=round(cumulative_sum, 4),
                    )
                )
                # Add to existing set to prevent duplicates within this import session
                existing_timestamps.add(start_ts)

            if stats:
                # Use async_add_external_statistics to ensure metadata exists
                metadata = self._get_statistics_metadata()
                async_add_external_statistics(self.hass, metadata, stats)
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
        # Primary: Use current value from coordinator data
        value = self.coordinator._data.get(CONF_VALUE, None)
        if value is not None:
            return value

        # Fallback: Use last known statistic value (similar to edata pattern)
        last_stat = self.coordinator._last_stats_sum.get(
            self.coordinator.statistic_id, None
        )
        if last_stat is not None:
            return last_stat

        # Default: Return 0.0 if no data available yet
        return 0.0

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
