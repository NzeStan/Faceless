# Changelog

This project follows [Semantic Versioning](https://semver.org/). The public API is the service
functions, the types, the errors, the signals, the adapter interface and the settings.

## 0.1.0 - 2026-11-05

### Added

- YouTube adapter with OAuth 2.0 and PKCE, resumable upload, quota tracking and reconciliation.
- Service functions: `start_connection`, `complete_connection`, `publish`, `reconcile`,
  `disconnect`, `get_capabilities`, `check_account_health`, `accounts_for_owner`,
  `publications_for_owner`, `purge_expired_states` and `rotate_tokens`.
- Optional DRF views and Celery tasks.
