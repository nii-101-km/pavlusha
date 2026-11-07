from tests.office_test_conversion import convert
import os
from pathlib import Path
import tempfile
import unittest
from pavlusha_agent.extensions import Event
from pavlusha_modules.office import Office, create_extension


class WriterAppendTests(unittest.TestCase):
    def test_tools_and_input_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            office=create_extension(settings={},context={'workdir':tmp,'output_limit':24000})
            tools={t.name:t for t in office.on_event(Event('session_started','s',0,0)).tools}
            self.assertTrue({'odt_add_table','odt_add_image'}<=tools.keys())
            self.assertFalse(office.add_table(path='a.odt',output_path='b.odt',revision='x',rows=[['x'],['a','b']])['ok'])
            self.assertFalse(office.add_image(path='a.odt',output_path='b.odt',revision='x',image_path='../a.png')['ok'])
            office.on_event(Event('session_stopped','s',0,0))


@unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_WRITER')=='1','Set PAVLUSHA_TEST_WRITER=1')
class WriterAppendIntegrationTests(unittest.TestCase):
    def test_append_table_image_revision_and_preservation(self):
        from docx import Document
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); office=Office(root,24000)
            initial=office.create_odt('source.odt',[{'role':'title','text':'Тест'}, {'text':'Исходный текст.'}])
            self.assertTrue(initial['ok'],initial)
            import hashlib
            rev=hashlib.sha256((root/'source.odt').read_bytes()).hexdigest()
            table=office.add_table(path='source.odt',output_path='table.odt',revision=rev,
                rows=[['Товар','Число'],['Яблоки','5']],caption='Таблица 1')
            self.assertTrue(table['ok'],table)
            Image.new('RGB',(240,120),'blue').save(root/'image.png')
            image=office.add_image(path='table.odt',output_path='result.odt',revision=table['revision'],
                image_path='image.png',width_cm=10,alt_text='Синий прямоугольник',caption='Рисунок 1')
            self.assertTrue(image['ok'],image)
            doc=Document(convert(root/'result.odt','docx'))
            self.assertEqual([p.text for p in doc.paragraphs[:2]],['Тест','Исходный текст.'])
            self.assertEqual([[c.text for c in row.cells] for row in doc.tables[0].rows], [['Товар','Число'],['Яблоки','5']])
            self.assertEqual(len(doc.inline_shapes),1)
            self.assertAlmostEqual(doc.inline_shapes[0].width.cm,10,places=1)
            self.assertIn('Рисунок 1',[p.text for p in doc.paragraphs])
            self.assertIn('Синий прямоугольник',doc.inline_shapes[0]._inline.xml)
            self.assertEqual(hashlib.sha256((root/'source.odt').read_bytes()).hexdigest(),rev)
            self.assertFalse(office.add_table(path='source.odt',output_path='bad.odt',revision='stale',rows=[['x']])['ok'])
            self.assertFalse(office.add_table(path='source.odt',output_path='result.odt',revision=rev,rows=[['x']])['ok'])
            self.assertFalse((root/'bad.odt').exists())
