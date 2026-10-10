"""Mechanical DOCX pack tests. Run with a Python containing tools/requirements-docx.txt."""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from zipfile import ZipFile

from pavlusha_agent.functions import FunctionRegistry

_HAVE_DOCX = bool(importlib.util.find_spec('docx') and importlib.util.find_spec('lxml'))
if _HAVE_DOCX:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from PIL import Image

PACK = Path(__file__).resolve().parents[1] / 'tools/docx_functions.py'
RENDERER = Path(os.getenv('PAVLUSHA_DOCX_RENDERER', '/nonexistent/render_docx.py'))


def load_pack(root, argv=None):
    with patch.object(sys, 'argv', argv if argv is not None else ['agent.py', '--workdir', str(root)]):
        spec = importlib.util.spec_from_file_location('docx_pack_test', PACK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def fixture(path):
    document = Document()
    document.add_heading('Launch brief', 0)
    p = document.add_paragraph('Launch date: ')
    p.add_run('15 ')
    p.add_run('November')
    document.add_paragraph('Historical review held on 15 November.')
    table = document.add_table(rows=4, cols=2)
    for row, values in zip(table.rows, [('Item', 'Amount'), ('A', '120 000'), ('B', '80 000'), ('Total', '240 000')]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.add_paragraph('After table.')
    document.sections[0].header.paragraphs[0].text = 'Launch: 15 November'
    document.sections[0].footer.paragraphs[0].text = 'Internal review copy'
    image = path.parent / 'sample.png'
    Image.new('RGB', (50, 20), 'navy').save(image)
    document.add_picture(str(image))
    document.save(path)


class RegistryTests(unittest.TestCase):
    def test_only_four_public_functions_and_supported_signatures(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, 'argv', ['agent.py', '--workdir', tmp]):
            registry = FunctionRegistry([PACK])
            self.assertEqual(list(registry.functions), ['docx_inspect', 'docx_read', 'docx_edit', 'docx_render'])
            self.assertEqual([d['name'] for d in registry.descriptions], list(registry.functions))

    def test_workdir_forms_default_and_frozen_root_ignore_old_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for option in (['--workdir', str(root)], ['--workdir=' + str(root)],
                           ['--workdir', '/unused', '--workdir', str(root)]):
                with self.subTest(option=option), patch.dict(os.environ, {'PAVLUSHA_DOCX_ROOT': '/unused'}):
                    pack = load_pack(root, ['agent.py', *option, '--functions', str(PACK), 'task'])
                    self.assertEqual(pack._ROOT, root.resolve())
                    with patch.object(sys, 'argv', ['agent.py', '--workdir', '/changed']), \
                         patch('os.getcwd', return_value='/tmp'):
                        self.assertEqual(pack._path('file.docx'), root / 'file.docx')
            with patch('os.getcwd', return_value=str(root)):
                pack = load_pack(root, ['agent.py'])
                self.assertEqual(pack._ROOT, root / 'agent-work')
                pack = load_pack(root, ['agent.py', '--workdir', 'relative work'])
                self.assertEqual(pack._ROOT, root / 'relative work')

    def test_transport_limit_uses_existing_cli_default_and_is_frozen(self):
        from pavlusha_agent.cli import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            default = build_parser().parse_args([]).output_limit
            self.assertEqual(load_pack(tmp)._OUTPUT_LIMIT, default)
            for option in (['--output-limit', '4000'], ['--output-limit=4000'],
                           ['--output-limit', '9000', '--output-limit', '4000']):
                pack = load_pack(tmp, ['agent.py', '--workdir', tmp, *option])
                with patch.object(sys, 'argv', ['agent.py', '--output-limit', '9000']):
                    self.assertEqual(pack._OUTPUT_LIMIT, 4000)


@unittest.skipUnless(_HAVE_DOCX, 'requires optional DOCX fixture dependencies; run with bundled document Python')
class DocxPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack = load_pack(self.root)
        fixture(self.root / 'brief.docx')
        self.info = self.pack.docx_inspect('brief.docx')
        self.assertTrue(self.info['ok'], self.info)
        self.rev = self.info['revision']

    def test_cli_launch_only_workdir_inspect_read_edit_and_confinement(self):
        from pavlusha_agent.cli import main
        from tests.test_reasoning_window import init_turn, turn
        from tests.test_checkpoint_snapshots import done

        # Root is different from cwd; no document root environment is supplied.
        observed = []
        replies = iter([
            init_turn(),
            turn({'action': 'call_function', 'name': 'docx_inspect',
                  'arguments': {'path': 'brief.docx'}}),
            turn({'action': 'call_function', 'name': 'docx_read',
                  'arguments': {'path': 'brief.docx', 'revision': self.rev, 'contains': 'Launch date:'}}),
            turn({'action': 'call_function', 'name': 'docx_edit',
                  'arguments': {'path': 'brief.docx', 'revision': self.rev, 'output_path': 'changed.docx',
                                'edits': [{'node_id': self.target('Launch date:'), 'old': '15', 'new': '22'}]}}),
            turn({'action': 'call_function', 'name': 'docx_inspect',
                  'arguments': {'path': 'changed.docx'}}),
            turn({'action': 'call_function', 'name': 'docx_inspect',
                  'arguments': {'path': '../brief.docx'}}),
            done('DOCX calls'), turn({'action': 'finish', 'summary': 'verified'}),
        ])

        def worker(provider, messages, **kwargs):
            observed.extend(json.loads(m['content'].split('\n', 1)[1]) for m in messages
                            if m['content'].startswith('FUNCTION RESULT'))
            return next(replies)

        argv = ['agent.py', '--no-interactive', '--no-live', '--no-network', '--project-map', 'off',
                '--workdir', str(self.root), '--functions', 'tools/docx_functions.py',
                '--model', 'scripted', '--worker-context-budget', '40000', '--max-tokens', '1024',
                '--max-steps', '10', '--project-review-every', '0', 'Verify DOCX']
        # Default State is adjacent to workdir, as for the ordinary command.
        self.addCleanup(shutil.rmtree, str(self.root) + '.pavlusha-state', True)
        launch_environment = dict(os.environ)
        launch_environment.pop('PAVLUSHA_DOCX_ROOT', None)
        with patch.object(sys, 'argv', argv), \
             patch.dict(os.environ, launch_environment, clear=True), \
             patch('pavlusha_agent.provider.ChatProvider.worker_completion', worker), \
             patch('pavlusha_agent.runtime.run_shell', side_effect=AssertionError('unexpected shell')), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)
        results = [item['result'] for item in observed]
        self.assertTrue(any(r.get('revision') == self.rev for r in results))
        self.assertTrue(any(r.get('items') and r['items'][0].get('text') == 'Launch date: 15 November'
                            for r in results))
        self.assertTrue(any(r.get('path') == 'changed.docx' for r in results))
        self.assertTrue(any(r.get('error', {}).get('code') == 'path_escape' for r in results))
        self.assertEqual(Document(self.root / 'changed.docx').paragraphs[1].text, 'Launch date: 22 November')

    def read_all(self, path='brief.docx', revision=None, **kwargs):
        items, cursor = [], None
        for _ in range(200):
            result = self.pack.docx_read(path, revision or self.rev, cursor=cursor, **kwargs)
            self.assertTrue(result['ok'], result)
            if 'max_chars' in kwargs:
                self.assertLessEqual(len(self.pack._json(result)), kwargs['max_chars'])
                self.assertLessEqual(len(json.dumps({'name': 'docx_read', 'result': result},
                                                   ensure_ascii=False)), kwargs['max_chars'])
            items.extend(result['items'])
            cursor = result['next_cursor']
            if cursor is None:
                return items
        self.fail('cursor did not terminate')

    def target(self, contains):
        return self.read_all(contains=contains)[0]['id']

    def edit(self, edits, output='changed.docx', revision=None):
        return self.pack.docx_edit('brief.docx', output, revision or self.rev, edits)

    def error(self, result, code):
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error']['code'], code, result)

    def test_inspect_document_order_and_parts(self):
        items = self.info['items']
        kinds = [i['kind'] for i in items if i['part'] == 'word/document.xml']
        self.assertEqual(kinds[:5], ['paragraph', 'paragraph', 'paragraph', 'table', 'paragraph'])
        self.assertEqual(items[0]['preview'], 'Launch brief')
        self.assertEqual(self.info['table_count'], 1)
        self.assertEqual(self.info['section_count'], 1)
        self.assertTrue(any(p.startswith('word/header') for p in self.info['parts']))
        self.assertTrue(any(p.startswith('word/footer') for p in self.info['parts']))
        texts = [i['text'] for i in self.read_all()]
        self.assertIn('Launch: 15 November', texts)
        self.assertIn('Internal review copy', texts)
        self.assertIn('240 000', texts)

    def test_inspect_pagination_exact_order_and_invalid_cursor(self):
        items, cursor = [], None
        while True:
            result = self.pack.docx_inspect('brief.docx', cursor=cursor, limit=2)
            self.assertTrue(result['ok'], result)
            self.assertLessEqual(len(result['items']), 2)
            items.extend(result['items'])
            cursor = result['next_cursor']
            if cursor is None:
                break
        self.assertEqual(items, self.info['items'])
        self.error(self.pack.docx_inspect('brief.docx', cursor='garbage'), 'invalid_cursor')

    def test_literal_search_cross_runs_case_and_context(self):
        results = self.read_all(contains='15 November')
        self.assertEqual(len(results), 3)
        launch = next(i for i in results if i['text'].startswith('Launch date:'))
        self.assertEqual(launch['text'], 'Launch date: 15 November')
        self.assertNotIn('runs', launch)  # Plain run boundaries carry no reading information.
        self.assertEqual(self.read_all(contains='15 november'), [])
        self.error(self.pack.docx_read('brief.docx', self.rev, node_ids=[], contains='x'), 'invalid_selector')

    def test_read_pagination_long_paragraph_and_many_runs(self):
        document = Document()
        p = document.add_paragraph()
        text = ''.join(str(i % 10) for i in range(3000))
        for char in text:
            p.add_run(char)
        document.save(self.root / 'long.docx')
        rev = self.pack.docx_inspect('long.docx')['revision']
        result = self.read_all('long.docx', rev, max_chars=1800)
        self.assertEqual(''.join(i['text'] for i in result), text)
        self.assertEqual([i['text_offset'] for i in result], sorted(i['text_offset'] for i in result))

    def test_read_transport_budget_regression_and_lossless_continuation(self):
        document = Document()
        p = document.add_paragraph()
        text = 'я"\\\n' * 600
        for index in range(600):
            p.add_run('я"\\\n').bold = bool(index % 2)
        document.save(self.root / 'transport.docx')
        revision = self.pack.docx_inspect('transport.docx')['revision']
        with patch.object(sys, 'argv', ['agent.py', '--workdir', str(self.root), '--output-limit', '12000']):
            registry = FunctionRegistry([PACK])
        globals_ = registry.functions['docx_read'][0].__globals__
        action = {'action': 'call_function', 'name': 'docx_read',
                  'arguments': {'path': 'transport.docx', 'revision': revision, 'max_chars': 12000}}
        # Reproduce the old compact-JSON admission rule with the actual registry.
        # Many run records make spaced transport JSON exceed the local budget.
        with patch.dict(globals_, {'_read_size': lambda r: len(self.pack._json(r))}):
            old = registry.call(action, 12000)
        self.assertEqual(old['error'], 'output_limit_exceeded')
        for runtime_limit, max_chars in ((12000, 12000), (4000, 12000), (4000, 1800), (24000, 8000)):
            with self.subTest(runtime_limit=runtime_limit, max_chars=max_chars), \
                 patch.object(sys, 'argv', ['agent.py', '--workdir', str(self.root),
                                          '--output-limit', str(runtime_limit)]):
                registry = FunctionRegistry([PACK])
                arguments = {'path': 'transport.docx', 'revision': revision, 'max_chars': max_chars}
                parts = []
                for _ in range(200):
                    reply = registry.call({'action': 'call_function', 'name': 'docx_read',
                                           'arguments': arguments}, runtime_limit)
                    self.assertNotIn('error', reply, reply)
                    result = reply['result']
                    self.assertTrue(result['ok'], result)
                    self.assertLessEqual(len(json.dumps(reply, ensure_ascii=False)), min(runtime_limit, max_chars))
                    parts.extend(result['items'])
                    # Identical calls/cursors produce identical portions.
                    self.assertEqual(reply, registry.call({'action': 'call_function', 'name': 'docx_read',
                                                          'arguments': arguments}, runtime_limit))
                    if result['next_cursor'] is None:
                        break
                    arguments['cursor'] = result['next_cursor']
                else:
                    self.fail('transport pagination did not terminate')
                self.assertGreater(len(parts), 1)
                self.assertEqual(''.join(i['text'] for i in parts), text)
                self.assertFalse(any(i.get('math_count') for i in parts))

    def test_read_empty_selection_and_unfittable_metadata_are_admitted(self):
        with patch.object(sys, 'argv', ['agent.py', '--workdir', str(self.root), '--output-limit', '1000']):
            registry = FunctionRegistry([PACK])
        def call(**options):
            reply = registry.call({'action': 'call_function', 'name': 'docx_read',
                                   'arguments': {'path': 'brief.docx', 'revision': self.rev, **options}}, 1000)
            self.assertNotIn('error', reply, reply)
            self.assertLessEqual(len(json.dumps(reply, ensure_ascii=False)), 1000)
            return reply['result']
        empty = call(node_ids=[])
        self.assertTrue(empty['ok'], empty)
        self.assertEqual(empty['items'], [])
        self.assertIsNone(empty['next_cursor'])
        # An exceptional style name cannot fit, even if max_chars is large.
        document = Document()
        style = document.styles.add_style('Long style ' * 200, 1)
        document.add_paragraph('Text', style=style)
        document.save(self.root / 'metadata.docx')
        revision = self.pack.docx_inspect('metadata.docx')['revision']
        small = call(path='metadata.docx', revision=revision, max_chars=12000)
        self.assertFalse(small['ok'], small)
        self.assertEqual(small['error']['code'], 'limit_too_small')

    def test_read_selector_table_and_unknown_node(self):
        # Table coordinates stay available in the compact reading view.
        table = next(i for i in self.info['items'] if i['kind'] == 'table')
        cells = self.read_all(node_ids=[table['id']])
        self.assertEqual(len(cells), 8)
        self.assertEqual(cells[-1]['text'], '240 000')
        self.assertEqual(cells[-1]['location']['column'], 1)
        self.error(self.pack.docx_read('brief.docx', self.rev, node_ids=['missing']), 'unknown_node')
        self.error(self.edit([{'node_id': table['id'], 'old': 'Item', 'new': 'Other'}]), 'unknown_node')

    def test_read_compact_emphasis_keeps_exact_edit_validation(self):
        from docx.shared import Pt
        document = Document()
        p = document.add_paragraph()
        for value, size in [('A', 10), ('B', 12)]:
            run = p.add_run(value)
            run.bold = True
            run.font.size = Pt(size)
        document.add_heading('Heading', level=1)
        document.add_paragraph()
        document.save(self.root / 'compact.docx')
        info = self.pack.docx_inspect('compact.docx')
        items = self.read_all('compact.docx', info['revision'])
        self.assertEqual(items[0], {'id': 'word/document.xml:p1', 'text': 'AB',
                                  'runs': [{'start': 0, 'end': 2, 'bold': True}]})
        self.assertEqual(items[1]['style_name'], 'heading 1')
        self.assertEqual(items[2], {'id': 'word/document.xml:p3', 'text': ''})
        self.assertEqual(info['items'][0]['kind'], 'paragraph')
        self.assertEqual(info['items'][0]['part'], 'word/document.xml')
        self.assertTrue(info['items'][0]['editable'])
        self.error(self.pack.docx_edit('compact.docx', 'changed.docx', info['revision'],
                   [{'node_id': items[0]['id'], 'old': 'AB', 'new': 'C'}]), 'format_conflict')

    def test_stale_revision_and_query_bound_cursor(self):
        first = self.pack.docx_read('brief.docx', self.rev, max_chars=1800)
        self.assertIsNotNone(first['next_cursor'])
        self.error(self.pack.docx_read('brief.docx', self.rev, contains='Launch', cursor=first['next_cursor']), 'invalid_cursor')
        document = Document(self.root / 'brief.docx')
        document.add_paragraph('New paragraph')
        document.save(self.root / 'brief.docx')
        self.error(self.pack.docx_read('brief.docx', self.rev), 'stale_revision')
        self.error(self.edit([{'node_id': 'word/document.xml:p2', 'old': '15', 'new': '22'}]), 'stale_revision')
        self.error(self.pack.docx_render('brief.docx', self.rev, 'pages'), 'stale_revision')
        self.error(self.pack.docx_inspect('brief.docx', cursor=self.info['next_cursor'] or 'bad'), 'invalid_cursor')

    def test_merged_and_skipped_cells_are_physical_not_duplicated(self):
        document = Document()
        table = document.add_table(rows=2, cols=3)
        table.cell(0, 0).text = 'merged'
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(1, 1).text = 'surviving'
        row = table.rows[1]._tr
        row.remove(row.findall(qn('w:tc'))[0])
        before = OxmlElement('w:gridBefore')
        before.set(qn('w:val'), '1')
        row.get_or_add_trPr().append(before)
        document.save(self.root / 'grid.docx')
        info = self.pack.docx_inspect('grid.docx')
        grid = info['items'][0]
        self.assertTrue(grid['merged_cells'])
        self.assertTrue(grid['skipped_cells'])
        cells = self.read_all('grid.docx', info['revision'])
        merged = [i for i in cells if i['text'] == 'merged']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['location']['column_span'], 2)
        surviving = next(i for i in cells if i['text'] == 'surviving')
        self.assertEqual(surviving['location']['column'], 1)

    def test_success_cross_run_original_and_untouched_package_preserved(self):
        original = (self.root / 'brief.docx').read_bytes()
        pid = self.target('Launch date:')
        result = self.edit([{'node_id': pid, 'old': '15 November', 'new': '22 November'}])
        self.assertTrue(result['ok'], result)
        self.assertNotEqual(result['revision'], self.rev)
        self.assertEqual((self.root / 'brief.docx').read_bytes(), original)
        items = self.read_all('changed.docx', result['revision'], contains='Launch date:')
        self.assertEqual(items[0]['text'], 'Launch date: 22 November')
        with ZipFile(self.root / 'brief.docx') as old, ZipFile(self.root / 'changed.docx') as new:
            self.assertEqual(old.namelist(), new.namelist())
            for name in old.namelist():
                if name != 'word/document.xml':
                    self.assertEqual(old.read(name), new.read(name), name)
            self.assertTrue(any(n.startswith('word/media/') for n in new.namelist()))
            self.assertEqual(old.read('word/_rels/document.xml.rels'), new.read('word/_rels/document.xml.rels'))
        document = Document(self.root / 'changed.docx')
        p = document.paragraphs[1]
        self.assertEqual(len(p.runs), 3)
        self.assertEqual([r.bold for r in p.runs], [None, None, None])

    def test_header_table_and_footer_edits_use_same_contract(self):
        edits = [{'node_id': self.target('Launch: 15 November'), 'old': '15 November', 'new': '22 November'},
                 {'node_id': self.target('240 000'), 'old': '240 000', 'new': '250 000'},
                 {'node_id': self.target('Internal review copy'), 'old': 'review', 'new': 'release'}]
        result = self.edit(edits)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['changed_parts']), 3)
        texts = [i['text'] for i in self.read_all('changed.docx', result['revision'])]
        self.assertIn('Launch: 22 November', texts)
        self.assertIn('250 000', texts)
        self.assertIn('Internal release copy', texts)

    def test_format_conflict_and_rich_format_preservation(self):
        document = Document()
        p = document.add_paragraph()
        p.add_run('15 ').bold = True
        p.add_run('November').italic = True
        document.save(self.root / 'format.docx')
        info = self.pack.docx_inspect('format.docx')
        pid = info['items'][0]['id']
        self.error(self.pack.docx_edit('format.docx', 'bad.docx', info['revision'],
                   [{'node_id': pid, 'old': '15 November', 'new': '22 November'}]), 'format_conflict')
        self.assertFalse((self.root / 'bad.docx').exists())
        result = self.pack.docx_edit('format.docx', 'good.docx', info['revision'], [{'node_id': pid, 'old': '15', 'new': '22'}])
        self.assertTrue(result['ok'], result)
        p = Document(self.root / 'good.docx').paragraphs[0]
        self.assertTrue(p.runs[0].bold)
        self.assertTrue(p.runs[1].italic)
        self.assertEqual(p.text, '22 November')

    def test_ambiguous_not_found_invalid_batch_and_atomicity(self):
        pid = self.target('Launch date:')
        for old, code in [('missing', 'text_not_found'), ('e', 'ambiguous_match')]:
            self.error(self.edit([{'node_id': pid, 'old': old, 'new': 'x'}]), code)
            self.assertFalse((self.root / 'changed.docx').exists())
        self.error(self.edit([{'node_id': pid, 'old': '15 November', 'new': '22 November'},
                             {'node_id': self.target('After table.'), 'old': 'absent', 'new': 'x'}]), 'text_not_found')
        self.assertFalse((self.root / 'changed.docx').exists())
        self.assertEqual(self.read_all(contains='Launch date:')[0]['text'], 'Launch date: 15 November')
        self.error(self.edit([{'node_id': pid, 'old': '15', 'new': '22'}] * 2), 'invalid_edit')
        self.error(self.edit([{'node_id': pid, 'old': '', 'new': 'x'}]), 'invalid_edit')
        self.error(self.edit([{'node_id': pid, 'old': '15', 'new': '\x00'}]), 'invalid_edit')

    def test_destination_exists_and_publication_failure_leave_no_temp(self):
        (self.root / 'changed.docx').write_bytes(b'keep')
        edits = [{'node_id': self.target('Launch date:'), 'old': '15', 'new': '22'}]
        self.error(self.edit(edits), 'destination_exists')
        self.assertEqual((self.root / 'changed.docx').read_bytes(), b'keep')
        with patch.object(self.pack.os, 'link', side_effect=OSError('publication failed')):
            self.error(self.edit(edits, 'failed.docx'), 'io_error')
        self.assertFalse((self.root / 'failed.docx').exists())
        self.assertEqual(list(self.root.glob('.docx-edit-*')), [])

    def test_path_traversal_absolute_and_symlink_escape(self):
        outside = Path(self.tmp.name).parent / (Path(self.tmp.name).name + '-outside')
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        for path in ('../brief.docx', str(self.root / 'brief.docx'), 'escape/document.docx'):
            self.error(self.pack.docx_inspect(path), 'path_escape')
            self.error(self.pack.docx_read(path, self.rev), 'path_escape')
        edits = [{'node_id': self.target('Launch date:'), 'old': '15', 'new': '22'}]
        for output in ('../out.docx', str(self.root / 'out.docx'), 'escape/out.docx'):
            self.error(self.edit(edits, output), 'path_escape')
            self.error(self.pack.docx_render('brief.docx', self.rev, output), 'path_escape')
        with patch.dict(os.environ, {'PAVLUSHA_DOCX_ROOT': str(outside)}):
            self.assertTrue(self.pack.docx_inspect('brief.docx')['ok'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupt_invalid_xml_dtd_and_package_limits(self):
        (self.root / 'broken.docx').write_bytes(b'not zip')
        self.error(self.pack.docx_inspect('broken.docx'), 'invalid_docx')
        for data in (b'<bad>', b'<!DOCTYPE x><x/>', b'<x/>'):
            with ZipFile(self.root / 'brief.docx') as old, ZipFile(self.root / 'xml.docx', 'w') as new:
                for name in old.namelist():
                    new.writestr(name, data if name == 'word/document.xml' else old.read(name))
            self.error(self.pack.docx_inspect('xml.docx'), 'invalid_docx')
        self.error(self.pack.docx_inspect('absent.docx'), 'file_not_found')

    def test_unsupported_construct_is_reported_and_edit_refused(self):
        document = Document()
        p = document.add_paragraph('Text ')
        hyperlink = OxmlElement('w:hyperlink')
        run, text = OxmlElement('w:r'), OxmlElement('w:t')
        text.text = 'link'
        run.append(text)
        hyperlink.append(run)
        p._p.append(hyperlink)
        field = OxmlElement('w:fldSimple')
        field.set(qn('w:instr'), 'DATE')
        p._p.append(field)
        document.save(self.root / 'complex.docx')
        info = self.pack.docx_inspect('complex.docx')
        self.assertTrue(info['coverage']['partial'])
        self.assertGreater(info['coverage']['unsupported']['fldSimple'], 0)
        self.error(self.pack.docx_edit('complex.docx', 'unsafe.docx', info['revision'],
                   [{'node_id': info['items'][0]['id'], 'old': 'Text', 'new': 'New'}]), 'unsupported_target')

    def math_document(self, expressions, table=False):
        from lxml import etree
        document = Document()
        if table:
            p = document.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
        else:
            p = document.add_paragraph()
        p.add_run('Before ')
        for xml in expressions:
            p._p.append(etree.fromstring(xml))
        p.add_run(' After').bold = True
        document.add_paragraph('Ordinary text')
        document.save(self.root / 'math.docx')
        return self.pack.docx_inspect('math.docx')

    def math_xml(self, content):
        return ('<m:oMath xmlns:m="' + self.pack._M + '">' + content + '</m:oMath>').encode()

    def test_omml_real_variant8_table_search_offsets_and_edit_protection(self):
        # Exact OMML subtree from the real laboratory, not a synthetic radical.
        xml = (PACK.parent.parent / 'tests/fixtures/docx_omml_variant8.xml').read_bytes()
        info = self.math_document([xml], table=True)
        coverage = info['coverage']['omml']
        self.assertEqual(coverage['formula_count'], 1)
        self.assertEqual(coverage['represented_count'], 1)
        self.assertEqual(coverage['partial_count'], 0)
        self.assertFalse(info['coverage']['partial'])
        items = self.read_all('math.docx', info['revision'], node_ids=[info['items'][0]['id']])
        item = items[0]
        formula = 'y=sqrt(3(x)^(2)−9x+6)'
        self.assertEqual(item['text'], 'Before ' + formula + ' After')
        self.assertEqual(item['id'], 'word/document.xml:p1')
        self.assertFalse(item['editable'])
        self.assertEqual(item['math_count'], 1)
        self.assertFalse(item['math_partial'])
        self.assertEqual(item['runs'][-1]['start'], len('Before ' + formula))
        self.assertTrue(item['runs'][-1]['bold'])
        found = self.read_all('math.docx', info['revision'], contains=formula)
        self.assertEqual(found[0]['id'], item['id'])
        self.error(self.pack.docx_edit('math.docx', 'changed.docx', info['revision'],
                   [{'node_id': item['id'], 'old': 'Before', 'new': 'New'}]), 'unsupported_target')

    def test_omml_display_delimiters_and_indexed_root(self):
        xml = self.math_xml('<m:d><m:dPr><m:begChr m:val="["/><m:endChr m:val="]"/></m:dPr>'
                            '<m:e><m:r><m:t>x+1</m:t></m:r></m:e></m:d>'
                            '<m:rad><m:deg><m:r><m:t>3</m:t></m:r></m:deg>'
                            '<m:e><m:r><m:t>x</m:t></m:r></m:e></m:rad>')
        wrapper = b'<m:oMathPara xmlns:m="' + self.pack._M.encode() + b'"><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>' + xml + b'</m:oMathPara>'
        info = self.math_document([wrapper])
        self.assertIn('[x+1]root(3, x)', info['items'][0]['preview'])
        self.assertEqual(info['coverage']['omml']['represented_count'], 1)
        self.assertFalse(info['coverage']['partial'])

    def test_omml_unknown_and_missing_content_never_silently_flatten(self):
        cases = [('<m:f><m:num><m:r><m:t>1</m:t></m:r></m:num>'
                  '<m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>', 'unsupported:f'),
                 ('<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/><m:e/></m:rad>', 'missing_or_empty:rad/e'),
                 ('<m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e></m:sSup>', 'missing_or_empty:sSup/sup'),
                 ('<m:rad><m:radPr><m:degHide/></m:radPr><m:deg/><m:e><m:r><m:t/></m:r></m:e></m:rad>', 'missing_or_empty:rad/e'),
                 ('<m:r><m:rPr><m:scr m:val="double-struck"/></m:rPr><m:t>R</m:t></m:r>', 'unsupported:scr'),
                 ('<m:r><m:t/></m:r>', 'empty:formula')]
        for content, warning in cases:
            with self.subTest(warning=warning):
                info = self.math_document([self.math_xml(content)])
                self.assertTrue(info['coverage']['partial'])
                self.assertEqual(info['coverage']['omml']['partial_count'], 1)
                self.assertIn(warning, info['coverage']['omml']['warnings'])
                item = self.read_all('math.docx', info['revision'])[0]
                self.assertTrue(item['math_partial'])
                self.assertIn('[OMML ', item['text'])
                if warning == 'unsupported:f':
                    self.assertIn('1', item['text'])
                    self.assertIn('2', item['text'])

    def test_omml_long_formula_pagination_is_lossless(self):
        content = '<m:r><m:t>' + 'x+' * 4000 + '1</m:t></m:r>'
        info = self.math_document([self.math_xml(content)])
        items = self.read_all('math.docx', info['revision'], node_ids=[info['items'][0]['id']], max_chars=1800)
        self.assertGreater(len(items), 1)
        self.assertEqual(''.join(i['text'] for i in items), 'Before ' + 'x+' * 4000 + '1 After')

    def test_omml_excluded_textbox_reported_in_coverage(self):
        xml = self.math_xml('<m:r><m:t>x</m:t></m:r>')
        info = self.math_document([xml])
        document = Document(self.root / 'math.docx')
        box = OxmlElement('w:txbxContent')
        p = OxmlElement('w:p')
        p.append(self.pack._ET.fromstring(xml))
        box.append(p)
        document.paragraphs[0]._p.append(box)
        document.save(self.root / 'math.docx')
        info = self.pack.docx_inspect('math.docx')
        self.assertEqual(info['coverage']['omml']['formula_count'], 2)
        self.assertEqual(info['coverage']['omml']['omitted_count'], 1)
        self.assertEqual(info['coverage']['omml']['represented_count'], 1)
        self.assertTrue(info['coverage']['partial'])

    def test_omml_survives_ordinary_edit_with_stable_ids_and_revision_checks(self):
        xml = (PACK.parent.parent / 'tests/fixtures/docx_omml_variant8.xml').read_bytes()
        info = self.math_document([xml])
        source = (self.root / 'math.docx').read_bytes()
        receipt = self.pack.docx_edit('math.docx', 'changed.docx', info['revision'],
                    [{'node_id': 'word/document.xml:p2', 'old': 'Ordinary', 'new': 'Revised'}])
        self.assertTrue(receipt['ok'], receipt)
        new_info = self.pack.docx_inspect('changed.docx')
        self.assertNotEqual(new_info['revision'], info['revision'])
        old = self.read_all('math.docx', info['revision'])[0]
        new = self.read_all('changed.docx', new_info['revision'])[0]
        self.assertEqual(old, new)
        self.assertEqual((self.root / 'math.docx').read_bytes(), source)
        self.error(self.pack.docx_read('changed.docx', info['revision']), 'stale_revision')
        with ZipFile(self.root / 'math.docx') as a, ZipFile(self.root / 'changed.docx') as b:
            def equation(z):
                root = self.pack._ET.fromstring(z.read('word/document.xml'))
                return self.pack._ET.tostring(next(root.iter(self.pack._m('oMath'))), method='c14n')
            self.assertEqual(equation(a), equation(b))

    def test_renderer_missing_failure_and_timeout_are_observations(self):
        self.pack._RENDERER = None
        with patch.object(self.pack.shutil, 'which', return_value=None):
            self.error(self.pack.docx_render('brief.docx', self.rev, 'pages'), 'dependency_missing')
        fake = self.root / 'renderer.py'
        fake.write_text('raise SystemExit(3)\n')
        self.pack._RENDERER = str(fake)
        self.pack._PYTHON = sys.executable
        self.error(self.pack.docx_render('brief.docx', self.rev, 'pages'), 'render_failed')
        self.assertFalse((self.root / 'pages').exists())
        fake.write_text('import time\ntime.sleep(30)\n')
        self.error(self.pack.docx_render('brief.docx', self.rev, 'pages', timeout_seconds=1), 'render_timeout')
        self.assertFalse((self.root / 'pages').exists())
        self.assertEqual(list(self.root.glob('.docx-render-*')), [])

    def test_default_renderer_missing_tools_and_invalid_override_are_bounded(self):
        self.pack._RENDERER = None
        for missing in ('soffice', 'pdfinfo', 'pdftoppm'):
            with self.subTest(missing=missing), patch.object(self.pack.shutil, 'which',
                    side_effect=lambda name: None if name == missing else '/available/' + name):
                result = self.pack.docx_render('brief.docx', self.rev, 'pages')
                self.error(result, 'dependency_missing')
                self.assertIn(missing, result['error']['message'])
                self.assertLess(len(json.dumps(result)), 500)
                self.assertFalse((self.root / 'pages').exists())
        self.pack._RENDERER = '/missing/explicit-renderer.py'
        self.error(self.pack.docx_render('brief.docx', self.rev, 'pages'), 'dependency_missing')

    def test_default_renderer_checks_pdf_page_count_and_cleans_failure(self):
        self.pack._RENDERER = None
        def converter(command, stage, deadline, log_name='render.log', env=None):
            if command[0].endswith('soffice'):
                (stage / 'source.pdf').write_bytes(b'%PDF-1.4\nfixture')
            elif command[0].endswith('pdfinfo'):
                (stage / 'pdfinfo.log').write_text('Pages: 201\n')
            else:
                self.fail('Oversized PDF must be refused before rasterization')
        with patch.object(self.pack.shutil, 'which', side_effect=lambda name: '/available/' + name), \
             patch.object(self.pack, '_render_command', side_effect=converter):
            self.error(self.pack.docx_render('brief.docx', self.rev, 'pages'), 'render_failed')
        self.assertFalse((self.root / 'pages').exists())
        self.assertEqual(list(self.root.glob('.docx-render-*')), [])

    @unittest.skipUnless(os.getenv('PAVLUSHA_TEST_REAL_RENDER') == '1'
                         and all(shutil.which(n) for n in ('soffice', 'pdfinfo', 'pdftoppm')),
                         'opt-in real standard LibreOffice/Poppler render')
    def test_real_default_renderer_without_env_through_public_function_registry(self):
        document = Document(self.root / 'brief.docx')
        for i in range(10):
            document.add_page_break()
            document.add_paragraph(f'Page {i + 2}')
        document.save(self.root / 'brief.docx')
        original = (self.root / 'brief.docx').read_bytes()
        with patch.dict(os.environ), patch.object(sys, 'argv',
                ['agent.py', '--workdir', str(self.root), '--functions', str(PACK), '--output-limit', '12000']):
            os.environ.pop('PAVLUSHA_DOCX_RENDERER', None)
            os.environ.pop('PAVLUSHA_DOCX_PYTHON', None)
            registry = FunctionRegistry([PACK])
        self.assertIsNone(registry.functions['docx_render'][0].__globals__['_RENDERER'])
        def call(name, **arguments):
            reply = registry.call({'action': 'call_function', 'name': name, 'arguments': arguments}, 12000)
            self.assertNotIn('error', reply, reply)
            self.assertTrue(reply['result']['ok'], reply)
            return reply['result']
        info = call('docx_inspect', path='brief.docx')
        result = call('docx_render', path='brief.docx', revision=info['revision'], output_dir='pages', dpi=72)
        manifest = json.loads((self.root / result['manifest_path']).read_text())
        self.assertEqual(manifest['renderer'], 'LibreOffice + Poppler')
        self.assertGreaterEqual(manifest['page_count'], 11)
        self.assertEqual(manifest['pages'], [f'page-{i}.png' for i in range(1, manifest['page_count'] + 1)])
        self.assertTrue((self.root / result['pdf_path']).read_bytes().startswith(b'%PDF-'))
        for page in manifest['pages']:
            with Image.open(self.root / 'pages' / page) as image:
                image.verify()
        self.assertEqual(original, (self.root / 'brief.docx').read_bytes())
        self.assertEqual(result['visual_verification'], 'not_performed')
        self.assertFalse((self.root / 'pages' / 'lo-profile').exists())

    def test_vertical_merge_nested_table_and_header_table(self):
        document = Document()
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = 'vertical'
        table.cell(0, 0).merge(table.cell(1, 0))
        table.cell(0, 1).add_table(rows=1, cols=1).cell(0, 0).text = 'nested'
        document.sections[0].header.add_table(rows=1, cols=1, width=document.sections[0].page_width).cell(0, 0).text = 'header table'
        document.save(self.root / 'nested.docx')
        info = self.pack.docx_inspect('nested.docx')
        items = self.read_all('nested.docx', info['revision'])
        self.assertEqual(sum(i['text'] == 'vertical' for i in items), 1)
        continuation = [i for i in items if i.get('location', {}).get('vertical_merge') == 'continue']
        self.assertTrue(continuation)
        self.assertTrue(any(i['text'] == 'nested' and i['location']['table_id'].endswith(':t2') for i in items))
        self.assertTrue(any(i['text'] == 'header table' and i['id'].startswith('word/header') for i in items))

    def test_configuration_dependency_and_result_budgets(self):
        with patch.object(self.pack, '_ROOT', self.root / 'missing-root'):
            self.error(self.pack.docx_inspect('brief.docx'), 'configuration_error')
        with patch.object(self.pack, '_ET', None):
            self.error(self.pack.docx_read('brief.docx', self.rev), 'dependency_missing')
        self.error(self.pack.docx_inspect('bad\x00path'), 'invalid_path')
        self.error(self.pack.docx_read('brief.docx', self.rev, max_chars=1700), 'limit_too_small')
        self.error(self.pack.docx_inspect('brief.docx', limit=41), 'invalid_limit')
        edit = [{'node_id': self.target('Launch date:'), 'old': '15', 'new': '22'}]
        with patch.object(self.pack, '_MAX_JSON', 100):
            self.error(self.edit(edit), 'result_limit_exceeded')
        self.assertFalse((self.root / 'changed.docx').exists())

    def test_atomic_no_clobber_directory_publication(self):
        stage = self.root / 'stage'
        output = self.root / 'already-created'
        stage.mkdir()
        (stage / 'complete').write_text('contents')
        output.mkdir()
        with self.assertRaises(self.pack._Error) as caught:
            self.pack._publish_directory(stage, output)
        self.assertEqual(caught.exception.code, 'destination_exists')
        self.assertTrue((stage / 'complete').exists())
        self.assertEqual(list(output.iterdir()), [])

    @unittest.skipUnless(RENDERER.is_file() and os.getenv('PAVLUSHA_TEST_REAL_RENDER') == '1', 'opt-in real LibreOffice render')
    def test_real_render_manifest_all_pages_and_original_unchanged(self):
        original = (self.root / 'brief.docx').read_bytes()
        self.pack._RENDERER = str(RENDERER)
        self.pack._PYTHON = sys.executable
        result = self.pack.docx_render('brief.docx', self.rev, 'pages', dpi=72)
        self.assertTrue(result['ok'], result)
        manifest = json.loads((self.root / result['manifest_path']).read_text())
        self.assertEqual(manifest['source_revision'], self.rev)
        self.assertEqual(manifest['page_count'], result['page_count'])
        for page in manifest['pages']:
            with Image.open(self.root / 'pages' / page) as image:
                image.verify()
        self.assertEqual((self.root / 'brief.docx').read_bytes(), original)
        self.assertEqual(result['visual_verification'], 'not_performed')
        self.error(self.pack.docx_render('brief.docx', self.rev, 'pages'), 'destination_exists')


if __name__ == '__main__':
    unittest.main()
