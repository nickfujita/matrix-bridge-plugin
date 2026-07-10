import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_matrix import cli


class CodexCliNotifyInstallTests(unittest.TestCase):
    def test_install_notify_hook_replaces_multi_command_array_with_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            config_dir.mkdir()
            config_path = config_dir / "config.toml"
            config_path.write_text(
                'notify = [ "/usr/local/bin/notify-codex.sh", "/opt/legacy/codex-notify.sh" ]\n'
            )

            with patch.object(cli.Path, "home", return_value=home):
                cli._install_notify_hook()

            content = config_path.read_text()
            self.assertIn(f'notify = [ "{home}/.ccmatrix/codex-notify-wrapper.sh" ]', content)
            self.assertNotIn("/usr/local/bin/notify-codex.sh\", \"", content)

            wrapper = home / ".ccmatrix" / "codex-notify-wrapper.sh"
            matrix_notify = home / ".ccmatrix" / "codex-notify.sh"
            self.assertTrue(os.access(wrapper, os.X_OK))
            self.assertTrue(os.access(matrix_notify, os.X_OK))

            wrapper_text = wrapper.read_text()
            self.assertIn('/usr/local/bin/notify-codex.sh "$@"', wrapper_text)
            self.assertIn(f'{matrix_notify} "$@"', wrapper_text)

    def test_install_notify_hook_preserves_existing_wrapper_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            state_dir = home / ".ccmatrix"
            config_dir.mkdir()
            state_dir.mkdir()
            wrapper = state_dir / "codex-notify-wrapper.sh"
            matrix_notify = state_dir / "codex-notify.sh"
            config_path = config_dir / "config.toml"
            config_path.write_text(f'notify = [ "{wrapper}" ]\n')
            wrapper.write_text(
                "#!/bin/bash\n"
                '/usr/local/bin/notify-codex.sh "$@" >> /tmp/old.log 2>&1 || true\n'
                f'{matrix_notify} "$@" >> /tmp/old.log 2>&1 || true\n'
            )

            with patch.object(cli.Path, "home", return_value=home):
                cli._install_notify_hook()

            wrapper_text = wrapper.read_text()
            self.assertIn('/usr/local/bin/notify-codex.sh "$@"', wrapper_text)
            self.assertIn(f'{matrix_notify} "$@"', wrapper_text)


if __name__ == "__main__":
    unittest.main()
