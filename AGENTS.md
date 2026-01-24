# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Key commands

### Test environment
- Install test dependencies:
  - `pip install -r requirements-test.txt`

### Running tests
- Run all tests:
  - `pytest`
- Run tests in a single module:
  - `pytest tests/test_api.py`
  - `pytest tests/test_sensor.py`
- Run a single test case:
  - `pytest tests/test_api.py::TestAiguesApiClient::test_is_token_expired_with_valid_token`

There is no dedicated build or lint tooling configured in this repository; tests are the primary automated check.

## Repository overview

### Domain
This is a Home Assistant custom integration (`custom_components/aigues_barcelona`) that fetches water consumption data from Aigües de Barcelona using the official APIs and handles CAPTCHA challenges via 2Captcha. It exposes one or more water consumption sensors that are compatible with Home Assistant's Energy Dashboard and provides a service to reset and rebuild historical statistics when needed.

### High-level architecture

#### Integration entry point (`custom_components/aigues_barcelona/__init__.py`)
- Defines the integration entry (`async_setup_entry`) and unload logic (`async_unload_entry`).
- Creates an `AiguesApiClient` using credentials and 2Captcha API key stored in the config entry.
- Restores a saved JWT token from the config entry if available and checks if it is expired using `AiguesApiClient.is_token_expired()`.
- If the token is missing or expired, it triggers a login in an executor thread, retrieves a fresh token from the API client, and persists it back into the config entry.
- Forwards the config entry setup to the sensor platform and registers the custom service handlers via `service.async_setup`.
- Manages a `hass.data[DOMAIN]` dict that is shared with the sensor coordinator and service layer.

#### External API client (`custom_components/aigues_barcelona/api.py`)
- `AiguesApiClient` is a synchronous wrapper around the Aigües de Barcelona HTTP APIs using `requests.Session`.
- Responsibilities:
  - Handle login, including 2Captcha solving via `TwoCaptcha` and a cooldown mechanism (`_captcha_cooldown_until`) to avoid hammering 2Captcha when errors occur.
  - Manage the JWT token cookie (`ofexTokenJwt`), including setting it on the session and decoding fields like `exp` and `name`.
  - Provide high-level methods for:
    - `profile()` – fetch user profile info.
    - `contracts()` – list water contracts for the logged-in user.
    - `invoices()` / `invoices_debt()` – retrieve billing data.
    - `consumptions()` plus helpers `consumptions_week()` and `consumptions_month()` – fetch water consumption over a date range at hourly or daily frequency.
  - Centralize HTTP request handling and error mapping in `_query()`, raising exceptions for HTTP 4xx/5xx and rate limiting.
- Home Assistant code calls this client only via `hass.async_add_executor_job(...)` to keep I/O off the event loop; any new blocking I/O should follow the same pattern.

#### Configuration flow (`custom_components/aigues_barcelona/config_flow.py`)
- Implements the UI-based config flow and re-authentication for the integration.
- Validates that the username looks like a valid NIF/DNI/NIE via `check_valid_nif` before contacting the API.
- `validate_credentials()`:
  - Instantiates `AiguesApiClient`, performs a login via executor, and retrieves available contracts and the JWT token.
  - Returns contract IDs and token to be stored in the config entry.
  - Interprets specific API error responses to raise custom exceptions for invalid credentials, token expiration, and reCAPTCHA-related issues.
- `AiguesBarcelonaConfigFlow`:
  - `async_step_user` handles initial onboarding: validates credentials, fetches contracts, and creates a config entry keyed by username.
  - `async_step_reauth` and `async_step_reauth_confirm` implement the token refresh path used when the saved token is revoked/expired, reusing stored credentials and contracts and updating the existing config entry.
- The config flow relies on the `API_ERROR_TOKEN_REVOKED` constant and `api.last_response` to distinguish between different error paths (invalid auth vs token/recaptcha problems).

#### Constants and metadata (`custom_components/aigues_barcelona/const.py`, `version.py`)
- `const.py` centralizes all Home Assistant domain constants, configuration keys, API host details, cookie name, error messages, and reCAPTCHA parameters (page URL and site key).
- `version.py` reads `manifest.json` at runtime to expose the integration version via `VERSION`, which is used in the HTTP User-Agent string in `AiguesApiClient`.

#### Sensor platform and data coordinator (`custom_components/aigues_barcelona/sensor.py`)
- `async_setup_entry` (sensor platform):
  - Reads credentials, contracts, and saved token from the config entry.
  - For each contract, creates a `ContratoAgua` coordinator and a corresponding `ContadorAgua` sensor entity.
  - Schedules an initial refresh on startup (immediately if HA is already running, or on `EVENT_HOMEASSISTANT_START`).
- `ContratoAgua`:
  - Subclasses `TimestampDataUpdateCoordinator` and is the main stateful component for consumption data and statistics.
  - Owns its own `AiguesApiClient` instance per contract and ensures the token is valid before requests (`_ensure_token`, `_force_relogin`).
  - Stores live data in `hass.data[DOMAIN][contract]`, which is also accessed by the service layer.
  - `_async_update_data`:
    - Enforces a minimum refresh interval (skips updates if the last measurement is too recent).
    - Ensures the token is valid (with server-side invalidation handling) and fetches a rolling 7-day window of consumptions.
    - Updates in-memory state (`CONF_VALUE`, `CONF_STATE`) and imports statistics into Home Assistant's recorder using `_async_import_statistics`.
  - Recorder/statistics integration:
    - Uses `async_import_statistics()` and `statistics_during_period()` to maintain hourly statistics required by the Energy Dashboard.
    - Normalizes raw API data into hourly buckets (`_normalize_consumptions`).
    - Manages a "baseline" reading so that statistic `sum` values are always `state - baseline`, avoiding negative consumption and reconciling with historical imports.
    - Implements logic to detect and correct bad statistics (e.g., negative sums or sums computed with the wrong baseline) by selectively deleting and re-importing statistics (`_clear_statistics`, `_clear_statistics_from_timestamp`).
    - Provides helper methods for bulk historical import (`import_old_consumptions`) and an alternate import path that reuses pre-fetched timestamps (`_async_import_statistics_with_existing`).
- `ContadorAgua`:
  - A `SensorEntity` tied to a `ContratoAgua` coordinator.
  - Exposes a water consumption sensor (`SensorDeviceClass.WATER`, `SensorStateClass.TOTAL`, `UnitOfVolume.CUBIC_METERS`).
  - Uses coordinator data as its state and adds an extra attribute `ATTR_LAST_MEASURE` (last measurement timestamp parsed from ISO string).

#### Services (`custom_components/aigues_barcelona/service.py`)
- Registers a single service under the integration domain: `aigues_barcelona.reset_and_refresh_data`.
- Service handler flow:
  - Selects the first available contract from `hass.data[DOMAIN]` and retrieves its `ContratoAgua` coordinator.
  - Calls `clear_stored_data()` to clear recorder statistics via the coordinator.
  - Calls `fetch_historic_data()` to import ~1 year of historical consumptions using `import_old_consumptions` and then does a normal `async_refresh()` to get current data.
- This service is the canonical way to repair Energy Dashboard data if statistics become inconsistent.

#### Tests (`tests/`)
- `tests/test_api.py`:
  - Uses `pytest` to test URL generation, token handling, expiration logic, consumption parsing, and login cooldown behavior of `AiguesApiClient`.
- `tests/test_sensor.py`:
  - Focuses on the normalization of consumption data into hourly buckets, ensuring max-per-hour behavior and correct handling of timestamps.
- `tests/conftest.py`:
  - Provides shared pytest fixtures (including Home Assistant-specific helpers via `pytest-homeassistant-custom-component`).

## Notes for future changes
- Any new code that performs network I/O or interacts with the Aigües API must remain synchronous inside `AiguesApiClient` and be invoked via `hass.async_add_executor_job(...)` from async contexts.
- When modifying token handling, ensure consistency between:
  - Token stored in the config entry data (`CONF_TOKEN` / `"token"`), and
  - Token stored in the `requests.Session` cookie (used by `AiguesApiClient`).
  Both `__init__.py` and `sensor.py` (`ContratoAgua`) currently participate in the token lifecycle.
- Be careful when changing any of the statistics import or deletion logic in `sensor.py`; incorrect handling can corrupt Energy Dashboard data. Prefer adding tests that exercise `_normalize_consumptions`-like behavior and statistics imports when making such changes.
