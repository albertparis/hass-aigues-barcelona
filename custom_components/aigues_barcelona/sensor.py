"""Platform for sensor integration."""

# from __future__ import annotations
import logging
from datetime import datetime
from datetime import timedelta

import homeassistant.components.recorder.util as recorder_util

from homeassistant.components.recorder.statistics import async_import_statistics
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
from .const import API_ERROR_TOKEN_INVALID
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

        # Baseline state from the first reading ever - used to ensure
        # consistent sum calculations across historical and regular imports
        self._baseline_state: Optional[float] = None

        self.contract = contract.upper()
        self.id = contract.lower()
        # Use sensor entity ID format for statistics (required for Energy Dashboard)
        self.statistic_id = f"sensor.contador_{self.id}"
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

    async def _clear_statistics(self) -> None:
        recorder = get_db_instance(self.hass)
        all_ids = await recorder.async_add_executor_job(list_statistic_ids, self.hass)
        to_clear = [
            x["statistic_id"] for x in all_ids if x["statistic_id"] == self.statistic_id
        ]

        if to_clear:
            _LOGGER.warning(
                f"About to delete {len(to_clear)} statistics entries for {self.contract}"
            )

            # Use recorder's session directly to delete statistics
            # This avoids the get_session() issue with clear_statistics
            def _delete_stats(recorder_instance, statistic_ids):
                try:
                    from homeassistant.components.recorder.db_schema import (
                        Statistics,
                        StatisticsShortTerm,
                        StatisticsMeta,
                    )
                except ImportError:
                    # Fallback for different HA versions
                    try:
                        from homeassistant.components.recorder.models.db_schema import (
                            Statistics,
                            StatisticsShortTerm,
                            StatisticsMeta,
                        )
                    except ImportError:
                        _LOGGER.error(
                            "Could not import database schema models for statistics deletion"
                        )
                        return

                from sqlalchemy import delete

                with recorder_instance.get_session() as session:
                    # Get metadata IDs for the statistic IDs
                    meta_ids = (
                        session.query(StatisticsMeta.id)
                        .filter(StatisticsMeta.statistic_id.in_(statistic_ids))
                        .all()
                    )
                    meta_ids = [row[0] for row in meta_ids]

                    if meta_ids:
                        # Delete from statistics table (long-term)
                        session.execute(
                            delete(Statistics).where(
                                Statistics.metadata_id.in_(meta_ids)
                            )
                        )
                        # Delete from statistics_short_term table (5-minute data)
                        session.execute(
                            delete(StatisticsShortTerm).where(
                                StatisticsShortTerm.metadata_id.in_(meta_ids)
                            )
                        )
                        # Note: We do NOT delete StatisticsMeta - it must remain
                        # so that new statistics can reference the same metadata_id.
                        # The metadata will be reused when importing new statistics.
                        session.commit()

            await recorder.async_add_executor_job(_delete_stats, recorder, to_clear)
            _LOGGER.info(f"Cleared statistics for {self.contract}")

    async def _clear_statistics_from_timestamp(self, from_timestamp: datetime) -> None:
        """Clear statistics from a specific timestamp forward.

        This is used to fix statistics that were incorrectly compiled by
        HA's recorder with wrong sum values.
        """
        recorder = get_db_instance(self.hass)
        all_ids = await recorder.async_add_executor_job(list_statistic_ids, self.hass)
        to_clear = [
            x["statistic_id"] for x in all_ids if x["statistic_id"] == self.statistic_id
        ]

        if not to_clear:
            return

        # Convert timestamp to Unix timestamp for database comparison
        from_ts = from_timestamp.timestamp()

        def _delete_stats_from_ts(recorder_instance, statistic_ids, cutoff_ts):
            try:
                from homeassistant.components.recorder.db_schema import (
                    Statistics,
                    StatisticsShortTerm,
                )
            except ImportError:
                try:
                    from homeassistant.components.recorder.models.db_schema import (
                        Statistics,
                        StatisticsShortTerm,
                    )
                except ImportError:
                    _LOGGER.error(
                        "Could not import database schema models for statistics deletion"
                    )
                    return

            from sqlalchemy import delete

            with recorder_instance.get_session() as session:
                # Get metadata IDs for the statistic IDs
                from homeassistant.components.recorder.db_schema import StatisticsMeta

                try:
                    meta_ids = (
                        session.query(StatisticsMeta.id)
                        .filter(StatisticsMeta.statistic_id.in_(statistic_ids))
                        .all()
                    )
                except Exception:
                    try:
                        from homeassistant.components.recorder.models.db_schema import (
                            StatisticsMeta,
                        )

                        meta_ids = (
                            session.query(StatisticsMeta.id)
                            .filter(StatisticsMeta.statistic_id.in_(statistic_ids))
                            .all()
                        )
                    except Exception as e:
                        _LOGGER.error("Failed to query metadata IDs: %s", e)
                        return

                meta_ids = [row[0] for row in meta_ids]

                if not meta_ids:
                    return

                # Delete statistics from cutoff timestamp forward
                deleted_long = (
                    session.execute(
                        delete(Statistics).where(
                            Statistics.metadata_id.in_(meta_ids),
                            Statistics.start_ts >= cutoff_ts,
                        )
                    )
                ).rowcount

                deleted_short = (
                    session.execute(
                        delete(StatisticsShortTerm).where(
                            StatisticsShortTerm.metadata_id.in_(meta_ids),
                            StatisticsShortTerm.start_ts >= cutoff_ts,
                        )
                    )
                ).rowcount

                session.commit()
                return deleted_long + deleted_short

        deleted_count = await recorder.async_add_executor_job(
            _delete_stats_from_ts, recorder, to_clear, from_ts
        )
        _LOGGER.info(
            "Cleared %d statistics entries for %s from %s forward",
            deleted_count or 0,
            self.contract,
            from_timestamp,
        )

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

    async def _get_baseline_from_statistics(
        self, lookback_days: int = 400
    ) -> Optional[float]:
        """Query the baseline (first state value) from existing statistics.

        Returns the state value from the oldest statistic in the
        database, which represents the baseline used for sum
        calculations. This ensures regular updates use the same baseline
        as historical imports.
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
                {"state"},
            )

            if existing_stats and self.statistic_id in existing_stats:
                stats_list = existing_stats[self.statistic_id]
                if stats_list:
                    # Find the oldest statistic by timestamp
                    # (don't assume the list is sorted)
                    oldest_stat = min(stats_list, key=lambda s: s.get("start", 0))
                    oldest_state = oldest_stat.get("state")

                    # Validate the baseline looks reasonable
                    if oldest_state is not None and oldest_state >= 0:
                        # Check if the oldest stat is not too old (more than 2 years)
                        oldest_ts = oldest_stat.get("start")
                        if oldest_ts and isinstance(oldest_ts, (int, float)):
                            oldest_datetime = dt_util.utc_from_timestamp(oldest_ts)
                            days_old = (dt_util.utcnow() - oldest_datetime).days
                            if days_old > 730:  # 2 years
                                _LOGGER.warning(
                                    "Baseline from %s is too old (%d days). "
                                    "Will use current data as baseline.",
                                    oldest_datetime,
                                    days_old,
                                )
                                return None

                        _LOGGER.info(
                            "Baseline query for %s: found %d stats, oldest at %s "
                            "with state=%.4f",
                            self.contract,
                            len(stats_list),
                            oldest_stat.get("start"),
                            oldest_state,
                        )
                        return oldest_state
                    else:
                        _LOGGER.warning(
                            "Invalid baseline state %.4f for %s. Will recalculate.",
                            oldest_state,
                            self.contract,
                        )
                        return None

                    if oldest_state is not None:
                        return oldest_state
            else:
                _LOGGER.warning(
                    "No statistics found for %s when querying baseline "
                    "(lookback=%d days)",
                    self.contract,
                    lookback_days,
                )
        except Exception as e:
            _LOGGER.warning(
                "Failed to query baseline for %s: %s",
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
        """Return metadata for statistics import.

        Using DOMAIN as source instead of "recorder" prevents Home
        Assistant's recorder from auto-compiling statistics with its own
        baseline, which can cause negative values in the Energy
        Dashboard. The integration manages statistics with a consistent
        baseline (first reading ever).
        """
        metadata = {
            "has_sum": True,
            "name": f"Contador {self.id}",
            "source": DOMAIN,  # Use domain instead of "recorder" to prevent recorder auto-compilation
            "statistic_id": self.statistic_id,
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
        _LOGGER.debug(
            "Starting _async_import_statistics for %s with %d consumptions",
            self.contract,
            len(consumptions) if consumptions else 0,
        )
        try:
            # Query existing statistics to avoid duplicates
            existing_timestamps = await self._get_existing_statistics(lookback_days=7)
            _LOGGER.debug(
                "Found %d existing timestamps for %s",
                len(existing_timestamps),
                self.contract,
            )

            # Normalize to hourly buckets
            items = self._normalize_consumptions(consumptions)
            if not items:
                _LOGGER.debug("No valid consumptions to process for %s", self.contract)
                return

            _LOGGER.debug(
                "Normalized to %d hourly items for %s (first: %s, last: %s)",
                len(items),
                self.contract,
                items[0] if items else None,
                items[-1] if items else None,
            )

            # Get the baseline state (first reading ever) for consistent sum calculation
            # This ensures regular updates use the same baseline as historical imports
            baseline_state = self._baseline_state
            _LOGGER.debug(
                "Current _baseline_state for %s: %s",
                self.contract,
                baseline_state,
            )
            if baseline_state is None:
                baseline_state = await self._get_baseline_from_statistics(
                    lookback_days=400
                )
                if baseline_state is not None:
                    self._baseline_state = baseline_state
                    _LOGGER.info(
                        "Loaded baseline from statistics for %s: %.4f",
                        self.contract,
                        baseline_state,
                    )
                else:
                    _LOGGER.warning(
                        "Could not load baseline from statistics for %s",
                        self.contract,
                    )

            # If no baseline exists (first time setup), use first reading
            if baseline_state is None:
                first_state = items[0][1]
                # Validate the first reading looks reasonable
                if first_state >= 0:
                    baseline_state = first_state
                    self._baseline_state = baseline_state
                    _LOGGER.warning(
                        "No baseline found, using first reading for %s: %.4f",
                        self.contract,
                        baseline_state,
                    )
                else:
                    _LOGGER.error(
                        "Invalid first reading %.4f for %s. Skipping statistics import.",
                        first_state,
                        self.contract,
                    )
                    return

            # Query the last existing statistic to know which timestamps to skip
            last_existing = await self._get_last_existing_statistic(lookback_days=30)
            last_existing_ts: Optional[datetime] = None
            if last_existing:
                last_existing_ts, last_existing_state, last_existing_sum = last_existing
                # Check if the last existing statistic has a correct sum value
                # HA's recorder might have compiled it with a wrong baseline
                expected_sum = last_existing_state - baseline_state
                sum_diff = abs(last_existing_sum - expected_sum)

                # If the sum is way off (more than 1 m³ difference), it's likely from HA recorder
                # with wrong baseline. We should delete it and reimport.
                if sum_diff > 1.0:
                    _LOGGER.warning(
                        "Last existing statistic for %s has incorrect sum: "
                        "expected=%.4f, actual=%.4f (diff=%.4f). "
                        "This is likely from HA recorder with wrong baseline. "
                        "Deleting statistics from %s forward to fix.",
                        self.contract,
                        expected_sum,
                        last_existing_sum,
                        sum_diff,
                        last_existing_ts,
                    )
                    # Delete statistics from this timestamp forward
                    await self._clear_statistics_from_timestamp(last_existing_ts)
                    # Re-query existing timestamps since we just deleted some
                    existing_timestamps = await self._get_existing_statistics(
                        lookback_days=7
                    )
                    # Clear the last_existing_ts so we reimport this data
                    last_existing_ts = None
                else:
                    _LOGGER.debug(
                        "Last existing statistic for %s: ts=%s, sum=%.4f (expected=%.4f)",
                        self.contract,
                        last_existing_ts,
                        last_existing_sum,
                        expected_sum,
                    )
            else:
                _LOGGER.debug(
                    "No existing statistics found for %s in last 30 days",
                    self.contract,
                )

            # Track the most recent data point for fill_to_now (before filtering)
            most_recent_ts, most_recent_state = items[-1]

            # Build stats list, filtering out duplicates and data older than last statistic
            stats = []
            skipped_existing = 0
            skipped_old = 0
            for start_ts, state in items:
                # Skip if this timestamp already has a statistic
                if start_ts in existing_timestamps:
                    skipped_existing += 1
                    continue
                # Skip if this data is older than or equal to the last existing statistic
                # This prevents re-importing old data
                if last_existing_ts is not None and start_ts <= last_existing_ts:
                    skipped_old += 1
                    continue
                # Calculate sum using the baseline (consistent with historical import)
                # sum = current_state - baseline_state
                new_sum = state - baseline_state

                # Prevent negative consumption sums (water usage can't be negative)
                if new_sum < 0:
                    _LOGGER.warning(
                        "Calculated negative sum %.4f for %s at %s "
                        "(state=%.4f, baseline=%.4f). Skipping this data point.",
                        new_sum,
                        self.contract,
                        start_ts,
                        state,
                        baseline_state,
                    )
                    continue

                stats.append(
                    {
                        "start": start_ts,
                        "state": state,
                        "sum": new_sum,
                    }
                )

            _LOGGER.debug(
                "Stats build for %s: %d to import, %d skipped (existing), "
                "%d skipped (old), baseline=%.4f",
                self.contract,
                len(stats),
                skipped_existing,
                skipped_old,
                baseline_state,
            )

            # Fill gaps up to previous hour to avoid conflicts with HA recorder
            if fill_to_now:
                now_utc = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
                # Fill up to previous hour to avoid conflicts with HA's automatic hourly compilation
                max_fill_ts = now_utc - timedelta(hours=1)
                fill_ts = most_recent_ts + timedelta(hours=1)

                while fill_ts <= max_fill_ts:
                    if fill_ts not in existing_timestamps:
                        # Calculate sum for fill values using baseline
                        fill_sum = most_recent_state - baseline_state

                        # Prevent negative consumption sums
                        if fill_sum < 0:
                            _LOGGER.warning(
                                "Calculated negative fill sum %.4f for %s at %s. "
                                "Skipping fill for this hour.",
                                fill_sum,
                                self.contract,
                                fill_ts,
                            )
                            continue

                        stats.append(
                            {
                                "start": fill_ts,
                                "state": most_recent_state,
                                "sum": fill_sum,
                            }
                        )
                        existing_timestamps.add(fill_ts)  # Prevent duplicates
                    fill_ts += timedelta(hours=1)

            if stats:
                # Log details about what we're importing
                if len(stats) > 0:
                    _LOGGER.info(
                        "Importing %d points for %s: first=%s (state=%.4f, sum=%.4f), "
                        "last=%s (state=%.4f, sum=%.4f)",
                        len(stats),
                        self.contract,
                        stats[0]["start"],
                        stats[0]["state"],
                        stats[0]["sum"],
                        stats[-1]["start"],
                        stats[-1]["state"],
                        stats[-1]["sum"],
                    )
                async_import_statistics(
                    self.hass, self._get_statistics_metadata(), stats
                )
            else:
                _LOGGER.warning(
                    "No new statistics to import for %s - all %d items were filtered",
                    self.contract,
                    len(items) if items else 0,
                )
        finally:
            self._import_in_progress = False

    async def clear_all_stored_data(self) -> None:
        await self._clear_statistics()

    async def import_old_consumptions(self, days: int = 365) -> None:
        """Import historical consumption data with HOURLY granularity.

        Fetches consumption data week by week going back the specified
        number of days, using HOURLY frequency to get proper hourly data
        for the Energy Dashboard.

        For historical imports, we use the first reading as the baseline
        and build cumulative sums from there, ensuring continuity across
        all weeks.
        """
        today = datetime.now()
        start_date = today - timedelta(days=days)

        await self._ensure_token()

        # Pre-fetch existing statistics for the entire period to avoid duplicates
        existing_timestamps = await self._get_existing_statistics(
            lookback_days=days + 7
        )

        # For historical imports, we need to establish a baseline from the
        # first data point and maintain it across all weeks
        baseline_state: Optional[float] = None

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
                items = self._normalize_consumptions(consumptions)
                if items:
                    # Initialize baseline from the very first data point
                    if baseline_state is None:
                        baseline_state = items[0][1]
                        # Store the baseline so regular updates use the same value
                        self._baseline_state = baseline_state
                        _LOGGER.info(
                            "Historical import baseline for %s: %.4f (from %s)",
                            self.contract,
                            baseline_state,
                            items[0][0],
                        )

                    # Build stats for this week
                    stats = []
                    for start_ts, state in items:
                        if start_ts in existing_timestamps:
                            continue
                        # Calculate sum from baseline (first reading ever)
                        new_sum = state - baseline_state
                        stats.append(
                            {
                                "start": start_ts,
                                "state": state,
                                "sum": new_sum,
                            }
                        )
                        existing_timestamps.add(start_ts)

                    if stats:
                        async_import_statistics(
                            self.hass, self._get_statistics_metadata(), stats
                        )
                        _LOGGER.debug(
                            "Imported %d hourly points for %s (week of %s)",
                            len(stats),
                            self.contract,
                            current_date,
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
        baseline_state: Optional[float] = None,
    ) -> None:
        """Import statistics using pre-fetched existing timestamps.

        This is used for bulk historical imports where we want to check
        duplicates against a pre-fetched set of existing statistics.

        Args:
            consumptions: Raw consumption data to import.
            existing_timestamps: Pre-fetched set of existing statistic timestamps.
            fill_to_now: Whether to fill gaps up to current time.
            baseline_state: The baseline state value (first reading ever) used
                           for sum calculations. If not provided, will use
                           the first reading from the data.
        """
        if self._import_in_progress:
            _LOGGER.debug("Import already in progress — skipping")
            return

        self._import_in_progress = True
        try:
            items = self._normalize_consumptions(consumptions)
            if not items:
                return

            # Use provided baseline or fall back to first reading
            if baseline_state is None:
                baseline_state = items[0][1]

            # Query the last existing statistic to know which timestamps to skip
            last_existing = await self._get_last_existing_statistic(lookback_days=30)
            last_existing_ts: Optional[datetime] = None
            if last_existing:
                last_existing_ts = last_existing[0]

            # Build stats list, filtering out duplicates and old data
            stats = []
            for start_ts, state in items:
                if start_ts in existing_timestamps:
                    continue
                # Skip data older than or equal to the last existing statistic
                if last_existing_ts is not None and start_ts <= last_existing_ts:
                    continue
                # Calculate sum using the baseline (consistent approach)
                new_sum = state - baseline_state
                stats.append(
                    {
                        "start": start_ts,
                        "state": state,
                        "sum": new_sum,
                    }
                )
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
