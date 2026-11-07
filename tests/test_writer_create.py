from tests.office_test_conversion import convert
import os
from pathlib import Path
import tempfile
import unittest

from pavlusha_agent.extensions import Event
from pavlusha_modules.office import create_extension


class WriterCreateTests(unittest.TestCase):
    def test_tool_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            office = create_extension(settings={}, context={'workdir': tmp, 'output_limit': 24000})
            tools = {t.name: t for t in office.on_event(Event('session_started', 's', 0, 0)).tools}
            self.assertIn('odt_create', tools)
            self.assertNotIn('docx_create', tools)
            self.assertFalse(tools['odt_create'].invoke({'output_path':'old.docx','paragraphs':[{'text':'Hi'}]},None).result['ok'])
            result = tools['odt_create'].invoke({'output_path':'../outside.odt', 'paragraphs':[{'text':'Hi'}]}, None).result
            self.assertEqual(result['error']['code'], 'invalid_request')
            office.on_event(Event('session_stopped', 's', 0, 0))


@unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_WRITER') == '1', 'Set PAVLUSHA_TEST_WRITER=1')
class WriterIntegrationTests(unittest.TestCase):
    def test_create_text_formatting_and_no_overwrite(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as tmp:
            office = create_extension(settings={}, context={'workdir': tmp, 'output_limit': 24000})
            result = office.create_odt('out.odt', [{'role':'title','text':'Заголовок'},
                {'text':'Первый абзац.'}, {'text':'Второй абзац.'}], paragraph_after_pt=14)
            self.assertTrue(result['ok'], result)
            from zipfile import ZipFile
            with ZipFile(Path(tmp)/'out.odt') as package:
                self.assertEqual(package.read('mimetype'),b'application/vnd.oasis.opendocument.text')
            doc = Document(convert(Path(tmp)/'out.odt','docx'))
            self.assertEqual([p.text for p in doc.paragraphs], ['Заголовок','Первый абзац.','Второй абзац.'])
            self.assertTrue(doc.paragraphs[0].runs[0].bold)
            for p in doc.paragraphs:
                self.assertEqual(p.runs[0].font.size.pt, 14)
                self.assertEqual(p.paragraph_format.line_spacing, 1.0)
                self.assertEqual(p.paragraph_format.space_after.pt, 14)
            self.assertEqual(doc.paragraphs[1].alignment, 3)
            self.assertAlmostEqual(doc.paragraphs[1].paragraph_format.first_line_indent.cm, 1.25, places=2)
            self.assertFalse(doc.paragraphs[0].paragraph_format.first_line_indent)
            self.assertEqual(doc.styles['Normal'].font.name, 'Liberation Serif')
            self.assertEqual(office.create_odt('out.odt',[{'text':'Other'}])['error']['code'],'destination_exists')
