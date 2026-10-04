import csv
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
from presentation import audit, refresh_paced


class PresentationTests(unittest.TestCase):
    def capture(self, directory, frames, mode='Hardware: Independent Flip', pid=100, tearing='0'):
        path = Path(directory) / 'capture.csv'
        with path.open('w', newline='', encoding='utf-8-sig') as file:
            writer = csv.writer(file)
            writer.writerow(['ProcessID', 'msBetweenPresents', 'PresentMode', 'SyncInterval', 'AllowsTearing'])
            writer.writerows([pid, frame, mode, '0', tearing] for frame in frames)
        return path

    def test_refresh_pacing_detected_at_different_monitor_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            for refresh in (60, 120, 144, 165, 240):
                stats = audit(self.capture(directory, [1000 / refresh] * 100), 100)
                self.assertTrue(refresh_paced(stats, refresh))
                self.assertFalse(refresh_paced(stats, None))

    def test_uncapped_or_naturally_slow_capture_not_mistaken_for_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            for intervals in ([4.4] * 100, [12] * 100, [2, 10.12] * 50):
                self.assertFalse(refresh_paced(audit(self.capture(directory, intervals), 100), 165))
            for mode, tearing in [('Composed: Flip', '0'), ('Hardware: Independent Flip', '1')]:
                self.assertFalse(refresh_paced(audit(self.capture(directory, [1000/165]*100, mode=mode, tearing=tearing), 100), 165))

    def test_wrong_pid_empty_and_invalid_frame_times_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'another process'):
                audit(self.capture(directory, [6, 6], pid=99), 100)
            for frames in ([], [0, 1], ['nan', 6], ['inf', 6], [-1, 6]):
                with self.assertRaises(RuntimeError):
                    audit(self.capture(directory, frames), 100)


if __name__ == '__main__':
    unittest.main()
