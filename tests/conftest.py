"""Pytest fixtures for Aigues de Barcelona tests."""

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def mock_consumptions():
    """Return mock consumption data."""
    base_time = datetime(2026, 1, 15, 10, 0, 0)
    return [
        {
            "datetime": (base_time + timedelta(hours=i)).isoformat(),
            "accumulatedConsumption": 100.0 + (i * 0.5),
        }
        for i in range(24)
    ]


@pytest.fixture
def mock_consumptions_with_duplicates():
    """Return mock consumption data with multiple entries per hour."""
    base_time = datetime(2026, 1, 15, 10, 0, 0)
    consumptions = []
    for i in range(12):
        hour_time = base_time + timedelta(hours=i)
        # Add multiple entries per hour with different values
        for minute in [0, 15, 30, 45]:
            consumptions.append(
                {
                    "datetime": (hour_time + timedelta(minutes=minute)).isoformat(),
                    "accumulatedConsumption": 100.0 + (i * 0.5) + (minute * 0.01),
                }
            )
    return consumptions
