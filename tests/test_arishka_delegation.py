import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pavlusha_agent.extensions import Event
from pavlusha_modules.orchestration import ArishkaDelegation


class DelegationTests(unittest.TestCase):
    def test_remote_parent_delegates_and_collects_local_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); work=root/'work'; work.mkdir()
            manifest = root/'child.json'; manifest.write_text(json.dumps({'modules': []}))
            settings={'base_url':'http://127.0.0.1:8899/v1', 'model':'local', 'modules':str(manifest)}
            module=ArishkaDelegation(settings, {'workdir':str(work), 'backend':'perplexity-agent'})
            module.on_event(Event('session_started', 's', 0, 0, {'session_dir':str(root/'state')}))
            def run(cmd, check, env):
                self.assertNotIn('PAVLUSHA_API_KEY', env)
                config = json.loads(Path(cmd[cmd.index('--config')+1]).read_text())
                self.assertEqual(config['runtime']['base_url'], settings['base_url'])
                cwork=Path(cmd[cmd.index('--workdir')+1]); state=Path(cmd[cmd.index('--session-dir')+1])
                (cwork/'out.txt').write_text('ok');state.mkdir()
                (state/'session.json').write_text('{"status":"finished"}')
                (state/'events.jsonl').write_text(json.dumps({'kind':'turn_completed','stop':True,'result':{'summary':'ok'}})+'\n')
                (cwork.parent/'exit.json').write_text('{"exit_code":0}')
                return SimpleNamespace(returncode=0)
            with patch('pavlusha_modules.orchestration.shutil.which',return_value='terminal'), \
                 patch.dict('os.environ',{'DISPLAY':':1','PAVLUSHA_API_KEY':'secret'}), \
                 patch('pavlusha_modules.orchestration.subprocess.run',side_effect=run):
                result=module.delegate({'task':'test','inputs':[]},None).result
            module.collect({'child_id':result['child_id'],'paths':['out.txt']},None)
            self.assertEqual((work/'out.txt').read_text(),'ok')
            self.assertEqual(result['status'],'finished')
