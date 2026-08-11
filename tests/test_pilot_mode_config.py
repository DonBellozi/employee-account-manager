from __future__ import annotations

from app.config import Settings


def test_global_dry_run_is_not_configurable(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    settings = Settings(
        app_secret_key="pilot-secret-key-1234567890",
        _env_file=None,
    )
    assert settings.dry_run is False


def test_legacy_constructor_value_cannot_enable_dry_run():
    settings = Settings(
        app_secret_key="pilot-secret-key-1234567890",
        dry_run=True,
        _env_file=None,
    )
    assert settings.dry_run is False
