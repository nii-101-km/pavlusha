"""Storage selection and save/restore boundaries for controller-owned KV snapshots."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pavlusha_agent.core import AgentError
from pavlusha_agent.llama_server import ManagedLlamaServer


class KvStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.binary=self.root/'llama-server';self.binary.write_text('#!/bin/sh\n');self.binary.chmod(0o700)
        self.model=self.root/'model.gguf';self.model.touch()
    def manager(self, **options):
        config=self.root/'server.json'
        config.write_text(json.dumps({'argv':[str(self.binary),'--model',str(self.model)],**options}))
        return ManagedLlamaServer(config,'http://127.0.0.1:8899/v1',self.root/'server.log',10)
    def test_default_retains_disk_and_preservation_is_opt_in(self):
        manager=self.manager()
        self.assertEqual(manager.kv_storage,'disk')
        self.assertFalse(manager.preserve_kv)
        with manager._create_kv_directory() as directory:
            self.assertTrue(Path(directory).is_dir())
        self.assertFalse(Path(directory).exists())
    def test_invalid_storage_is_rejected(self):
        for value in ('gpu','',None,False,{},[]):
            with self.subTest(value=value),self.assertRaisesRegex(AgentError,'kv_storage'):
                self.manager(kv_storage=value)
    def test_ram_selection_requires_tmpfs_and_has_no_disk_fallback(self):
        manager=self.manager(preserve_kv=True,kv_storage='ram')
        with patch.object(manager,'_filesystem_type',return_value='ext4'), \
             patch('pavlusha_agent.llama_server.tempfile.TemporaryDirectory') as create:
            with self.assertRaisesRegex(AgentError,'no disk fallback'):manager._create_kv_directory()
            create.assert_not_called()
        with patch.object(manager,'_filesystem_type',return_value='tmpfs'), \
             patch('pavlusha_agent.llama_server.tempfile.TemporaryDirectory',side_effect=PermissionError('readonly')) as create:
            with self.assertRaises(PermissionError):manager._create_kv_directory()
            create.assert_called_once_with(prefix='pavlusha-kv-',dir='/dev/shm')
    def test_filesystem_resolution_uses_the_most_specific_mount(self):
        mounts=('1 0 0:1 / / rw - ext4 /dev/root rw\n'
                '2 1 0:2 / /dev rw - tmpfs tmpfs rw\n'
                '3 2 0:3 / /dev/shm rw - tmpfs tmpfs rw\n'
                '4 3 0:4 / /dev/shm/disk rw - ext4 /dev/data rw\n')
        with patch('pathlib.Path.read_text',return_value=mounts):
            self.assertEqual(ManagedLlamaServer._filesystem_type('/dev/shm/cache'),'tmpfs')
            self.assertEqual(ManagedLlamaServer._filesystem_type('/dev/shm/disk/cache'),'ext4')
    def test_ram_start_passes_memory_directory_to_server(self):
        manager=self.manager(preserve_kv=True,kv_storage='ram')
        directory=Mock(name='directory');directory.name='/dev/shm/pavlusha-kv-test'
        process=Mock();process.poll.return_value=None
        with patch('socket.create_connection',side_effect=ConnectionRefusedError), \
             patch.object(manager,'_create_kv_directory',return_value=directory), \
             patch.object(manager,'_ready',return_value=True), \
             patch('subprocess.Popen',return_value=process) as launch:
            manager.start()
        argv=launch.call_args.args[0]
        self.assertEqual(argv[-3:],('--slots','--slot-save-path',directory.name))
        self.assertEqual(manager.process,process)
    def test_prepared_directory_is_reused_and_cleaned(self):
        manager = self.manager(preserve_kv=True, kv_storage='ram')
        path = Path(manager.prepare_kv_directory())
        self.assertTrue(path.is_dir())
        self.assertEqual(str(path), manager.prepare_kv_directory())
        manager.__exit__(None, None, None)
        self.assertFalse(path.exists())
        self.assertIsNone(self.manager().prepare_kv_directory())
    def test_save_failure_keeps_model_loaded_and_does_not_execute(self):
        manager=self.manager(preserve_kv=True,kv_storage='ram')
        manager.process=Mock();manager.process.poll.return_value=None
        with patch.object(manager,'_confirm_model'), \
             patch.object(manager,'_save_kv',side_effect=AgentError('save failed')), \
             patch.object(manager,'stop') as stop:
            with self.assertRaisesRegex(AgentError,'save failed'):
                with manager.released_worker('model'):self.fail('workload must not execute')
            stop.assert_not_called()

    def test_interrupted_start_stops_server_and_cleans_kv_directory(self):
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error.__name__):
                manager=self.manager(preserve_kv=True)
                process=Mock();process.poll.return_value=None
                with patch('socket.create_connection',side_effect=ConnectionRefusedError), \
                     patch('subprocess.Popen',return_value=process), \
                     patch.object(manager,'_ready',side_effect=error), \
                     patch('os.killpg') as kill:
                    with self.assertRaises(error):
                        with manager:self.fail('startup must not succeed')
                kill.assert_called_once()
                process.wait.assert_called_once()
                self.assertIsNone(manager.process)
                self.assertIsNone(manager._kv_directory)
    def test_snapshot_survives_release_then_is_restored_and_deleted(self):
        manager=self.manager(preserve_kv=True)
        manager._kv_directory=tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(manager._kv_directory.cleanup)
        manager.process=Mock();manager.process.poll.return_value=None
        path=Path(manager._kv_directory.name)/'worker.bin'
        def request(route,payload=None):
            if route=='/slots':return [{'id':0,'is_processing':False}]
            if 'action=save' in route:
                path.write_bytes(b'kv snapshot')
                return {'id_slot':0,'filename':'worker.bin','n_saved':42,'n_written':11}
            if 'action=restore' in route:
                self.assertEqual(path.read_bytes(),b'kv snapshot')
                return {'id_slot':0,'filename':'worker.bin','n_restored':42,'n_read':11}
            self.fail(route)
        with patch.object(manager,'_confirm_model'),patch.object(manager,'_json',side_effect=request), \
             patch.object(manager,'stop'),patch.object(manager,'start'):
            with manager.released_worker('model'):
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())
