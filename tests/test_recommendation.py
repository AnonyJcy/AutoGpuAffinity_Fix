import struct
import contextlib
import io
import json
import sys
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
from recommendation import parse_topology, recommend, display_recommendations


def core(cpus, group=0):
    return {'cpus': cpus, 'group': group, 'smt': len(cpus) > 1, 'efficiency_class': 0}


def scores(cpus):
    return ({str(cpu): {'maximum': 500 + cpu, 'average': 300 + cpu,
                       'minimum': 100 + cpu, 'lows1': 120 + cpu, 'stdev': -10 - cpu}
             for cpu in cpus}, {str(cpu): 2000 for cpu in cpus})


class RecommendationTests(unittest.TestCase):
    def test_changed_cpu_invalidates_previous_recommendations(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            captures = session / 'CSVs'
            captures.mkdir()
            (session / 'cpu-topology.json').write_text(json.dumps({'machine': 'old', 'cores': [core([0])]}))
            output = captures / 'core-recommendations.json'
            output.write_text(json.dumps({'status': 'heuristic', 'recommended': {'gpu': [2, 3]}}))
            with patch('recommendation.current_topology', return_value={'machine': 'new', 'cores': [core([0])]}), \
                 contextlib.redirect_stdout(io.StringIO()):
                display_recommendations(captures, {}, {})
            self.assertEqual(json.loads(output.read_text())['status'], 'unavailable')

    def test_native_topology_records_and_truncation(self):
        record = struct.pack('<II', 0, 48) + bytes([1, 0]) + bytes(20) + struct.pack('<H', 1)
        record += struct.pack('<QH', 12, 0) + bytes(6)
        self.assertEqual(parse_topology(record)[0]['cpus'], [2, 3])
        with self.assertRaises(ValueError):
            parse_topology(record[:-1])
        with self.assertRaises(RuntimeError):
            parse_topology(record, 4)

    def test_smt_excludes_zero_and_never_pairs_unrelated_threads(self):
        topology = {'cores': [core([0, 1]), core([2, 5]), core([3, 4]), core([6, 7]), core([8, 9])]}
        rows, frames = scores(range(10))
        report = recommend(rows, frames, topology)
        self.assertEqual([c['cpus'] for c in report['candidates']], [[6, 7], [8, 9]])
        self.assertEqual(report['recommended']['gpu']['cpus'], [8, 9])
        self.assertEqual(report['recommended']['mouse_keyboard']['cpus'], [6, 7])
        self.assertIsNone(report['recommended']['timer'])

    def test_no_smt_singletons_and_lower_stdev(self):
        rows, frames = scores(range(5))
        report = recommend(rows, frames, {'cores': [core([cpu]) for cpu in range(5)]})
        self.assertFalse(report['smt'])
        self.assertEqual(report['recommended']['timer']['cpus'], [1])
        self.assertEqual(report['recommended']['gpu']['mask_hex'], '0x10')

    def test_partial_pair_and_short_low_capture_not_recommended(self):
        rows, frames = scores([0, 1, 2, 3, 4, 5, 6])
        frames = {cpu: 100 for cpu in frames}
        report = recommend(rows, frames, {'cores': [core([cpu, cpu + 1]) for cpu in (0, 2, 4, 6)]})
        self.assertEqual(len(report['candidates']), 2)
        self.assertIsNone(report['recommended']['mouse_keyboard'])

    def test_processor_groups_and_topology_mismatch_refused(self):
        rows, frames = scores([0])
        with self.assertRaisesRegex(ValueError, 'processor groups'):
            recommend(rows, frames, {'cores': [core([0]), core([1], 1)]})
        with self.assertRaisesRegex(ValueError, 'do not match'):
            recommend(rows, frames, {'cores': [core([1])]})

    def test_pair_is_conservative_and_outlier_does_not_dominate(self):
        rows, frames = scores(range(8))
        rows['2'].update(maximum=1e9, average=1, minimum=1)
        report = recommend(rows, frames, {'cores': [core([cpu, cpu + 1]) for cpu in (0, 2, 4, 6)]})
        pair = next(c for c in report['candidates'] if c['cpus'] == [2, 3])
        self.assertEqual(pair['metrics']['maximum'], rows['3']['maximum'])
        self.assertEqual(report['recommended']['gpu']['cpus'], [6, 7])
