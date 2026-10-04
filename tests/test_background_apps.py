import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
import background_apps


class BackgroundConsentTests(unittest.TestCase):
    def test_unattended_does_not_scan_and_default_skip_does_not_close(self):
        with patch('background_apps.candidates', return_value=[]) as scan, patch('background_apps.close_windows') as close, \
             patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            background_apps.assist(True)
            scan.assert_not_called()
            background_apps.assist(False)
        close.assert_not_called()

    def test_only_one_is_consent(self):
        group = {'label': 'test', 'items': [{'pid': 1}], 'cpu': 0, 'memory': 0}
        for reply in ('', '0', '全部', '取消'):
            with patch('background_apps.candidates', return_value=[]), \
                 patch('background_apps.ranked_apps', return_value=[group]), \
                 patch('background_apps.close_windows') as close, \
                 patch('builtins.input', return_value=reply), contextlib.redirect_stdout(io.StringIO()):
                background_apps.assist()
            close.assert_not_called()

    def test_reused_pid_is_excluded_after_confirmation(self):
        item = {'label': 'test', 'name': 'test.exe', 'pid': 1, 'exe': 'test', 'created': 100}
        changed = dict(item, created=101)
        with patch('background_apps.candidates', side_effect=[[item], [changed]]), \
             patch('background_apps.ranked_apps', return_value=[{'label': 'test', 'items': [item], 'cpu': 0, 'memory': 0}]), \
             patch('background_apps.close_windows', return_value=set()) as close, \
             patch('builtins.input', side_effect=['1', '']), \
             contextlib.redirect_stdout(io.StringIO()):
            background_apps.assist()
        close.assert_called_once_with(set())

    def test_software_grouping_and_top_ten(self):
        from unittest.mock import Mock
        items = [{'pid': i, 'created': 100, 'exe': 'test', 'label': f'app-{i // 2}', 'cpu_seconds': 0}
                 for i in range(24)]
        def process(pid):
            result = Mock()
            result.create_time.return_value = 100
            result.exe.return_value = 'test'
            result.cpu_times.return_value = (pid // 2, 0)
            result.memory_info.return_value.rss = 100
            return result
        with patch('background_apps.time.sleep'), patch('background_apps.time.monotonic', side_effect=[0] + [1] * 24), \
             patch('background_apps.psutil.Process', side_effect=process), patch('background_apps.os.cpu_count', return_value=1):
            groups = background_apps.ranked_apps(items)
        self.assertEqual(len(groups), 10)
        self.assertEqual(groups[0]['label'], 'app-11')
        self.assertTrue(all(len(group['items']) == 2 for group in groups))
        self.assertNotIn('app-0', [group['label'] for group in groups])
