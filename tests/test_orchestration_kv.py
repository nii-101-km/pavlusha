import tempfile
from pathlib import Path
import unittest

from pavlusha_agent.core import AgentError
from pavlusha_modules.orchestration_kv import SlotSnapshot


class SlotSnapshotTests(unittest.TestCase):
    def test_nested_snapshots_have_distinct_files_and_restore_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {}
            calls = []

            def request(route, payload=None):
                calls.append(route)
                if route == '/slots':
                    return [{'id': 0, 'is_processing': False}]
                name = payload['filename']
                if route.endswith('save'):
                    files[name] = len(files) + 1
                    (Path(directory) / name).write_bytes(b'kv')
                    return {'id_slot': 0, 'filename': name, 'n_saved': files[name], 'n_written': 2}
                return {'id_slot': 0, 'filename': name, 'n_restored': files[name], 'n_read': 2}

            outer = SlotSnapshot('http://localhost/v1', directory)
            inner = SlotSnapshot('http://localhost/v1', directory)
            outer._request = inner._request = request
            outer.save()
            inner.save()
            self.assertNotEqual(outer.snapshot[1], inner.snapshot[1])
            inner.restore()
            outer.restore()
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(calls.count('/slots/0?action=restore'), 2)

    def test_failed_restore_discards_snapshot_for_cold_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = SlotSnapshot('http://localhost/v1', directory)
            def request(route, payload=None):
                if route == '/slots':
                    return [{'id': 0, 'is_processing': False}]
                if route.endswith('save'):
                    (Path(directory) / payload['filename']).write_bytes(b'kv')
                    return {'id_slot': 0, 'filename': payload['filename'], 'n_saved': 1, 'n_written': 2}
                return {'id_slot': 0, 'filename': payload['filename'], 'n_restored': 0, 'n_read': 2}
            snapshot._request = request
            snapshot.save()
            with self.assertRaises(AgentError):
                snapshot.restore()
            self.assertIsNone(snapshot.snapshot)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_busy_slot_does_not_save(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = SlotSnapshot('http://localhost/v1', directory)
            snapshot._request = lambda route, payload=None: [{'id': 0, 'is_processing': True}]
            with self.assertRaises(AgentError):
                snapshot.save()
            self.assertEqual(list(Path(directory).iterdir()), [])
