from tests.office_test_conversion import convert
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile
from xml.etree import ElementTree

from pavlusha_agent.extensions import Event
from pavlusha_modules.office import Office, create_extension


class WriterOperationTests(unittest.TestCase):
    def test_exposed_and_invalid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            office=create_extension(settings={},context={'workdir':tmp,'output_limit':24000})
            tools={t.name for t in office.on_event(Event('session_started','s',0,0)).tools}
            self.assertTrue({'odt_insert_paragraph','odt_insert_list','odt_replace_table','odt_format_paragraph','odt_normalize'} <= tools)
            self.assertFalse(office.insert_list(path='x.odt',output_path='y.odt',revision='bad',items=[None])['ok'])
            self.assertFalse(office.format_paragraph(path='x.odt',output_path='y.odt',revision='bad',match_text='A')['ok'])
            office.on_event(Event('session_stopped','s',0,0))


@unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_WRITER')=='1','Set PAVLUSHA_TEST_WRITER=1')
class WriterOperationIntegrationTests(unittest.TestCase):
    def test_uno_inspect_returns_revision_and_ordered_content(self):
        from pavlusha_modules.office.writer_append import inspect_writer, replace_writer_text
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); office=Office(root,24000)
            result=office.create_odt('base.odt',[{'text':'Заголовок','role':'title'},{'text':'Пример текста'}])
            self.assertTrue(result['ok'],result)
            info=inspect_writer(root,'base.odt')
            self.assertEqual(info['revision'],hashlib.sha256((root/'base.odt').read_bytes()).hexdigest())
            self.assertEqual([n['text'] for n in info['nodes'] if n['type']=='paragraph'],['Заголовок','Пример текста'])
            with self.assertRaisesRegex(ValueError,'stale'):
                inspect_writer(root,'base.odt',revision='wrong')
            edited=replace_writer_text(root,'base.odt','edited.odt',info['revision'],
                [{'node_id':'paragraph:2','old':'Пример','new':'Новый'}])
            self.assertTrue(edited['ok'])
            self.assertEqual(inspect_writer(root,'edited.odt')['nodes'][1]['text'],'Новый текста')
            repeated=office.create_odt('repeated.odt',[{'text':'Общее слово'}, {'text':'Общее слово'}])
            info=inspect_writer(root,'repeated.odt')
            replace_writer_text(root,'repeated.odt','second.odt',info['revision'],
                [{'node_id':'paragraph:2','old':'Общее','new':'Другое'}])
            self.assertEqual([n['text'] for n in inspect_writer(root,'second.odt')['nodes']],
                             ['Общее слово','Другое слово'])

    def test_libreoffice_render_from_snapshot(self):
        from pavlusha_modules.office.render_adapter import render
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); office=Office(root,24000)
            created=office.create_odt('base.odt',[{'text':'Страница'}])
            self.assertTrue(created['ok'],created)
            rev=hashlib.sha256((root/'base.odt').read_bytes()).hexdigest()
            result=render(root,'base.odt',rev,'preview',suffix='.odt')
            self.assertEqual(result['page_count'],1)
            self.assertTrue((root/result['pages'][0]).is_file())
            self.assertTrue((root/result['pdf']).is_file())
            with self.assertRaises(ValueError): render(root,'base.odt',rev,'preview',suffix='.odt')

    def test_primary_tools_paginate_and_reject_stale_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); office=Office(root,24000)
            created=office.create_odt('base.odt',[{'text':'Первый'}, {'text':'Второй'}])
            self.assertTrue(created['ok'],created)
            office.on_event(Event('session_started','s',0,0))
            self.addCleanup(office.on_event,Event('session_stopped','s',0,0))
            first=office.native_call('odt','inspect',{'path':'base.odt','limit':1})
            self.assertTrue(first['ok'],first)
            second=office.native_call('odt','inspect',{'path':'base.odt','limit':1,'cursor':first['next_cursor']})
            self.assertEqual(second['items'][0]['text'],'Второй')
            result=office.native_call('odt','read',{'path':'base.odt','revision':'bad'})
            self.assertFalse(result['ok'])

    def test_insertion_replacement_lists_and_alignment(self):
        from docx import Document
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);o=Office(root,24000)
            created=o.create_odt('base.odt',[{'text':'Заголовок','role':'title'},
                {'text':'Первый абзац.'},{'text':'Последний абзац.'}])
            self.assertTrue(created['ok'],created)
            base_hash=hashlib.sha256((root/'base.odt').read_bytes()).hexdigest()
            def apply(method,source,destination,**kwargs):
                revision=hashlib.sha256((root/source).read_bytes()).hexdigest()
                result=getattr(o,method)(path=source,output_path=destination,revision=revision,**kwargs)
                self.assertTrue(result['ok'],result)
                return result
            apply('insert_paragraph','base.odt','paragraph.odt',text='Вставленный абзац.',after_text='Первый абзац.')
            apply('insert_list','paragraph.odt','list.odt',items=['Первый пункт','Второй пункт'],kind='bullet',after_text='Вставленный абзац.')
            apply('add_table','list.odt','table.odt',rows=[['Имя','Число'],['А','1']],after_text='Первый абзац.')
            apply('replace_table','table.odt','replaced.odt',table_index=1,
                rows=[['Имя','Число','Да'],['Б','2','x'],['В','3','y']])
            Image.new('RGB',(400,200),'blue').save(root/'image.png')
            apply('add_image','replaced.odt','image.odt',image_path='image.png',width_cm=8,
                after_text='Вставленный абзац.',caption='Рисунок',alt_text='Синий прямоугольник')
            apply('format_paragraph','image.odt','final.odt',match_text='Последний абзац.',alignment='right')
            doc=Document(convert(root/'final.odt','docx'))
            self.assertEqual([[cell.text for cell in row.cells] for row in doc.tables[0].rows],
                [['Имя','Число','Да'],['Б','2','x'],['В','3','y']])
            self.assertEqual(len(doc.inline_shapes),1)
            self.assertEqual(hashlib.sha256((root/'base.odt').read_bytes()).hexdigest(),base_hash)
            with ZipFile(convert(root/'final.odt','docx')) as package:
                xml=ElementTree.fromstring(package.read('word/document.xml'))
            ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            body=xml.find('w:body',ns)
            blocks=[]
            for el in body:
                name=el.tag.rsplit('}',1)[-1]
                if name=='p': blocks.append(('p',''.join(n.text or '' for n in el.findall('.//w:t',ns))))
                elif name=='tbl': blocks.append(('table',''))
            self.assertLess(blocks.index(('p','Первый абзац.')),blocks.index(('table','')))
            self.assertLess(blocks.index(('table','')),blocks.index(('p','Вставленный абзац.')))
            self.assertLess(blocks.index(('p','Вставленный абзац.')),blocks.index(('p','Рисунок')))
            self.assertLess(blocks.index(('p','Рисунок')),blocks.index(('p','Первый пункт')))
            self.assertLess(blocks.index(('p','Второй пункт')),blocks.index(('p','Последний абзац.')))
            caption=next(el for el in body.findall('w:p',ns) if ''.join(n.text or '' for n in el.findall('.//w:t',ns))=='Рисунок')
            self.assertEqual(len(caption.findall('.//w:drawing',ns)),0)
            preceding=list(body)[list(body).index(caption)-1]
            self.assertTrue(preceding.findall('.//w:drawing',ns))
            list_paras=[el for el in body.findall('w:p',ns) if ''.join(n.text or '' for n in el.findall('.//w:t',ns)) in ('Первый пункт','Второй пункт')]
            self.assertEqual(len(list_paras),2)
            self.assertTrue(all(p.find('w:pPr/w:numPr',ns) is not None for p in list_paras))
            last=next(el for el in body.findall('w:p',ns) if ''.join(n.text or '' for n in el.findall('.//w:t',ns))=='Последний абзац.')
            self.assertIn(last.find('w:pPr/w:jc',ns).get('{%s}val'%ns['w']),('right','end'))
            apply('format_paragraph','final.odt','title.odt',match_text='Первый абзац.',role='title',alignment='center')
            apply('normalize_document','title.odt','normalized.odt',font='Liberation Serif',size=14)
            normalized=Document(convert(root/'normalized.odt','docx'))
            heading=next(p for p in normalized.paragraphs if p.text=='Первый абзац.')
            ordinary=next(p for p in normalized.paragraphs if p.text=='Последний абзац.')
            self.assertTrue(heading.runs[0].bold)
            self.assertFalse(ordinary.runs[0].bold)
            self.assertFalse(ordinary.runs[0].italic)
            self.assertEqual(heading.runs[0].font.size.pt,14)
            self.assertAlmostEqual(ordinary.paragraph_format.first_line_indent.cm,1.25,places=2)
            list_item=next(p for p in normalized.paragraphs if p.text=='Первый пункт')
            self.assertFalse(list_item.paragraph_format.first_line_indent)
            self.assertFalse(heading.paragraph_format.first_line_indent)
            self.assertFalse(normalized.tables[0].cell(1,0).paragraphs[0].paragraph_format.first_line_indent)
            self.assertEqual(ordinary.paragraph_format.line_spacing,1.0)
            self.assertEqual(len(normalized.inline_shapes),1)
            bad=o.insert_paragraph(path='base.odt',output_path='bad.odt',revision=base_hash,text='x',after_text='не найден')
            self.assertFalse(bad['ok'])
            self.assertFalse((root/'bad.odt').exists())
            shrink=apply('replace_table','replaced.odt','smaller.odt',table_index=1,rows=[['Одна ячейка']],header=False)
            self.assertTrue(shrink['ok'])
            self.assertEqual([[c.text for c in row.cells] for row in Document(convert(root/'smaller.odt','docx')).tables[0].rows],[['Одна ячейка']])
            merged=Document(); table=merged.add_table(rows=2,cols=2)
            table.cell(0,0).merge(table.cell(0,1));merged.save(root/'merged.docx'); (root/'merged.odt').write_bytes(convert(root/'merged.docx','odt').read_bytes())
            merged_rev=hashlib.sha256((root/'merged.odt').read_bytes()).hexdigest()
            rejected=o.replace_table(path='merged.odt',output_path='merged-result.odt',revision=merged_rev,
                                     table_index=1,rows=[['новая']])
            self.assertFalse(rejected['ok'])
            self.assertIn('Merged',rejected['error']['message'])
            self.assertFalse((root/'merged-result.odt').exists())
