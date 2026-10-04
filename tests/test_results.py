import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
from main import display_results, restore_affinity
import winreg


class ResultTests(unittest.TestCase):
    def test_sidecar_logs_are_not_treated_as_csvs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'CPU-0.csv').write_text('msBetweenPresents\n4\n5\n6\n', encoding='utf-8')
            (path / 'CPU-0.presentmon.log').write_text('PresentMon diagnostic output')
            (path / 'readme.txt').write_text('notes')
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                display_results(directory, False)
            self.assertIn('200.00', stream.getvalue())

    def test_failed_session_cannot_be_ranked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'CSVs').mkdir()
            (path / 'session-status.json').write_text(json.dumps({'status': 'failed'}))
            with self.assertRaisesRegex(RuntimeError, 'refusing to rank'):
                display_results(str(path / 'CSVs'), False)

    def test_restore_preserves_preexisting_partial_binary_policy(self):
        original = {'DevicePolicy': None, 'AssignmentSetOverride': (b'\x08\x00', winreg.REG_BINARY)}
        with patch('main.winreg.CreateKey') as create, patch('main.winreg.SetValueEx') as write, \
             patch('main.winreg.DeleteValue', side_effect=FileNotFoundError), \
             patch('main.snapshot_affinity', return_value=original), patch('main.restart_driver', return_value=0) as restart:
            restore_affinity('TARGET', original)
            write.assert_called_once_with(create.return_value.__enter__.return_value,
                                         'AssignmentSetOverride', 0, winreg.REG_BINARY, b'\x08\x00')
            restart.assert_called_once_with('TARGET')

    def test_failed_restore_is_reported_before_driver_restart(self):
        with patch('main.winreg.CreateKey'), patch('main.winreg.DeleteValue'), \
             patch('main.snapshot_affinity', return_value={'DevicePolicy': (4, winreg.REG_DWORD)}), \
             patch('main.restart_driver') as restart:
            with self.assertRaisesRegex(RuntimeError, 'could not be restored'):
                restore_affinity('TARGET', {'DevicePolicy': None})
            restart.assert_not_called()


if __name__ == '__main__':
    unittest.main()
