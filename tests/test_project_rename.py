from pathlib import Path

from app.config import Settings


def test_new_project_defaults(monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_name == "Управление жизненным циклом учетных записей"
    assert settings.session_cookie_name == "employee_offboarding_manager_session"
    assert settings.dry_run is False


def test_compose_uses_new_runtime_name():
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "name: account-provisioner" in text
    assert "  employee-offboarding-manager:" in text
    assert "container_name: employee-offboarding-manager" in text
    assert "account-provisioner-data:/app/data" in text
    assert "./certs:/app/certs:ro" in text
    assert "./ssh/known_hosts:/app/known_hosts:ro" in text


def test_base_template_uses_new_brand():
    text = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "Управление жизненным циклом учетных записей" in text
