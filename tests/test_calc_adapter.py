from tests.office_test_conversion import convert
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pavlusha_modules.office.calc_adapter import LibreOfficeCalcAdapter, OfficeAdapterError, revision


class CalcAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'input.ods').write_bytes(b'example')
        self.adapter = LibreOfficeCalcAdapter(self.root)
        self.rev = revision(self.root / 'input.ods')

    def test_rejects_paths_and_stale_revision_before_starting_worker(self):
        with patch('subprocess.Popen') as worker:
            for path in ('../input.ods', '/tmp/input.ods'):
                with self.assertRaises(OfficeAdapterError):
                    self.adapter.read(path, self.rev, 'Sheet', 'A1')
            with self.assertRaisesRegex(OfficeAdapterError, 'stale'):
                self.adapter.read('input.ods', 'old', 'Sheet', 'A1')
            worker.assert_not_called()

    def test_rejects_large_ranges_and_duplicate_edits(self):
        with self.assertRaises(OfficeAdapterError):
            self.adapter.read('input.ods', self.rev, 'Sheet', 'A1:Z1000')
        with self.assertRaises(OfficeAdapterError):
            self.adapter.edit('input.ods', 'out.ods', self.rev, [
                {'sheet': 'Sheet', 'cell': 'A1', 'value': 1},
                {'sheet': 'Sheet', 'cell': 'A1', 'value': 2}])

    def test_refuses_existing_output(self):
        with self.assertRaisesRegex(OfficeAdapterError, 'exists'):
            self.adapter.edit('input.ods', 'input.ods', self.rev,
                              [{'sheet': 'Sheet', 'cell': 'A1', 'value': 1}])

    def test_calc_tools_are_opt_in_and_return_domain_errors(self):
        from pavlusha_agent.extensions import Event
        from pavlusha_modules.office import create_extension

        event = Event('session_started', 'test', 0, 0)
        context = {'workdir': self.root, 'output_limit': 24000}
        default = create_extension(settings={}, context=context)
        names={tool.name for tool in default.on_event(event).tools}
        self.assertIn('ods_create',names)
        self.assertIn('ods_read',names)
        self.assertFalse(any(name.startswith(('docx_','xlsx_')) for name in names))
        default.on_event(Event('session_stopped', 'test', 0, 0))
        enabled = create_extension(settings={'enable_calc': True}, context=context)
        tools = {tool.name: tool for tool in enabled.on_event(event).tools}
        self.assertIn('ods_edit', tools)
        result = tools['ods_read'].invoke({'path': 'input.ods', 'revision': 'old',
            'sheet': 'Sheet', 'cell_range': 'A1'}, None).result
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'office_error')
        enabled.on_event(Event('session_stopped', 'test', 0, 0))

    def test_basic_macro_requires_explicit_opt_in_and_valid_output(self):
        from pavlusha_agent.extensions import Event
        from pavlusha_modules.office import create_extension
        context = {'workdir': self.root, 'output_limit': 24000}
        extension = create_extension(settings={'enable_basic_macro': True}, context=context)
        tools = {t.name: t for t in extension.on_event(Event('session_started', 'test', 0, 0)).tools}
        for name in ('../bad.ods', 'bad.xlsx'):
            result = tools['calc_basic_macro'].invoke({'code': 'Sub Main\nEnd Sub', 'output_path': name}, None).result
            self.assertEqual(result['error']['code'], 'invalid_request')
        extension.on_event(Event('session_stopped', 'test', 0, 0))


@unittest.skipUnless(__import__('os').environ.get('PAVLUSHA_TEST_CALC') == '1',
                     'Set PAVLUSHA_TEST_CALC=1 to run the real LibreOffice integration test')
class CalcIntegrationTests(unittest.TestCase):
    def test_create_native_empty_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=LibreOfficeCalcAdapter(Path(tmp))
            created=adapter.create('empty.ods',sheet='Данные')
            from zipfile import ZipFile
            with ZipFile(Path(tmp)/'empty.ods') as package:
                self.assertEqual(package.read('mimetype'),b'application/vnd.oasis.opendocument.spreadsheet')
            self.assertEqual(adapter.inspect('empty.ods')['sheets'][0]['name'],'Данные')
            self.assertEqual(created['revision'],revision(Path(tmp)/'empty.ods'))
            with self.assertRaises(OfficeAdapterError): adapter.create('empty.ods')
            with self.assertRaises(OfficeAdapterError): adapter.create('unsupported.xlsx')
    def test_basic_macro_creates_ods_with_embedded_code(self):
        from zipfile import ZipFile
        from pavlusha_modules.office import create_extension
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extension = create_extension(settings={'enable_basic_macro': True},
                                         context={'workdir': root, 'output_limit': 24000})
            code = 'Sub Main\nThisComponent.Sheets(0).getCellRangeByName("A1").String = "hello"\nEnd Sub'
            result = extension.basic_macro(code, 'macro.ods')
            self.assertTrue(result['ok'], result)
            with ZipFile(root / 'macro.ods') as archive:
                self.assertTrue(any('Pavlusha.xml' in n for n in archive.namelist()))
                self.assertIn(b'hello', archive.read('content.xml'))
            self.assertEqual(extension.basic_macro(code, 'macro.ods')['error']['code'], 'destination_exists')

    def test_read_edit_and_recalculate(self):
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Font
        from pavlusha_agent.extensions import Event
        from pavlusha_modules.office import create_extension

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = 'Data'
            sheet['A1'] = 2
            sheet['B1'] = '=A1*3'
            sheet['A1'].font = Font(bold=True)
            sheet.merge_cells('A3:B3')
            sheet['A3'] = 'Merged title'
            workbook.create_sheet('Other')['A1'] = 'Untouched'
            workbook.save(root / 'fixture.xlsx'); (root/'source.ods').write_bytes(convert(root/'fixture.xlsx','ods').read_bytes())
            extension = create_extension(settings={'enable_calc': True},
                                         context={'workdir': root, 'output_limit': 24000})
            tools = {t.name: t for t in extension.on_event(Event('session_started', 'test', 0, 0)).tools}
            self.addCleanup(extension.on_event, Event('session_stopped', 'test', 0, 0))
            rev = revision(root / 'source.ods')
            inspected = extension.calc.inspect('source.ods')
            self.assertEqual(inspected['source_revision'], rev)
            self.assertEqual([s['name'] for s in inspected['sheets']], ['Data', 'Other'])
            from pavlusha_modules.office.render_adapter import render
            rendered=render(root,'source.ods',rev,'preview',suffix='.ods')
            self.assertGreaterEqual(rendered['page_count'],1)
            self.assertTrue((root/rendered['pdf']).is_file())
            before = tools['ods_read'].invoke({'path': 'source.ods', 'revision': rev,
                'sheet': 'Data', 'cell_range': 'A1:B1'}, None).result
            self.assertTrue(before['ok'])
            self.assertEqual(before['cells'][1]['formula'], '=A1*3')
            self.assertEqual(before['cells'][1]['value'], 6)
            result = tools['ods_edit'].invoke({'path': 'source.ods', 'output_path': 'result.ods',
                'revision': rev, 'edits': [{'sheet': 'Data', 'cell': 'A1', 'value': 5},
                                           {'sheet':'Data','cell':'C1','date':'2026-11-07'}]}, None).result
            self.assertTrue(result['ok'])
            self.assertTrue(result['recalculated'])
            from datetime import datetime
            self.assertEqual(load_workbook(convert(root/'result.ods','xlsx'))['Data']['C1'].value,datetime(2026,11,7))
            self.assertEqual(revision(root / 'source.ods'), rev)
            exported=convert(root/'result.ods','xlsx')
            self.assertEqual(load_workbook(exported, data_only=True)['Data']['B1'].value, 15)
            saved = load_workbook(exported)
            self.assertEqual(saved['Data']['B1'].value, '=A1*3')
            self.assertTrue(saved['Data']['A1'].font.bold)
            self.assertIn('A3:B3', {str(r) for r in saved['Data'].merged_cells.ranges})
            self.assertEqual(saved['Other']['A1'].value, 'Untouched')
