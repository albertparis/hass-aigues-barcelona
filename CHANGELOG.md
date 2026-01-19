# Changelog

All notable changes to this project will be documented in this file.

## [0.5.1]

### Added
- Automatic login with 2Captcha integration - no more manual token copying
- 2Captcha API rate limiting with cooldown periods to prevent balance exhaustion
- Service `aigues_barcelona.reset_and_refresh_data` to clear and reimport statistics
- Full compatibility with Home Assistant Energy Dashboard
- Comprehensive test suite for API client and sensor

### Changed
- Renamed project to "Aigües de Barcelona (2Captcha)" to reflect 2Captcha integration
- Default scan period changed from 4 hours to 8 hours to reduce API load
- Repository ownership transferred to @albertparis

### Fixed
- Auto-relogin when server rejects JWT token with "Invalid JWT" error
- Energy dashboard statistics calculation - fixed negative values issue
- Statistics import now correctly calculates cumulative sum from baseline
- KeyError for existing config entries upgrading from versions without 2Captcha
- Separated token refresh errors from consumption request errors in logs
- Duplicate statistics import prevention

## [0.5.0]

### Added
- 2Captcha integration for automatic CAPTCHA solving during login
- `2captcha-python` as a required dependency

### Changed
- Login flow now requires 2Captcha API key instead of manual token
- Removed manual token input step from configuration flow

## [0.4.7] - Previous Release (duhow/hass-aigues-barcelona)

See [original repository](https://github.com/duhow/hass-aigues-barcelona) for earlier changes.
