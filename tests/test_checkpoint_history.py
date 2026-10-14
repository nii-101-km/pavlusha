"""Actual filesystem publication and fallback to whole committed generations."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.checkpoint import validate_checkpoint
from pavlusha_agent.core import AgentError
from pavlusha_agent.state_store import StateStore
from tests import test_atomic_checkpoint_recovery as atomic_tests
from tests.test_reasoning_window import init_turn


class CheckpointHistoryTests(unittest.TestCase):
    def seed(self, root):
        return atomic_tests.AtomicCheckpointTests().seed(root)

    def test_failed_history_write_or_publication_preserves_primary(self):
        for boundary in ('open', 'fsync', 'replace'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self.seed(root)
                before = store.state_path.read_bytes()
                previous = store.load()['recovery_checkpoint']
                actual_open, actual_fsync, actual_replace = open, os.fsync, os.replace

                def open_file(path, *args, **kwargs):
                    if boundary == 'open' and Path(path).name.startswith('.checkpoint.tmp.'):
                        raise OSError('injected history open failure')
                    return actual_open(path, *args, **kwargs)

                def sync(fd):
                    if boundary == 'fsync' and stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError('injected history flush failure')
                    return actual_fsync(fd)

                def replace(source, destination):
                    if boundary == 'replace' and Path(destination).parent == store.checkpoint_history:
                        raise OSError('injected history publication failure')
                    return actual_replace(source, destination)

                with patch('builtins.open', side_effect=open_file), \
                     patch('pavlusha_agent.state_store.os.fsync', side_effect=sync), \
                     patch('pavlusha_agent.state_store.os.replace', side_effect=replace):
                    with self.assertRaisesRegex(OSError, 'injected history'):
                        store.complete_project_review(step=4, handoff='NEW')
                self.assertEqual(store.state_path.read_bytes(), before)
                restored = StateStore(root/'state', 'task', cold_restart=True).load()
                self.assertEqual(restored['recovery_checkpoint'], previous)

    def test_history_is_immutable_and_fallback_ignores_invalid_newer_and_temporary_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.seed(root)
            store.complete_project_review(step=4, handoff='SECOND')
            previous = copy.deepcopy(store.load()['recovery_checkpoint'])
            store.complete_project_review(step=5, handoff='THIRD')
            originals = {p.name: p.read_bytes() for p in store.checkpoint_history.glob('generation-*.json')}
            self.assertEqual(len(originals), 3)
            store.complete_project_review(step=6, handoff='FOURTH')
            for name, data in originals.items():
                self.assertEqual((store.checkpoint_history/name).read_bytes(), data)
            newest = max(store.checkpoint_history.glob('generation-*.json'))
            newest.write_text('{"incomplete":')
            # A fully valid unpublished temp image must not beat older commits.
            (store.checkpoint_history/'.checkpoint.tmp.dead').write_bytes(store.state_path.read_bytes())
            store.state_path.write_text('{"incomplete":')
            recovered = StateStore(root/'state', 'task', cold_restart=True).load()
            self.assertEqual(recovered['recovery_checkpoint'], previous)
            self.assertEqual(recovered['project_state'], previous['project_state'])
            validate_checkpoint(recovered, 'task')

    def test_missing_primary_recovers_committed_history_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.seed(root)
            previous = copy.deepcopy(store.load()['recovery_checkpoint'])
            store.complete_project_review(step=4, handoff='LATEST')
            store.state_path.unlink()
            code = ('import json,sys; from pathlib import Path; '
                    'from pavlusha_agent.state_store import StateStore; '
                    'print(json.dumps(StateStore(Path(sys.argv[1]), "task", cold_restart=True).load()))')
            process = subprocess.run([sys.executable, '-c', code, str(store.state_dir)],
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            recovered = json.loads(process.stdout)
            self.assertEqual(recovered['recovery_checkpoint'], previous)
            self.assertEqual(recovered['project_state'], previous['project_state'])
            self.assertTrue(store.state_path.exists())

    def test_failed_repair_keeps_historical_checkpoint_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.seed(root)
            historical = {p.name: p.read_bytes() for p in store.checkpoint_history.glob('generation-*.json')}
            store.state_path.write_text('broken')
            with patch('pavlusha_agent.state_store.os.replace', side_effect=OSError('repair failed')):
                with self.assertRaisesRegex(OSError, 'repair failed'):
                    StateStore(root/'state', 'task', cold_restart=True)
            for name, data in historical.items():
                self.assertEqual((store.checkpoint_history/name).read_bytes(), data)
            recovered = StateStore(root/'state', 'task', cold_restart=True).load()
            validate_checkpoint(recovered, 'task')

    def test_no_valid_history_fails_without_touching_data_or_using_wrong_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.seed(root)
            store.state_path.write_text('broken')
            before = store.state_path.read_bytes()
            with self.assertRaisesRegex(AgentError, 'RECOVERY FAILED'):
                StateStore(root/'state', 'different task', cold_restart=True)
            self.assertEqual(store.state_path.read_bytes(), before)
            for path in store.checkpoint_history.glob('generation-*.json'):
                path.write_text('broken')
            with self.assertRaisesRegex(AgentError, 'RECOVERY FAILED'):
                StateStore(root/'state', 'task', cold_restart=True)
            self.assertEqual(store.state_path.read_bytes(), before)

    def test_reset_preserves_history_but_excludes_it_from_new_run_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.seed(root)
            historical = {p.name: p.read_bytes() for p in store.checkpoint_history.glob('generation-*.json')}
            store = StateStore(root/'state', 'task', reset=True, cold_restart=True)
            preserved = next(store.state_dir.glob('checkpoint-history.reset-*'))
            self.assertEqual({p.name: p.read_bytes() for p in preserved.iterdir()}, historical)
            store.initialize_project(json.loads(init_turn().content), step=1)
            store.state_path.write_text('broken')
            with self.assertRaisesRegex(AgentError, 'RECOVERY FAILED'):
                StateStore(root/'state', 'task', cold_restart=True)

    def test_valid_legacy_single_file_checkpoint_without_history_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root/'state', 'task')
            expected = store.initialize_project(json.loads(init_turn().content), step=1)
            self.assertFalse(store.checkpoint_history.exists())
            recovered = StateStore(root/'state', 'task', cold_restart=True).load()
            self.assertEqual(recovered['project_state'], expected)
            validate_checkpoint(recovered, 'task')
