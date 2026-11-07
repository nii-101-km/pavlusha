import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.extensions import Event
from pavlusha_modules.drawio import Drawio


XML = '''<mxfile><diagram name="Page-1"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="Start" vertex="1" parent="1"><mxGeometry x="30" y="30" width="120" height="60" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>'''


class DrawioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = Drawio(self.root, '/bin/true')

    def test_can_import_without_office_module(self):
        code = '''import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "pavlusha_modules.office" or name.startswith("pavlusha_modules.office."):
        raise ImportError("office disabled")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import pavlusha_modules.drawio
'''
        completed=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stderr)

    def test_save_and_tool_declarations(self):
        event = Event('session_started', 's', 0, 0)
        self.assertEqual([t.name for t in self.module.on_event(event).tools], ['drawio_save', 'drawio_export'])
        result = self.module.save({'output_path': 'diagram.drawio', 'xml': XML}, None).result
        self.assertEqual(result['pages'], 1)
        self.assertEqual((self.root / 'diagram.drawio').read_text(), XML)
        with self.assertRaises(FileExistsError):
            self.module.save({'output_path': 'diagram.drawio', 'xml': XML}, None)

    def test_invalid_xml_and_paths_are_rejected(self):
        for xml in ('<mxfile/>', '<!DOCTYPE foo><mxfile/>', '<mxfile>'):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                self.module.save({'output_path': 'bad.drawio', 'xml': xml}, None)
        with self.assertRaises(ValueError):
            self.module.save({'output_path': '../bad.drawio', 'xml': XML}, None)
        (self.root/'link.drawio').symlink_to(self.root/'outside.drawio')
        with self.assertRaises(ValueError):
            self.module.save({'output_path':'link.drawio','xml':XML},None)

    def test_export_copies_png_without_overwrite(self):
        self.module.save({'output_path': 'a.drawio', 'xml': XML}, None)
        def run(command, **kwargs):
            Path(command[command.index('--output') + 1]).write_bytes(b'png')
            return type('Result', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()
        with patch('pavlusha_modules.drawio.subprocess.run', side_effect=run):
            self.module.export({'path': 'a.drawio', 'output_path': 'a.png'}, None)
            self.assertEqual((self.root / 'a.png').read_bytes(), b'png')
            with self.assertRaises(ValueError):
                self.module.export({'path': 'a.drawio', 'output_path': 'a.png'}, None)

    def test_export_failure_does_not_publish_partial_output(self):
        self.module.save({'output_path':'a.drawio','xml':XML},None)
        with patch('pavlusha_modules.drawio.subprocess.run') as run:
            run.return_value=type('Result',(),{'returncode':1,'stderr':'failed','stdout':''})()
            with self.assertRaisesRegex(ValueError,'export failed'):
                self.module.export({'path':'a.drawio','output_path':'a.png'},None)
        self.assertFalse((self.root/'a.png').exists())
