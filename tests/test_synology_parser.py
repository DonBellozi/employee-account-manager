from datetime import date
import sys
import types

# Изолированный тест парсера не устанавливает SSH-соединение; подменяем только
# отсутствующий в тестовом runtime импорт Paramiko. В production он уже есть
# в requirements.txt.
sys.modules.setdefault("paramiko", types.ModuleType("paramiko"))

from app.services.synology import SynologyService


def test_enum_parser_accepts_plain_and_prefixed_logins():
    output = """
Local User:
admin
ivanov
user: external.partner
ivanov
Total: 3
"""
    assert SynologyService._parse_enum_output(output) == [
        "admin",
        "ivanov",
        "external.partner",
    ]


def test_detail_parser_reads_identity_state_and_expiry():
    output = """
User Name: [ivanov]
User uid: [1026]
Fullname: [Иванов Иван Иванович]
Email: [Ivanov@corp.ru]
Account Disabled: [0]
Expired: [0]
Expiration Date: [2026-11-11]
Groups: [users]
"""
    row = SynologyService._parse_detail("ivanov", output)
    assert row.login == "ivanov"
    assert row.stable_id == "uid:1026"
    assert row.uid == "1026"
    assert row.email == "ivanov@corp.ru"
    assert row.description == "Иванов Иван Иванович"
    assert row.is_active is True
    assert row.status == "active"
    assert row.expires_at == date(2026, 11, 11)
    assert row.protected is False


def test_detail_parser_detects_disabled_and_system_accounts():
    disabled = SynologyService._parse_detail(
        "former",
        """
User Name: former
User uid: 2048
Email: former@corp.ru
Account Disabled: 1
Expired: 0
""",
    )
    assert disabled.is_active is False
    assert disabled.status == "disabled"

    admin = SynologyService._parse_detail(
        "admin",
        """
User Name: admin
User uid: 1024
Email: admin@corp.ru
Account Disabled: 0
Groups: administrators
""",
    )
    assert admin.protected is True


def test_bool_parser_handles_enabled_as_not_disabled():
    assert SynologyService._as_bool("disabled") is True
    assert SynologyService._as_bool("expired") is True
    assert SynologyService._as_bool("enabled") is False
    assert SynologyService._as_bool("0") is False
