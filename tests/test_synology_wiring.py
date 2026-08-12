from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_main_registers_router_and_scheduler():
    text = read("app/main.py")
    assert "from app.services.synology_scheduler import SynologyLifecycleScheduler" in text
    assert "synology_scheduler = SynologyLifecycleScheduler(settings, SessionLocal)" in text
    assert "synology_scheduler.start()" in text
    assert "synology_scheduler.stop()" in text
    assert "app.include_router(synology.router)" in text


def test_config_and_compose_have_synology_readonly_connection():
    config = read("app/config.py")
    compose = read("docker-compose.yml")
    for name in (
        "synology_enabled",
        "synology_ssh_host",
        "synology_ssh_user",
        "synology_ssh_known_hosts",
        "synology_synouser_command",
    ):
        assert name in config
    for env in (
        "SYNOLOGY_ENABLED",
        "SYNOLOGY_SSH_HOST",
        "SYNOLOGY_SSH_USER",
        "SYNOLOGY_SSH_KNOWN_HOSTS",
        "SYNOLOGY_SYNOUSER_COMMAND",
    ):
        assert env in compose
    # Не создаем новый Portainer bind-mount: используется уже рабочий
    # /app/known_hosts, примонтированный для Zimbra.
    assert "/opt/account-provisioner/ssh/known_hosts:/app/known_hosts:ro" in compose


def test_settings_has_synology_card_and_readonly_page():
    js = read("app/static/settings_extensions.js")
    page = read("app/templates/synology.html")
    assert "href: '/settings/synology'" in js
    assert "Synology DSM" in js
    assert "Только чтение" in page
    assert "которой нет среди действующих работников" in page


def test_first_phase_contains_no_synology_mutating_command_methods():
    service = read("app/services/synology.py")
    # Разрешены только read-side вызовы enum/get/help.
    assert '["--enum", "local"]' in service
    assert '["--get", login]' in service
    assert '["--help"]' in service
    for token in ("--del", "--delete", "--disable", "--setpw", "--modify"):
        assert token not in service


def test_gradual_batch_is_bounded_and_waits_for_pending_batch():
    lifecycle = read("app/services/synology_lifecycle.py")
    assert "migration_batch_size" in lifecycle
    assert "last_migration_batch_at" in lifecycle
    assert "SystemRandom().sample" in lifecycle
    assert "if pending:" in lifecycle
    assert "ACTION_SET_EXPIRY_INTERNAL" in lifecycle
