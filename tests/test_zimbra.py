import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# В минимальном окружении тестов Paramiko может быть не установлена.
# Подменяем только модуль импорта; в Docker она устанавливается из requirements.txt.
sys.modules.setdefault("paramiko", MagicMock())

from app.config import Settings
from app.services.zimbra import ZimbraService


class ZimbraSshAuthTests(unittest.TestCase):
    def _settings(self, **overrides):
        base = dict(
            _env_file=None,
            app_secret_key="1234567890abcdef",
            zimbra_ssh_host="mail.example.local",
            zimbra_ssh_user="provisioner",
        )
        base.update(overrides)
        return Settings(**base)

    def test_password_auth_passes_password_to_paramiko(self):
        with tempfile.TemporaryDirectory() as tmp:
            known_hosts = Path(tmp) / "known_hosts"
            known_hosts.write_text("placeholder", encoding="utf-8")
            settings = self._settings(
                zimbra_ssh_auth="password",
                zimbra_ssh_password="SshSecret!",
                zimbra_ssh_known_hosts=str(known_hosts),
            )
            fake_client = MagicMock()
            with patch("app.services.zimbra.paramiko.SSHClient", return_value=fake_client):
                ZimbraService(settings)._client()

            kwargs = fake_client.connect.call_args.kwargs
            self.assertEqual(kwargs["password"], "SshSecret!")
            self.assertNotIn("key_filename", kwargs)
            self.assertFalse(kwargs["look_for_keys"])
            self.assertFalse(kwargs["allow_agent"])

    def test_password_file_has_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            password_file = Path(tmp) / "ssh_password"
            password_file.write_text("FromFile!\n", encoding="utf-8")
            settings = self._settings(
                zimbra_ssh_auth="password",
                zimbra_ssh_password="FromEnv!",
                zimbra_ssh_password_file=str(password_file),
            )
            self.assertEqual(ZimbraService(settings)._read_ssh_password(), "FromFile!")

    def test_auto_uses_password_when_key_is_absent(self):
        settings = self._settings(
            zimbra_ssh_auth="auto",
            zimbra_ssh_private_key="/does/not/exist",
            zimbra_ssh_password="SshSecret!",
        )
        self.assertEqual(ZimbraService(settings)._resolve_ssh_auth(), "password")

    def test_missing_password_is_rejected(self):
        settings = self._settings(zimbra_ssh_auth="password")
        with self.assertRaisesRegex(RuntimeError, "ZIMBRA_SSH_PASSWORD"):
            ZimbraService(settings)._read_ssh_password()

    def test_close_account_sets_closed_status(self):
        settings = self._settings(dry_run=False)
        service = ZimbraService(settings)
        with patch.object(service, "_run_zmprov_direct") as run:
            service.close_account("User@Example.Local")
        run.assert_called_once_with(
            ["ma", "user@example.local", "zimbraAccountStatus", "closed"]
        )


if __name__ == "__main__":
    unittest.main()
