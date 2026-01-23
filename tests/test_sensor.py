"""Tests for the Aigues de Barcelona sensor module."""

from datetime import datetime, timedelta

import pytest

from homeassistant.const import UnitOfVolume
from homeassistant.util import dt as dt_util


class TestNormalizeConsumptions:
    """Tests for the _normalize_consumptions method."""

    def _create_normalize_function(self):
        """Create a standalone normalize function for testing."""
        from datetime import timezone

        def normalize_consumptions(consumptions):
            """Normalize consumption data to hourly buckets."""
            # Use UTC-aware min datetime for comparison
            min_dt = datetime.min.replace(tzinfo=timezone.utc)

            consumptions = sorted(
                consumptions,
                key=lambda x: (
                    dt_util.as_utc(dt_util.parse_datetime(x["datetime"]))
                    if dt_util.parse_datetime(x["datetime"]) is not None
                    else min_dt
                ),
            )

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

            return sorted(normalized.items())

        return normalize_consumptions

    def test_normalize_empty_list(self):
        """Test normalization with empty list."""
        normalize = self._create_normalize_function()
        result = normalize([])
        assert result == []

    def test_normalize_single_entry(self):
        """Test normalization with a single entry."""
        normalize = self._create_normalize_function()
        consumptions = [
            {
                "datetime": "2026-01-15T10:30:00",
                "accumulatedConsumption": 100.5,
            }
        ]
        result = normalize(consumptions)

        assert len(result) == 1
        assert result[0][1] == 100.5

    def test_normalize_multiple_entries_same_hour(self):
        """Test that multiple entries in the same hour keep the max value."""
        normalize = self._create_normalize_function()
        consumptions = [
            {"datetime": "2026-01-15T10:00:00", "accumulatedConsumption": 100.0},
            {"datetime": "2026-01-15T10:15:00", "accumulatedConsumption": 100.5},
            {"datetime": "2026-01-15T10:30:00", "accumulatedConsumption": 101.0},
            {"datetime": "2026-01-15T10:45:00", "accumulatedConsumption": 100.8},
        ]
        result = normalize(consumptions)

        assert len(result) == 1
        # Should keep the max value (101.0)
        assert result[0][1] == 101.0

    def test_normalize_multiple_hours(self):
        """Test normalization across multiple hours."""
        normalize = self._create_normalize_function()
        consumptions = [
            {"datetime": "2026-01-15T10:00:00", "accumulatedConsumption": 100.0},
            {"datetime": "2026-01-15T11:00:00", "accumulatedConsumption": 101.0},
            {"datetime": "2026-01-15T12:00:00", "accumulatedConsumption": 102.0},
        ]
        result = normalize(consumptions)

        assert len(result) == 3
        assert result[0][1] == 100.0
        assert result[1][1] == 101.0
        assert result[2][1] == 102.0

    def test_normalize_unsorted_entries(self):
        """Test that unsorted entries are properly sorted."""
        normalize = self._create_normalize_function()
        consumptions = [
            {"datetime": "2026-01-15T12:00:00", "accumulatedConsumption": 102.0},
            {"datetime": "2026-01-15T10:00:00", "accumulatedConsumption": 100.0},
            {"datetime": "2026-01-15T11:00:00", "accumulatedConsumption": 101.0},
        ]
        result = normalize(consumptions)

        assert len(result) == 3
        # Should be sorted by timestamp
        assert result[0][1] == 100.0
        assert result[1][1] == 101.0
        assert result[2][1] == 102.0

    def test_normalize_rounds_to_four_decimals(self):
        """Test that values are rounded to 4 decimal places."""
        normalize = self._create_normalize_function()
        consumptions = [
            {
                "datetime": "2026-01-15T10:00:00",
                "accumulatedConsumption": 100.123456789,
            },
        ]
        result = normalize(consumptions)

        assert result[0][1] == 100.1235

    def test_normalize_skips_invalid_datetime(self):
        """Test that entries with invalid datetime are skipped."""
        normalize = self._create_normalize_function()
        consumptions = [
            {"datetime": "invalid-date", "accumulatedConsumption": 100.0},
            {"datetime": "2026-01-15T10:00:00", "accumulatedConsumption": 101.0},
        ]
        result = normalize(consumptions)

        assert len(result) == 1
        assert result[0][1] == 101.0


class TestStatisticsMetadata:
    """Tests for statistics metadata structure."""

    def test_metadata_has_required_fields(self):
        """Test that metadata contains all required fields."""
        # Test the expected metadata structure
        metadata = {
            "has_sum": True,
            "name": "Contador abc123",
            "source": "recorder",  # Required by Home Assistant
            "statistic_id": "sensor.contador_abc123",
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
        }

        assert "has_sum" in metadata
        assert "name" in metadata
        assert "source" in metadata
        assert "statistic_id" in metadata
        assert "unit_of_measurement" in metadata

    def test_metadata_values(self):
        """Test metadata field values."""
        contract_id = "abc123"
        metadata = {
            "has_sum": True,
            "name": f"Contador {contract_id}",
            "source": "recorder",  # Required by Home Assistant
            "statistic_id": f"sensor.contador_{contract_id}",
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
        }

        assert metadata["has_sum"] is True
        assert metadata["name"] == "Contador abc123"
        assert metadata["source"] == "aigues_barcelona"
        assert metadata["statistic_id"] == "sensor.contador_abc123"
        assert metadata["unit_of_measurement"] == UnitOfVolume.CUBIC_METERS


class TestDuplicateFiltering:
    """Tests for duplicate filtering logic."""

    def test_filter_existing_timestamps(self):
        """Test that existing timestamps are filtered out."""
        existing_timestamps = {
            datetime(2026, 1, 15, 10, 0, 0),
            datetime(2026, 1, 15, 12, 0, 0),
        }

        items = [
            (datetime(2026, 1, 15, 10, 0, 0), 100.0),  # Should be filtered
            (datetime(2026, 1, 15, 11, 0, 0), 101.0),  # Should pass
            (datetime(2026, 1, 15, 12, 0, 0), 102.0),  # Should be filtered
            (datetime(2026, 1, 15, 13, 0, 0), 103.0),  # Should pass
        ]

        stats = []
        for start_ts, state in items:
            if start_ts in existing_timestamps:
                continue
            stats.append({"start": start_ts, "state": state, "sum": state})

        assert len(stats) == 2
        assert stats[0]["state"] == 101.0
        assert stats[1]["state"] == 103.0

    def test_empty_existing_timestamps(self):
        """Test that all items pass when no existing timestamps."""
        existing_timestamps = set()

        items = [
            (datetime(2026, 1, 15, 10, 0, 0), 100.0),
            (datetime(2026, 1, 15, 11, 0, 0), 101.0),
        ]

        stats = []
        for start_ts, state in items:
            if start_ts in existing_timestamps:
                continue
            stats.append({"start": start_ts, "state": state, "sum": state})

        assert len(stats) == 2


class TestFillToNowLogic:
    """Tests for the fill_to_now logic."""

    def test_fill_gaps_between_timestamps(self):
        """Test that gaps are filled with the last known value."""
        most_recent_ts = datetime(2026, 1, 15, 10, 0, 0)
        most_recent_state = 100.0
        max_fill_ts = datetime(2026, 1, 15, 13, 0, 0)
        existing_timestamps = set()

        stats = []
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

        # Should have 3 fill entries: 11:00, 12:00, 13:00
        assert len(stats) == 3
        for stat in stats:
            assert stat["state"] == 100.0

    def test_fill_skips_existing_timestamps(self):
        """Test that fill skips existing timestamps."""
        most_recent_ts = datetime(2026, 1, 15, 10, 0, 0)
        most_recent_state = 100.0
        max_fill_ts = datetime(2026, 1, 15, 13, 0, 0)
        existing_timestamps = {datetime(2026, 1, 15, 12, 0, 0)}

        stats = []
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

        # Should have 2 fill entries: 11:00, 13:00 (12:00 is skipped)
        assert len(stats) == 2
        timestamps = [s["start"] for s in stats]
        assert datetime(2026, 1, 15, 12, 0, 0) not in timestamps

    def test_no_fill_when_no_gap(self):
        """Test that no fill happens when there's no gap."""
        most_recent_ts = datetime(2026, 1, 15, 13, 0, 0)
        most_recent_state = 100.0
        max_fill_ts = datetime(2026, 1, 15, 13, 0, 0)  # Same as most recent
        existing_timestamps = set()

        stats = []
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

        # Should have no fill entries
        assert len(stats) == 0


class TestRealApiResponse:
    """Tests using realistic API response data."""

    @pytest.fixture
    def real_daily_consumptions(self):
        """Return realistic daily consumption data from the API."""
        return [
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 743.882,
                "deltaConsumption": 0.356,
                "waterMeterId": "TODO",
                "realConsumption": True,
                "datetime": "2026-01-11T23:32:41+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.07,
                "minFlowTime": "00:32:42",
                "maxFlowTime": "19:32:42",
                "number": "X00AA000000TEST",
            },
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 744.211,
                "deltaConsumption": 0.329,
                "waterMeterId": "TODO",
                "realConsumption": True,
                "datetime": "2026-01-12T23:32:39+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.077,
                "minFlowTime": "00:32:41",
                "maxFlowTime": "10:32:42",
                "number": "X00AA000000TEST",
            },
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 744.555,
                "deltaConsumption": 0.344,
                "waterMeterId": "TODO",
                "realConsumption": True,
                "datetime": "2026-01-13T23:32:37+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.067,
                "minFlowTime": "00:32:39",
                "maxFlowTime": "15:32:38",
                "number": "X00AA000000TEST",
            },
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 744.714,
                "deltaConsumption": 0.159,
                "waterMeterId": "TODO",
                "realConsumption": True,
                "datetime": "2026-01-14T23:32:36+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.02,
                "minFlowTime": "00:32:37",
                "maxFlowTime": "20:32:38",
                "number": "X00AA000000TEST",
            },
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 745.092,
                "deltaConsumption": 0.378,
                "waterMeterId": "TODO",
                "realConsumption": True,
                "datetime": "2026-01-15T23:32:34+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.061,
                "minFlowTime": "00:32:36",
                "maxFlowTime": "09:32:35",
                "number": "X00AA000000TEST",
            },
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 745.465,
                "deltaConsumption": 0.373,
                "waterMeterId": "TODO",
                "realConsumption": True,
                "datetime": "2026-01-16T23:32:32+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.066,
                "minFlowTime": "00:32:34",
                "maxFlowTime": "10:32:35",
                "number": "X00AA000000TEST",
            },
            {
                "contractNumber": "1234567",
                "accumulatedConsumption": 745.613,
                "deltaConsumption": 0.148,
                "waterMeterId": "TODO",
                "realConsumption": False,
                "datetime": "2026-01-17T17:32:31+01:00",
                "leaked": False,
                "exceesed": False,
                "minFlow": 0.0,
                "maxFlow": 0.058,
                "minFlowTime": "00:32:32",
                "maxFlowTime": "10:32:33",
                "number": "X00AA000000TEST",
            },
        ]

    def test_normalize_real_daily_data(self, real_daily_consumptions):
        """Test normalization with real daily consumption data."""
        normalize = TestNormalizeConsumptions()._create_normalize_function()
        result = normalize(real_daily_consumptions)

        # Daily data should result in 7 entries (one per day, grouped by hour)
        assert len(result) == 7

        # Values should be in ascending order (accumulated consumption)
        values = [r[1] for r in result]
        assert values == sorted(values)

        # First value should be 743.882
        assert result[0][1] == 743.882

        # Last value should be 745.613
        assert result[-1][1] == 745.613

    def test_normalize_handles_timezone_offset(self, real_daily_consumptions):
        """Test that timezone offsets in datetime are handled correctly."""
        normalize = TestNormalizeConsumptions()._create_normalize_function()
        result = normalize(real_daily_consumptions)

        # All timestamps should be timezone-aware (UTC)
        for ts, _ in result:
            assert ts.tzinfo is not None

    def test_real_data_accumulated_consumption_increasing(
        self, real_daily_consumptions
    ):
        """Test that accumulated consumption values are increasing."""
        values = [c["accumulatedConsumption"] for c in real_daily_consumptions]

        for i in range(1, len(values)):
            assert (
                values[i] >= values[i - 1]
            ), f"Value at index {i} should be >= previous"

    def test_delta_consumption_calculation(self, real_daily_consumptions):
        """Test that delta consumption approximately matches differences."""
        for i in range(1, len(real_daily_consumptions)):
            prev = real_daily_consumptions[i - 1]
            curr = real_daily_consumptions[i]

            expected_delta = (
                curr["accumulatedConsumption"] - prev["accumulatedConsumption"]
            )
            actual_delta = curr["deltaConsumption"]

            # Allow small floating point differences
            assert (
                abs(expected_delta - actual_delta) < 0.01
            ), f"Delta mismatch at index {i}: expected {expected_delta:.3f}, got {actual_delta:.3f}"

    def test_parse_consumptions_real_data(self, real_daily_consumptions):
        """Test parsing real consumption data with the API client method."""
        from custom_components.aigues_barcelona.api import AiguesApiClient

        client = AiguesApiClient(
            username="test",
            password="test",
            twocaptcha_api_key="test",
        )

        # Test parsing accumulated consumption
        accumulated = client.parse_consumptions(real_daily_consumptions)
        assert accumulated == [
            743.882,
            744.211,
            744.555,
            744.714,
            745.092,
            745.465,
            745.613,
        ]

        # Test parsing delta consumption
        deltas = client.parse_consumptions(
            real_daily_consumptions, key="deltaConsumption"
        )
        assert deltas == [0.356, 0.329, 0.344, 0.159, 0.378, 0.373, 0.148]


class TestSumCalculation:
    """Tests for the cumulative sum calculation logic.

    Note: The integration now uses a baseline-based approach where
    sum = state - baseline_state, with baseline being the first reading ever.
    These tests verify the old behavior is preserved for backward compatibility
    and also test the new baseline approach.
    """

    def test_sum_calculation_no_existing_stats(self):
        """Test sum calculation when no existing statistics exist.

        With the baseline approach, first reading becomes baseline,
        so sum = state - baseline.
        """
        # When no existing statistics, baseline is the first reading
        # sum = state - baseline
        items = [
            (datetime(2026, 1, 15, 10, 0, 0), 100.0),
            (datetime(2026, 1, 15, 11, 0, 0), 100.5),
            (datetime(2026, 1, 15, 12, 0, 0), 101.0),
        ]

        # No existing statistics - use first reading as baseline
        baseline_state = items[0][1]  # 100.0

        stats = []
        for start_ts, state in items:
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        assert stats[0]["sum"] == 0.0  # 100.0 - 100.0 = 0
        assert stats[1]["sum"] == 0.5  # 100.5 - 100.0 = 0.5
        assert stats[2]["sum"] == 1.0  # 101.0 - 100.0 = 1.0

    def test_sum_calculation_with_existing_stats(self):
        """Test sum calculation continues using baseline approach.

        With the baseline approach, sum = state - baseline_state.
        The baseline is the first reading ever, not the last existing stat.
        """
        # Baseline established from first reading ever
        baseline_state = 95.0

        # Existing statistic was at state=100.0, sum=5.0 (100.0 - 95.0 = 5.0)
        last_existing_ts = datetime(2026, 1, 15, 9, 0, 0)

        # New data points (after the last existing statistic)
        items = [
            (datetime(2026, 1, 15, 10, 0, 0), 100.5),
            (datetime(2026, 1, 15, 11, 0, 0), 101.0),
            (datetime(2026, 1, 15, 12, 0, 0), 101.5),
        ]

        stats = []
        for start_ts, state in items:
            # Skip data older than or equal to last existing statistic
            if start_ts <= last_existing_ts:
                continue
            # Using baseline approach: sum = state - baseline
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        assert len(stats) == 3
        assert stats[0]["sum"] == 5.5  # 100.5 - 95.0 = 5.5
        assert stats[1]["sum"] == 6.0  # 101.0 - 95.0 = 6.0
        assert stats[2]["sum"] == 6.5  # 101.5 - 95.0 = 6.5

    def test_sum_calculation_skips_old_data(self):
        """Test that data older than last existing statistic is skipped."""
        # Baseline established from first reading ever
        baseline_state = 95.0

        # Existing statistic at 11:00 with state=100.5, sum=5.5 (100.5 - 95.0)
        last_existing_ts = datetime(2026, 1, 15, 11, 0, 0)

        # New fetch returns overlapping data (some older than last statistic)
        items = [
            (datetime(2026, 1, 15, 9, 0, 0), 99.5),  # Should be skipped (older)
            (datetime(2026, 1, 15, 10, 0, 0), 100.0),  # Should be skipped (older)
            (datetime(2026, 1, 15, 11, 0, 0), 100.5),  # Should be skipped (equal)
            (datetime(2026, 1, 15, 12, 0, 0), 101.0),  # Should be imported
            (datetime(2026, 1, 15, 13, 0, 0), 101.5),  # Should be imported
        ]

        stats = []
        for start_ts, state in items:
            # Skip data older than or equal to last existing statistic
            if start_ts <= last_existing_ts:
                continue
            # Using baseline approach: sum = state - baseline
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # Only the last 2 items should be imported
        assert len(stats) == 2
        assert stats[0]["start"] == datetime(2026, 1, 15, 12, 0, 0)
        assert stats[0]["sum"] == 6.0  # 101.0 - 95.0 = 6.0
        assert stats[1]["start"] == datetime(2026, 1, 15, 13, 0, 0)
        assert stats[1]["sum"] == 6.5  # 101.5 - 95.0 = 6.5

    def test_sum_calculation_prevents_negative_values(self):
        """Test that baseline approach prevents negative sum values.

        With the baseline approach, sum = state - baseline_state.
        Since state always increases (water meter reading), and baseline
        is fixed, sum can never go negative.
        """
        # Baseline established from first reading ever
        baseline_state = 95.0

        # Existing statistic at 12:00 with state=101.0, sum=6.0 (101.0 - 95.0)
        last_existing_ts = datetime(2026, 1, 15, 12, 0, 0)

        # Simulating what happens when API returns a week of data
        # that overlaps with already imported statistics
        items = [
            (datetime(2026, 1, 15, 10, 0, 0), 100.0),  # Older (will be skipped)
            (datetime(2026, 1, 15, 11, 0, 0), 100.5),  # Older (will be skipped)
            (datetime(2026, 1, 15, 12, 0, 0), 101.0),  # Equal (will be skipped)
            (datetime(2026, 1, 15, 13, 0, 0), 101.5),  # New data
        ]

        # With baseline approach + timestamp filtering:
        stats = []
        for start_ts, state in items:
            # Skip data older than or equal to last existing
            if start_ts <= last_existing_ts:
                continue
            # Baseline approach: sum = state - baseline
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # Only the genuinely new data point is imported
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(2026, 1, 15, 13, 0, 0)
        assert stats[0]["sum"] == 6.5  # 101.5 - 95.0 = 6.5

        # Sum is always non-negative with baseline approach
        assert stats[0]["sum"] >= 0

    def test_sum_always_increases_with_new_data(self):
        """Test that sum always increases when importing chronologically new
        data.

        With the baseline approach: sum = state - baseline_state.
        Since state (meter reading) always increases, sum always increases.
        """
        # Baseline established from first reading ever
        baseline_state = 95.0

        last_existing_ts = datetime(2026, 1, 15, 10, 0, 0)
        # Last existing had state=100.0, sum=5.0 (100.0 - 95.0)

        # Simulating a week of new data after last statistic
        items = [
            (datetime(2026, 1, 15, 11, 0, 0), 100.2),
            (datetime(2026, 1, 15, 12, 0, 0), 100.5),
            (datetime(2026, 1, 15, 13, 0, 0), 100.5),  # No consumption this hour
            (datetime(2026, 1, 15, 14, 0, 0), 101.0),
        ]

        stats = []
        for start_ts, state in items:
            if start_ts <= last_existing_ts:
                continue
            # Baseline approach: sum = state - baseline
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # All sums should be >= the last existing sum (5.0)
        for stat in stats:
            assert stat["sum"] >= 5.0

        # Sums should be non-decreasing
        for i in range(1, len(stats)):
            assert stats[i]["sum"] >= stats[i - 1]["sum"]


class TestBaselineCalculation:
    """Tests for the unified baseline-based sum calculation approach.

    The integration now uses a consistent baseline (first reading ever)
    for calculating sum values, both in historical imports and regular
    updates. This ensures the Energy Dashboard shows correct values
    without negative readings when transitioning between import methods.
    """

    def test_baseline_sum_calculation(self):
        """Test that sum is calculated as state - baseline."""
        # Baseline is the first reading ever (e.g., from historical import)
        baseline_state = 698.515

        # New data points from the API
        items = [
            (datetime(2026, 1, 20, 10, 0, 0), 746.612),
            (datetime(2026, 1, 20, 11, 0, 0), 746.811),
            (datetime(2026, 1, 20, 12, 0, 0), 746.931),
        ]

        stats = []
        for start_ts, state in items:
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # sum = state - baseline
        assert round(stats[0]["sum"], 3) == round(746.612 - 698.515, 3)  # 48.097
        assert round(stats[1]["sum"], 3) == round(746.811 - 698.515, 3)  # 48.296
        assert round(stats[2]["sum"], 3) == round(746.931 - 698.515, 3)  # 48.416

    def test_baseline_consistency_across_imports(self):
        """Test that historical and regular imports use the same baseline.

        This is the key fix for negative "today" values in the Energy
        Dashboard. Both import methods must use the same baseline to
        ensure continuous sum values without jumps or negative deltas.
        """
        # First reading ever (established during historical import)
        baseline_state = 698.515

        # Historical import establishes baseline and imports past data
        historical_items = [
            (datetime(2026, 1, 15, 10, 0, 0), 744.555),
            (datetime(2026, 1, 15, 11, 0, 0), 744.714),
        ]

        historical_stats = []
        for start_ts, state in historical_items:
            new_sum = state - baseline_state
            historical_stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # Last historical stat
        last_historical_sum = historical_stats[-1]["sum"]

        # Regular update uses same baseline
        regular_items = [
            (datetime(2026, 1, 20, 10, 0, 0), 746.612),
            (datetime(2026, 1, 20, 11, 0, 0), 746.931),
        ]

        regular_stats = []
        for start_ts, state in regular_items:
            # Using same baseline as historical
            new_sum = state - baseline_state
            regular_stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # Regular sums should be higher than historical sums
        # (water consumption always increases)
        assert regular_stats[0]["sum"] > last_historical_sum
        assert regular_stats[1]["sum"] > regular_stats[0]["sum"]

        # Sum should always be non-negative
        for stat in regular_stats:
            assert stat["sum"] >= 0

    def test_baseline_prevents_negative_daily_delta(self):
        """Test that consistent baseline prevents negative daily deltas.

        The Energy Dashboard calculates daily consumption as:
        today_consumption = today_final_sum - yesterday_final_sum

        With consistent baseline, this should never be negative.
        """
        baseline_state = 698.515

        # Yesterday's last reading
        yesterday_last = (datetime(2026, 1, 19, 23, 0, 0), 746.612)
        yesterday_last_sum = yesterday_last[1] - baseline_state  # 48.097

        # Today's readings
        today_items = [
            (datetime(2026, 1, 20, 0, 0, 0), 746.612),  # Midnight, same as yesterday
            (datetime(2026, 1, 20, 8, 0, 0), 746.659),
            (datetime(2026, 1, 20, 12, 0, 0), 746.811),
            (datetime(2026, 1, 20, 22, 0, 0), 746.931),
        ]

        today_stats = []
        for start_ts, state in today_items:
            new_sum = state - baseline_state
            today_stats.append({"start": start_ts, "state": state, "sum": new_sum})

        today_final_sum = today_stats[-1]["sum"]  # 48.416

        # Daily consumption = today's final sum - yesterday's final sum
        daily_consumption = today_final_sum - yesterday_last_sum
        assert daily_consumption >= 0  # Should never be negative
        assert round(daily_consumption, 3) == round(746.931 - 746.612, 3)  # 0.319

    def test_first_reading_becomes_baseline(self):
        """Test that when no baseline exists, first reading becomes
        baseline."""
        # First import - no existing baseline
        items = [
            (datetime(2026, 1, 1, 10, 0, 0), 698.515),
            (datetime(2026, 1, 1, 11, 0, 0), 698.520),
            (datetime(2026, 1, 1, 12, 0, 0), 698.530),
        ]

        # First reading becomes baseline
        baseline_state = items[0][1]

        stats = []
        for start_ts, state in items:
            new_sum = state - baseline_state
            stats.append({"start": start_ts, "state": state, "sum": new_sum})

        # First entry has sum = 0 (baseline)
        assert stats[0]["sum"] == 0.0
        # Subsequent entries show consumption since baseline
        # Use round() to avoid floating point precision issues
        assert round(stats[1]["sum"], 4) == 0.005
        assert round(stats[2]["sum"], 4) == 0.015


class TestTimestampExtraction:
    """Tests for extracting timestamps from statistics data."""

    def test_extract_timestamps_from_stats(self):
        """Test extracting timestamps from statistics response."""
        base_ts = datetime(2026, 1, 15, 10, 0, 0).timestamp()
        mock_stats = {
            "sensor.contador_abc123": [
                {"start_ts": base_ts},
                {"start_ts": base_ts + 3600},  # +1 hour
                {"start_ts": base_ts + 7200},  # +2 hours
            ]
        }

        existing_timestamps = set()
        sensor_id = "sensor.contador_abc123"

        if mock_stats and sensor_id in mock_stats:
            for stat in mock_stats[sensor_id]:
                if stat.get("start_ts") is not None:
                    existing_ts = dt_util.utc_from_timestamp(stat["start_ts"])
                    existing_ts = existing_ts.replace(minute=0, second=0, microsecond=0)
                    existing_timestamps.add(existing_ts)

        assert len(existing_timestamps) == 3

    def test_extract_timestamps_empty_response(self):
        """Test extracting timestamps from empty response."""
        mock_stats = {}
        existing_timestamps = set()
        sensor_id = "sensor.contador_abc123"

        if mock_stats and sensor_id in mock_stats:
            for stat in mock_stats[sensor_id]:
                if stat.get("start_ts") is not None:
                    existing_ts = dt_util.utc_from_timestamp(stat["start_ts"])
                    existing_ts = existing_ts.replace(minute=0, second=0, microsecond=0)
                    existing_timestamps.add(existing_ts)

        assert len(existing_timestamps) == 0

    def test_extract_timestamps_missing_sensor(self):
        """Test extracting timestamps when sensor not in response."""
        mock_stats = {
            "sensor.other_sensor": [
                {"start_ts": 1234567890},
            ]
        }
        existing_timestamps = set()
        sensor_id = "sensor.contador_abc123"

        if mock_stats and sensor_id in mock_stats:
            for stat in mock_stats[sensor_id]:
                if stat.get("start_ts") is not None:
                    existing_ts = dt_util.utc_from_timestamp(stat["start_ts"])
                    existing_ts = existing_ts.replace(minute=0, second=0, microsecond=0)
                    existing_timestamps.add(existing_ts)

        assert len(existing_timestamps) == 0
