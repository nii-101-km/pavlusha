"""Trusted DOCX capabilities confined to the existing CLI --workdir.

Only four public functions are exported. Rendering uses local LibreOffice/Poppler
or the explicit PAVLUSHA_DOCX_RENDERER/PAVLUSHA_DOCX_PYTHON override.
The existing CLI parser supplies the workdir; no runtime objects or semantic
decisions are used.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zipfile import BadZipFile, ZipFile

try:
    from lxml import etree as _ET
except ImportError:
    _ET = None

# Read the same CLI option/default as runtime, without a separate root setting
# or changing the cwd observed by other trusted custom-function modules.
from pavlusha_agent.cli import build_parser as _build_parser

_CLI_ARGS = _build_parser().parse_args()
_ROOT = Path(_CLI_ARGS.workdir).expanduser().resolve()
_OUTPUT_LIMIT = _CLI_ARGS.output_limit
_RENDERER = os.getenv('PAVLUSHA_DOCX_RENDERER')
_PYTHON = os.getenv('PAVLUSHA_DOCX_PYTHON', sys.executable)
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_NS = {'w': _W, 'r': _R}
_MAX_JSON = 12000
_MAX_FILE = 32 * 1024 * 1024
_MAX_EXPANDED = 128 * 1024 * 1024


class _Error(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise _Error(code, message)


def _w(local):
    return '{' + _W + '}' + local


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _read_size(result):
    # Match FunctionRegistry/runtime's spaced serialization, conservatively
    # including the outer envelope (Core admission itself checks only result).
    return len(json.dumps({'name': 'docx_read', 'result': result},
                          ensure_ascii=False, allow_nan=False))


def _run(function, *args):
    try:
        result = function(*args)
        if len(_json(result)) > _MAX_JSON:
            _fail('result_limit_exceeded', 'Request a smaller result batch.')
        return result
    except _Error as exc:
        return {'ok': False, 'error': {'code': exc.code, 'message': str(exc)[:1000]}}
    except (OSError, BadZipFile) as exc:
        return {'ok': False, 'error': {'code': 'io_error', 'message': str(exc)[:1000]}}


def _path(value):
    if not _ROOT.is_dir():
        _fail('configuration_error', 'The runtime workdir is not an existing document directory.')
    if not isinstance(value, str) or not value or len(value) > 512 or '\x00' in value:
        _fail('invalid_path', 'Use a nonempty relative document path, at most 512 characters.')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        _fail('path_escape', 'Only paths relative to the runtime workdir are accepted.')
    try:
        resolved = (_ROOT / path).resolve()
    except (OSError, RuntimeError, ValueError):
        _fail('invalid_path', 'Path cannot be resolved safely.')
    if not resolved.is_relative_to(_ROOT) or resolved == _ROOT:
        _fail('path_escape', 'Path resolves outside the document root or selects the root itself.')
    return resolved


def _relative(path):
    return path.relative_to(_ROOT).as_posix()


def _xml(data):
    if b'<!DOCTYPE' in data.upper():
        _fail('invalid_docx', 'DTD declarations are not supported.')
    try:
        return _ET.fromstring(data, parser=_ET.XMLParser(resolve_entities=False, no_network=True))
    except _ET.XMLSyntaxError as exc:
        _fail('invalid_docx', 'Malformed XML: ' + str(exc)[:400])


def _number(element, default):
    try:
        value = int(element.get(_w('val'), str(default))) if element is not None else default
        if value < 0:
            raise ValueError()
        return value
    except ValueError:
        _fail('invalid_docx', 'Invalid numeric table grid property.')


def _open(path, revision=None):
    source = _path(path)
    if not source.is_file():
        _fail('file_not_found', 'Document does not exist.')
    if source.stat().st_size > _MAX_FILE:
        _fail('document_limit_exceeded', 'DOCX exceeds the 32 MiB file limit.')
    with source.open('rb') as stream:
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        _fail('document_limit_exceeded', 'DOCX exceeds the 32 MiB file limit.')
    digest = hashlib.sha256(data).hexdigest()
    if revision is not None and digest != revision:
        _fail('stale_revision', 'Document bytes changed; inspect/read the current revision first.')
    if _ET is None:
        _fail('dependency_missing', 'lxml is required; dependencies are not installed automatically.')
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(infos) > 2048 or len(set(names)) != len(names) or sum(i.file_size for i in infos) > _MAX_EXPANDED:
                _fail('document_limit_exceeded', 'Too many parts, duplicate ZIP names, or expanded size above 128 MiB.')
            if any(i.flag_bits & 1 for i in infos):
                _fail('invalid_docx', 'Encrypted ZIP parts are not supported.')
            members = {i.filename: archive.read(i) for i in infos}
            comment = archive.comment
    except (BadZipFile, RuntimeError, EOFError) as exc:
        _fail('invalid_docx', 'Not a readable DOCX ZIP: ' + str(exc)[:300])
    if not {'[Content_Types].xml', '_rels/.rels', 'word/document.xml'} <= members.keys():
        _fail('invalid_docx', 'Missing required DOCX package parts.')
    _xml(members['[Content_Types].xml'])
    package_rels = _xml(members['_rels/.rels'])
    if not any(e.get('Type', '').endswith('/officeDocument') and e.get('Target', '').lstrip('/') == 'word/document.xml'
               for e in package_rels):
        _fail('invalid_docx', 'Expected a standard word/document.xml officeDocument relationship.')
    document = _xml(members['word/document.xml'])
    if document.tag != _w('document') or document.find(_w('body')) is None:
        _fail('invalid_docx', 'Missing WordprocessingML document/body.')
    styles = {}
    if 'word/styles.xml' in members:
        for style in _xml(members['word/styles.xml']).findall(_w('style')):
            name = style.find(_w('name'))
            styles[style.get(_w('styleId'))] = name.get(_w('val')) if name is not None else ''
    # Header/footer order follows document section references; linked parts appear once.
    parts = {'word/document.xml': document}
    rels = {}
    if 'word/_rels/document.xml.rels' in members:
        rels = {e.get('Id'): e for e in _xml(members['word/_rels/document.xml.rels'])}
    for ref in document.xpath('.//w:headerReference | .//w:footerReference', namespaces=_NS):
        rel = rels.get(ref.get('{' + _R + '}id'))
        if rel is None or rel.get('TargetMode') == 'External':
            _fail('invalid_docx', 'Invalid header/footer relationship.')
        target = rel.get('Target', '')
        # Normalize OPC targets without reading any host filesystem path.
        import posixpath
        name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'word/' + target)
        if not name.startswith('word/') or name not in members:
            _fail('invalid_docx', 'Header/footer part is missing or has an unsupported target.')
        if name not in parts:
            parts[name] = _xml(members[name])
            expected = _w('hdr') if ref.tag == _w('headerReference') else _w('ftr')
            if parts[name].tag != expected:
                _fail('invalid_docx', 'Header/footer has an invalid XML root.')
    doc = {'data': data, 'revision': digest, 'members': members, 'infos': infos, 'comment': comment,
           'parts': parts, 'styles': styles, 'nodes': {}, 'paragraphs': [], 'blocks': []}
    unsupported = {tag: sum(len(root.findall('.//' + _w(tag))) for root in parts.values())
                   for tag in ('ins', 'del', 'moveFrom', 'moveTo', 'pPrChange', 'rPrChange',
                               'txbxContent', 'fldSimple', 'fldChar', 'sdt', 'drawing', 'hyperlink')}
    unsupported.update({name: name in members for name in ('word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml')})
    unsupported['digital_signatures'] = any(n.startswith('_xmlsignatures/') for n in members)
    doc['coverage'] = {'scope': 'body, physical table cells, referenced headers/footers',
                       'unsupported': unsupported,
                       'partial': any(v for k, v in unsupported.items() if k != 'drawing')}
    for part_name, root in parts.items():
        paras, tables = list(root.iter(_w('p'))), list(root.iter(_w('tbl')))
        if len(paras) + len(tables) > 50000:
            _fail('document_limit_exceeded', 'Too many addressable elements.')
        pids = {p: f'{part_name}:p{i + 1}' for i, p in enumerate(paras)}
        tids = {t: f'{part_name}:t{i + 1}' for i, t in enumerate(tables)}
        cells = {}
        table_info = {}
        for table, tid in tids.items():
            rows = table.findall(_w('tr'))
            grid = table.find(_w('tblGrid'))
            merged = skipped = False
            for row_index, row in enumerate(rows):
                before = row.find(_w('trPr') + '/' + _w('gridBefore'))
                after = row.find(_w('trPr') + '/' + _w('gridAfter'))
                col = _number(before, 0)
                skipped |= col > 0 or _number(after, 0) > 0
                for cell_index, cell in enumerate(row.findall(_w('tc'))):
                    span = cell.find(_w('tcPr') + '/' + _w('gridSpan'))
                    merge = cell.find(_w('tcPr') + '/' + _w('vMerge'))
                    width = _number(span, 1)
                    if width < 1 or col < 0:
                        _fail('invalid_docx', 'Invalid table grid coordinates.')
                    merged |= width > 1 or merge is not None
                    cells[cell] = {'table_id': tid, 'cell_id': f'{tid}:r{row_index + 1}:c{cell_index + 1}',
                                   'row': row_index, 'column': col, 'column_span': width,
                                   'vertical_merge': merge.get(_w('val'), 'continue') if merge is not None else None}
                    col += width
            table_info[table] = {'id': tid, 'kind': 'table', 'part': part_name, 'rows': len(rows),
                                 'grid_columns': len(grid) if grid is not None else None,
                                 'merged_cells': bool(merged), 'skipped_cells': bool(skipped)}
            doc['nodes'][tid] = table
        for p in paras:
            if any(a.tag == _w('txbxContent') for a in p.iterancestors()):
                continue  # Explicitly omitted, rather than mistaken for body text.
            pid = pids[p]
            doc['nodes'][pid] = p
            location = next((cells[a] for a in p.iterancestors() if a in cells), None)
            style = p.find(_w('pPr') + '/' + _w('pStyle'))
            style_id = style.get(_w('val')) if style is not None else 'Normal'
            record = {'id': pid, 'kind': 'paragraph', 'part': part_name, 'style_id': style_id,
                      'style_name': styles.get(style_id, style_id), 'location': location,
                      'editable': _editable(p),
                      'text': _text(p), 'element': p}
            math_segments = [s for s in _paragraph_segments(p) if s[3]]
            if math_segments:
                record['math_count'] = sum(s[3] for s in math_segments)
                record['math_partial'] = any(s[2] for s in math_segments)
            doc['paragraphs'].append(record)
        # Walk XML order, retaining top-level blocks; nested contents are read through tables.
        by_element = {r['element']: r for r in doc['paragraphs'] if r['part'] == part_name}
        for node in root.iter():
            if any(a.tag in (_w('tbl'), _w('txbxContent')) for a in node.iterancestors()):
                continue
            if node in by_element:
                rec = by_element[node]
                doc['blocks'].append({k: v for k, v in rec.items() if k not in ('element', 'text')}
                                     | {'preview': rec['text'][:160]})
            elif node in table_info:
                doc['blocks'].append(table_info[node])
    total = sum(len(list(root.iter(_m('oMath')))) for root in parts.values())
    # A malformed standalone math paragraph must also be visible in coverage.
    total += sum(1 for root in parts.values() for e in root.iter(_m('oMathPara'))
                 if not list(e.iter(_m('oMath'))))
    represented = sum(r.get('math_count', 0) for r in doc['paragraphs'])
    issues = set()
    partial = 0
    for rec in doc['paragraphs']:
        if rec.get('math_count'):
            for _, _, warnings, count in _paragraph_segments(rec['element']):
                issues.update(warnings)
                if warnings:
                    partial += count
    omitted = max(0, total - represented)
    if total:
        doc['coverage']['omml'] = {'formula_count': total, 'represented_count': represented,
                                   'partial_count': partial, 'omitted_count': omitted,
                                   'warnings': sorted(issues)[:12],
                                   'warning_types_omitted': max(0, len(issues) - 12)}
        doc['coverage']['partial'] |= bool(partial or omitted)
    return doc


def _text(p):
    return ''.join(text for text, _, _, _ in _paragraph_segments(p))


def _m(local):
    return '{' + _M + '}' + local


def _math_text(element):
    """Small structural reader, not a calculator or a general OMML converter."""
    issues = set()

    def render(node):
        tag = node.tag
        name = tag.split('}')[-1] if isinstance(tag, str) else 'xml-node'
        def incomplete(reason):
            label = reason[:80]
            issues.add(label)
            return '[OMML incomplete: ' + label + ']' + ''.join(render(c) for c in node)
        def argument(local):
            found = node.findall(_m(local))
            if len(found) != 1 or not len(found[0]):
                issues.add('missing_or_empty:' + name + '/' + local)
                return '[OMML missing: ' + local + ']'
            text = render(found[0])
            if not text.strip():
                issues.add('missing_or_empty:' + name + '/' + local)
                return '[OMML missing: ' + local + ']'
            return text
        if tag == _m('t'):
            return node.text or ''
        if tag == _w('rPr'):
            return ''  # Visual run formatting, not equation structure.
        if tag in {_m(n) for n in ('rPr', 'oMathParaPr', 'ctrlPr')}:
            allowed = {'lit', 'nor', 'sty', 'jc'}
            return ''.join('' if c.tag in {_m(n) for n in allowed} or c.tag == _w('rPr')
                           else render(c) for c in node)
        if tag in {_m(n) for n in ('oMath', 'oMathPara', 'e', 'sup', 'deg', 'r')}:
            if not len(node):
                return incomplete('empty:' + name)
            return ''.join(render(c) for c in node)
        if tag == _m('sSup'):
            if any(c.tag not in {_m('e'), _m('sup')} for c in node):
                return incomplete('unsupported:' + name)
            return '(' + argument('e') + ')^(' + argument('sup') + ')'
        if tag == _m('rad'):
            if any(c.tag not in {_m(n) for n in ('radPr', 'deg', 'e')} for c in node):
                return incomplete('unsupported:' + name)
            if len(node.findall(_m('radPr'))) > 1 or len(node.findall(_m('deg'))) > 1:
                return incomplete('duplicate:rad_child')
            props = node.find(_m('radPr'))
            hide = props.find(_m('degHide')) if props is not None else None
            value = hide.get(_m('val'), '1') if hide is not None else '0'
            if (props is not None and any(c.tag not in {_m('degHide'), _m('ctrlPr')} for c in props)
                    or props is not None and len(props.findall(_m('degHide'))) > 1
                    or value not in ('1', 'true', 'on', '0', 'false', 'off')):
                return incomplete('unsupported:radPr')
            if props is not None:
                for control in props.findall(_m('ctrlPr')):
                    render(control)
            degree = node.find(_m('deg'))
            base = argument('e')
            if value in ('1', 'true', 'on'):
                if degree is not None and len(degree):
                    return incomplete('hidden_nonempty_degree')
                return 'sqrt(' + base + ')'
            return 'root(' + argument('deg') + ', ' + base + ')'
        if tag == _m('d'):
            if any(c.tag not in {_m('dPr'), _m('e')} for c in node) or len(node.findall(_m('e'))) != 1:
                return incomplete('unsupported:' + name)
            props = node.find(_m('dPr'))
            if (len(node.findall(_m('dPr'))) > 1 or props is not None and
                    (any(c.tag not in {_m('begChr'), _m('endChr'), _m('ctrlPr')} for c in props)
                     or any(len(props.findall(_m(n))) > 1 for n in ('begChr', 'endChr')))):
                return incomplete('unsupported:dPr')
            if props is not None:
                for control in props.findall(_m('ctrlPr')):
                    render(control)
            def delimiter(local, default):
                prop = props.find(_m(local)) if props is not None else None
                return default if prop is None else prop.get(_m('val'), '')
            return delimiter('begChr', '(') + argument('e') + delimiter('endChr', ')')
        return incomplete('unsupported:' + name)

    text = render(element)
    if not text.strip():
        issues.add('empty:formula')
        text = '[OMML incomplete: empty:formula]'
    return text, issues


def _paragraph_segments(p):
    """Share text order with formatting offsets; consume math subtrees once."""
    def walk(node, run=None):
        if node is not p and node.tag == _w('p'):
            return
        if node.tag in (_m('oMath'), _m('oMathPara')):
            text, issues = _math_text(node)
            yield text, None, issues, max(1, len(list(node.iter(_m('oMath')))))
            return
        if node.tag == _w('r'):
            run = node
        if node.tag in (_w('t'), _w('tab'), _w('br'), _w('cr')):
            yield node.text or '' if node.tag == _w('t') else '\t' if node.tag == _w('tab') else '\n', run, (), 0
            return
        for child in node:
            yield from walk(child, run)
    yield from walk(p)


def _editable(p):
    complex_tags = {_w(t) for t in ('sdt', 'ins', 'del', 'moveFrom', 'moveTo', 'fldSimple', 'pPrChange', 'rPrChange')}
    return (not any(a.tag in complex_tags for a in p.iterancestors())
            and not any(e.tag in complex_tags for e in p.iterdescendants())
            and all(e.tag in {_w('pPr'), _w('r')} for e in p)
            and all(e.tag in {_w('rPr'), _w('t')} for run in p.findall(_w('r')) for e in run))


def _token(revision, query, index, offset=0):
    return base64.urlsafe_b64encode(_json([revision, query, index, offset]).encode()).decode()


def _cursor(token, revision, query, count):
    if token is None:
        return 0, 0
    try:
        if len(token) > 1024:
            raise ValueError()
        rev, q, index, offset = json.loads(base64.urlsafe_b64decode(token))
        if rev != revision or q != query or type(index) is not int or type(offset) is not int or not 0 <= index <= count or offset < 0:
            raise ValueError()
        return index, offset
    except (ValueError, TypeError, UnicodeError):
        _fail('invalid_cursor', 'Cursor does not belong to this document revision/query.')


def _inspect(path, cursor, limit):
    if type(limit) is not int or not 1 <= limit <= 40:
        _fail('invalid_limit', 'limit must be between 1 and 40.')
    doc = _open(path)
    index, offset = _cursor(cursor, doc['revision'], 'inspect', len(doc['blocks']))
    if offset:
        _fail('invalid_cursor', 'Inspect cursor has an invalid offset.')
    result = {'ok': True, 'revision': doc['revision'], 'paragraph_count': len(doc['paragraphs']),
              'table_count': sum(len(root.findall('.//' + _w('tbl'))) for root in doc['parts'].values()),
              'section_count': len(doc['parts']['word/document.xml'].findall('.//' + _w('sectPr'))),
              'parts': list(doc['parts']), 'coverage': doc['coverage'], 'items': [], 'next_cursor': None}
    while index < len(doc['blocks']) and len(result['items']) < limit:
        result['items'].append(doc['blocks'][index])
        result['next_cursor'] = _token(doc['revision'], 'inspect', index + 1)
        if len(_json(result)) > _MAX_JSON:
            result['items'].pop()
            break
        index += 1
    result['next_cursor'] = _token(doc['revision'], 'inspect', index) if index < len(doc['blocks']) else None
    if not result['items'] and index < len(doc['blocks']):
        _fail('result_limit_exceeded', 'An inspection item exceeds the result budget.')
    return result


def _runs(p):
    result, offset = [], 0
    offsets = {}
    for text, run, _, _ in _paragraph_segments(p):
        if run is not None:
            spans = offsets.setdefault(run, [])
            if spans and spans[-1][1] == offset:
                spans[-1][1] = offset + len(text)
            else:
                spans.append([offset, offset + len(text)])
        offset += len(text)
    offset = 0
    for run in p.iter(_w('r')):
        if next((a for a in run.iterancestors() if a.tag == _w('p')), None) is not p:
            continue
        text = ''.join(e.text or '' if e.tag == _w('t') else '\t' if e.tag == _w('tab') else '\n'
                       for e in run if e.tag in (_w('t'), _w('tab'), _w('br'), _w('cr')))
        props = run.find(_w('rPr'))
        def flag(name):
            prop = props.find(_w(name)) if props is not None else None
            return None if prop is None else prop.get(_w('val'), 'true') not in ('false', '0', 'off')
        record = {'bold': flag('b'), 'italic': flag('i')}
        if props is not None:
            for tag, key in [('rStyle', 'style_id'), ('sz', 'size_half_points'), ('u', 'underline'), ('color', 'color')]:
                prop = props.find(_w(tag))
                if prop is not None:
                    record[key] = prop.get(_w('val'))
            fonts = props.find(_w('rFonts'))
            if fonts is not None:
                record['fonts'] = {k.split('}')[-1]: v for k, v in fonts.attrib.items()}
        result.extend({'start': start, 'end': end, **record}
                      for start, end in offsets.get(run, [(offset, offset)]))
        offset += len(text)
    return result


def _reading_runs(p):
    # Reading needs emphasis, not a repeated font/size/style snapshot. Exact
    # editing validates the original XML independently of these observations.
    result = []
    for run in _runs(p):
        formatting = {k: run[k] for k in ('bold', 'italic', 'underline', 'color')
                      if run.get(k) is not None}
        if not formatting or run['start'] == run['end']:
            continue
        if (result and result[-1]['end'] == run['start']
                and {k: v for k, v in result[-1].items() if k not in ('start', 'end')} == formatting):
            result[-1]['end'] = run['end']
        else:
            result.append({'start': run['start'], 'end': run['end'], **formatting})
    return result


def _read_item(rec, offset, end, runs):
    item = {'id': rec['id'], 'text': rec['text'][offset:end]}
    if offset or end < len(rec['text']):
        item.update(text_offset=offset, complete=end == len(rec['text']))
    if not rec['editable']:
        item['editable'] = False
    if rec['style_id'] != 'Normal':
        item['style_name'] = rec['style_name']
    if rec['location'] is not None:
        item['location'] = {k: v for k, v in rec['location'].items()
                            if k in ('table_id', 'row', 'column')
                            or k == 'column_span' and v != 1
                            or k == 'vertical_merge' and v is not None}
    if rec.get('math_count'):
        item.update(math_count=rec['math_count'], math_partial=rec['math_partial'])
    selected_runs = [r for r in runs if r['end'] > offset and r['start'] < end]
    if selected_runs:
        item['runs'] = selected_runs
    return item


def _read_coverage(coverage):
    result = {'partial': coverage['partial']}
    unsupported = {k: v for k, v in coverage['unsupported'].items() if v}
    if unsupported:
        result['unsupported'] = unsupported
    math = coverage.get('omml')
    if math and (math['partial_count'] or math['omitted_count']):
        result['omml'] = {k: v for k, v in math.items()
                          if k not in ('formula_count', 'represented_count')}
    return result


def _read(path, revision, node_ids, contains, cursor, max_chars):
    if type(max_chars) is not int or not 1800 <= max_chars <= _MAX_JSON:
        _fail('limit_too_small', 'max_chars must be between 1800 and 12000 serialized JSON characters including the function envelope.')
    budget = min(max_chars, _OUTPUT_LIMIT)
    if node_ids is not None and contains is not None:
        _fail('invalid_selector', 'Choose node_ids or contains, not both.')
    if contains is not None and (not isinstance(contains, str) or not contains or len(contains) > 512):
        _fail('invalid_selector', 'contains must be a nonempty literal string, at most 512 characters.')
    doc = _open(path, revision)
    selected = doc['paragraphs']
    if node_ids is not None:
        if not isinstance(node_ids, list) or len(node_ids) > 64 or any(type(n) is not str for n in node_ids):
            _fail('invalid_selector', 'node_ids must be a list of at most 64 IDs.')
        nodes = []
        for node_id in node_ids:
            if node_id not in doc['nodes']:
                _fail('unknown_node', 'Node does not exist in this revision: ' + node_id[:150])
            nodes.append(doc['nodes'][node_id])
        selected = [p for p in selected if any(p['element'] is n or n in p['element'].iterancestors() for n in nodes)]
    elif contains is not None:
        selected = [p for p in selected if contains in p['text']]
    query = hashlib.sha256(_json([node_ids, contains]).encode()).hexdigest()
    index, offset = _cursor(cursor, doc['revision'], query, len(selected))
    result = {'ok': True, 'revision': doc['revision'], 'coverage': _read_coverage(doc['coverage']), 'items': [], 'next_cursor': None}
    if _read_size(result) > budget:
        _fail('limit_too_small', 'Document metadata exceeds min(max_chars, --output-limit).')
    while index < len(selected):
        rec = selected[index]
        if offset > len(rec['text']):
            _fail('invalid_cursor', 'Text offset is outside the selected paragraph.')
        # Segment long paragraphs and bound run metadata as well as text.
        end = min(len(rec['text']), offset + budget)
        item = _read_item(rec, offset, end, _reading_runs(rec['element']))
        result['items'].append(item)
        result['next_cursor'] = _token(doc['revision'], query, index if end < len(rec['text']) else index + 1,
                                       end if end < len(rec['text']) else 0)
        if _read_size(result) > budget:
            result['items'].pop()
            if result['items']:
                break
            if end - offset > 1:
                # Retry with a smaller segment budget, without losing any text/runs.
                return _read_segment(doc, rec, query, index, offset, budget, len(selected))
            _fail('limit_too_small', 'Paragraph metadata exceeds min(max_chars, --output-limit).')
        if end < len(rec['text']):
            offset = end
            break
        index, offset = index + 1, 0
    result['next_cursor'] = _token(doc['revision'], query, index, offset) if index < len(selected) else None
    return result


def _read_segment(doc, rec, query, index, offset, budget, count):
    runs = _reading_runs(rec['element'])
    low, high, best = offset + 1, min(len(rec['text']), offset + budget), None
    while low <= high:
        end = (low + high) // 2
        item = _read_item(rec, offset, end, runs)
        next_index, next_offset = (index + 1, 0) if end == len(rec['text']) else (index, end)
        result = {'ok': True, 'revision': doc['revision'], 'coverage': _read_coverage(doc['coverage']), 'items': [item],
                  'next_cursor': _token(doc['revision'], query, next_index, next_offset) if next_index < count else None}
        if _read_size(result) <= budget:
            best, low = result, end + 1
        else:
            high = end - 1
    if best is not None:
        return best
    _fail('limit_too_small', 'Paragraph metadata exceeds the response budget.')


def _replace(p, old, new):
    # Refuse complex paragraphs rather than silently destroying fields/anchors/revisions.
    if not _editable(p):
        _fail('unsupported_target', 'Only ordinary text-run paragraphs can be edited.')
    texts, offset = [], 0
    for run in p.findall(_w('r')):
        if any(e.tag not in {_w('rPr'), _w('t')} for e in run):
            _fail('unsupported_target', 'Target paragraph contains non-text run content.')
        props = run.find(_w('rPr'))
        signature = _ET.tostring(props, method='c14n') if props is not None and (len(props) or props.attrib) else b''
        for text in run.findall(_w('t')):
            value = text.text or ''
            texts.append((text, offset, offset + len(value), signature))
            offset += len(value)
    visible = ''.join(t.text or '' for t, *_ in texts)
    start = visible.find(old)
    if start < 0:
        _fail('text_not_found', 'The exact old text is not present in the target paragraph.')
    if visible.find(old, start + 1) >= 0:
        _fail('ambiguous_match', 'The old text occurs more than once in the target paragraph.')
    end = start + len(old)
    affected = [(t, a, b, fmt) for t, a, b, fmt in texts if a < end and b > start]
    if len({fmt for _, _, _, fmt in affected}) != 1:
        _fail('format_conflict', 'Replacement crosses different run formatting; select a compatible fragment.')
    for i, (text, a, b, _) in enumerate(affected):
        value = text.text or ''
        text.text = value[:max(0, start - a)] + (new if i == 0 else '') + value[min(len(value), end - a):]
        if text.text and (text.text[0].isspace() or text.text[-1].isspace()):
            text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')


def _edit(path, output_path, revision, edits):
    doc = _open(path, revision)
    output = _path(output_path)
    if output.exists() or (_ROOT / output_path).is_symlink():
        _fail('destination_exists', 'Output already exists; choose a new copy path.')
    if not output.parent.is_dir():
        _fail('invalid_path', 'Output parent directory must already exist.')
    if doc['coverage']['unsupported']['digital_signatures']:
        _fail('unsupported_target', 'Editing signed packages would invalidate their signatures.')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
        _fail('invalid_edit', 'Supply 1 to 64 exact text replacements.')
    seen, changed, receipt = set(), set(), []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'node_id', 'old', 'new'} or any(type(v) is not str for v in edit.values()):
            _fail('invalid_edit', 'Each edit must contain string node_id, old, new only.')
        node_id, old, new = edit['node_id'], edit['old'], edit['new']
        if not old or len(old) + len(new) > 16000 or any(c in old + new for c in '\n\r\t'):
            _fail('invalid_edit', 'Use nonempty old text and single-line replacement strings, at most 16000 combined characters.')
        if node_id in seen:
            _fail('invalid_edit', 'Only one replacement per paragraph is allowed in a batch.')
        seen.add(node_id)
        p = doc['nodes'].get(node_id)
        if p is None or p.tag != _w('p'):
            _fail('unknown_node', 'Edit target must be a paragraph ID from this revision.')
        if any(a.tag in (_w('sdt'), _w('ins'), _w('del'), _w('fldSimple')) for a in p.iterancestors()):
            _fail('unsupported_target', 'Target is inside a content control, field or revision.')
        try:
            _replace(p, old, new)
        except ValueError:
            _fail('invalid_edit', 'Replacement contains invalid XML characters.')
        changed.add(node_id.rsplit(':', 1)[0])
        receipt.append({'node_id': node_id, 'replacements': 1})
    updated = dict(doc['members'])
    for name in changed:
        updated[name] = _ET.tostring(doc['parts'][name], xml_declaration=True, encoding='UTF-8', standalone=True)
        _xml(updated[name])
    # Construct and validate the entire output before creating its public name.
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.comment = doc['comment']
        for info in doc['infos']:
            archive.writestr(copy.copy(info), updated[info.filename])
    data = buffer.getvalue()
    with ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None or any(archive.read(name) != doc['members'][name]
                                               for name in doc['members'] if name not in changed):
            _fail('write_failed', 'Output package failed preservation verification.')
    result = {'ok': True, 'path': _relative(output), 'source_revision': doc['revision'],
              'revision': hashlib.sha256(data).hexdigest(), 'edits': receipt,
              'changed_parts': sorted(changed), 'untouched_parts_verified': len(updated) - len(changed)}
    if len(_json(result)) > _MAX_JSON:
        _fail('result_limit_exceeded', 'Edit receipt exceeds the result budget; use a smaller batch.')
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.docx-edit-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            # Atomic, no-clobber publication (same filesystem); no overwrite race.
            try:
                os.link(temporary, output)
            except FileExistsError:
                _fail('destination_exists', 'Output was created by another operation.')
        finally:
            temporary.unlink(missing_ok=True)
    return result


def _publish_directory(stage, output):
    # Pavlusha runs on Linux (bubblewrap). Never replace even an empty destination
    # created between our admission check and directory publication.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        _fail('dependency_missing', 'Atomic no-overwrite directory publication requires Linux renameat2.')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(output), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            _fail('destination_exists', 'Render output was created by another operation.')
        _fail('write_failed', 'Cannot publish render output: ' + os.strerror(code))


def _render_command(command, stage, deadline, log_name='render.log', env=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _fail('render_timeout', 'Renderer exceeded its timeout; no output directory published.')
    log_path = stage / log_name
    with log_path.open('ab') as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=log,
                                       start_new_session=True, env=env)
        except FileNotFoundError:
            _fail('dependency_missing', 'Rendering executable is missing: ' + Path(command[0]).name)
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            _fail('render_timeout', 'Renderer exceeded its timeout; no output directory published.')
    if code != 0:
        with log_path.open('rb') as log:
            log.seek(max(0, log_path.stat().st_size - 800))
            detail = log.read().decode(errors='replace')
        _fail('render_failed', f'Renderer exit {code}: ' + detail)


def _render_standard(source, stage, dpi, deadline, commands):
    soffice, pdfinfo, pdftoppm = commands
    profile = stage / 'lo-profile'
    _render_command([soffice, '-env:UserInstallation=' + profile.as_uri(),
                     '--headless', '--convert-to', 'pdf', '--outdir', str(stage), str(source)], stage, deadline)
    pdf = stage / 'source.pdf'
    if not pdf.is_file() or not pdf.stat().st_size:
        _fail('render_failed', 'LibreOffice did not produce a PDF.')
    _render_command([pdfinfo, str(pdf)], stage, deadline, 'pdfinfo.log',
                    {**os.environ, 'LC_ALL': 'C'})
    with (stage / 'pdfinfo.log').open('r', errors='replace') as log:
        info = log.read(65536)
    match = re.search(r'^Pages:\s+(\d+)\s*$', info, re.MULTILINE)
    if match is None or not 1 <= int(match[1]) <= 200:
        _fail('render_failed', 'Expected a PDF with 1..200 pages.')
    count = int(match[1])
    _render_command([pdftoppm, '-f', '1', '-l', str(count), '-r', str(dpi),
                     '-png', str(pdf), str(stage / 'page')], stage, deadline)
    # Poppler pads the numeric suffix for multi-page documents; public filenames
    # retain the existing page-1.png, page-2.png contract.
    for page in stage.glob('page-*.png'):
        match = re.fullmatch(r'page-(\d+)\.png', page.name)
        if match is None:
            _fail('render_failed', 'Poppler produced invalid page filenames.')
        target = stage / f'page-{int(match[1])}.png'
        if target != page:
            page.rename(target)
    if len(list(stage.glob('page-*.png'))) != count:
        _fail('render_failed', 'Poppler did not rasterize every PDF page.')
    if profile.exists():
        shutil.rmtree(profile)


def _render(path, revision, output_dir, dpi, timeout_seconds):
    doc = _open(path, revision)
    output = _path(output_dir)
    if output.exists() or (_ROOT / output_dir).is_symlink():
        _fail('destination_exists', 'Render output directory already exists.')
    if not output.parent.is_dir():
        _fail('invalid_path', 'Render output parent must exist.')
    if type(dpi) is not int or not 72 <= dpi <= 200 or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        _fail('invalid_render_options', 'Use dpi 72..200 and timeout_seconds 1..300.')
    commands = None
    if _RENDERER:
        if not Path(_RENDERER).is_file() or not shutil.which(_PYTHON):
            _fail('dependency_missing', 'Configured DOCX renderer or its Python executable is missing.')
    else:
        names = ('soffice', 'pdfinfo', 'pdftoppm')
        commands = tuple(shutil.which(name) for name in names)
        missing = [name for name, command in zip(names, commands) if not command]
        if missing:
            _fail('dependency_missing', 'Rendering requires tools on host PATH: ' + ', '.join(missing))
    try:
        from PIL import Image as _Image
    except ImportError:
        _fail('dependency_missing', 'Pillow is required for mechanical PNG integrity checks.')
    stage = Path(tempfile.mkdtemp(dir=output.parent, prefix='.docx-render-'))
    try:
        source = stage / 'source.docx'
        source.write_bytes(doc['data'])
        deadline = time.monotonic() + timeout_seconds
        if _RENDERER:
            _render_command([_PYTHON, _RENDERER, str(source), '--output_dir', str(stage),
                             '--emit_pdf', '--dpi', str(dpi)], stage, deadline)
        else:
            _render_standard(source, stage, dpi, deadline, commands)
        pdf = stage / 'source.pdf'
        try:
            pages = sorted(stage.glob('page-*.png'), key=lambda p: int(p.stem.split('-')[-1]))
        except ValueError:
            _fail('render_failed', 'Renderer produced invalid page filenames.')
        if not pdf.is_file() or pdf.stat().st_size == 0 or not pages or len(pages) > 200:
            _fail('render_failed', 'Renderer did not produce a PDF and 1..200 page PNGs.')
        if [p.name for p in pages] != [f'page-{i}.png' for i in range(1, len(pages) + 1)]:
            _fail('render_failed', 'Renderer page numbering is incomplete.')
        try:
            with pdf.open('rb') as stream:
                if stream.read(5) != b'%PDF-':
                    raise ValueError('invalid PDF header')
            for page in pages:
                with _Image.open(page) as image:
                    image.verify()
        except (OSError, ValueError, _Image.DecompressionBombError) as exc:
            _fail('render_failed', 'Invalid renderer artifacts: ' + str(exc)[:300])
        pdf.rename(stage / 'document.pdf')
        source.unlink()
        manifest = {'source_revision': doc['revision'], 'page_count': len(pages), 'dpi': dpi,
                    'renderer': Path(_RENDERER).name if _RENDERER else 'LibreOffice + Poppler',
                    'pages': [p.name for p in pages],
                    'pdf': 'document.pdf', 'visual_verification': 'not_performed',
                    'warnings': ['LibreOffice output may differ from Word; cached fields/comments are not certified.']}
        (stage / 'manifest.json').write_text(_json(manifest), encoding='utf-8')
        # rename publishes one complete directory, never incremental render output.
        # Refuse symlinks/existing directories again immediately before publication.
        if output.exists() or output.is_symlink():
            _fail('destination_exists', 'Render output appeared during execution.')
        _publish_directory(stage, output)
        return {'ok': True, 'revision': doc['revision'], 'page_count': len(pages), 'dpi': dpi,
                'manifest_path': _relative(output / 'manifest.json'), 'pdf_path': _relative(output / 'document.pdf'),
                'page_pattern': _relative(output / 'page-{N}.png'), 'visual_verification': 'not_performed',
                'warnings': manifest['warnings']}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def docx_inspect(path: str, cursor: str | None = None, limit: int = 40) -> dict:
    """Map DOCX body/table/header/footer structure, IDs, revision and coverage warnings, including OMML equations; no semantic summary."""
    return _run(_inspect, path, cursor, limit)


def docx_read(path: str, revision: str, node_ids: list[str] | None = None,
              contains: str | None = None, cursor: str | None = None, max_chars: int = 8000) -> dict:
    """Read addressable text and OMML with compact emphasis metadata. Whole paragraphs omit text_offset/complete (0/true); editable defaults true. max_chars bounds full JSON including envelope, capped by --output-limit. Follow next_cursor; IDs require revision."""
    return _run(_read, path, revision, node_ids, contains, cursor, max_chars)


def docx_edit(path: str, output_path: str, revision: str, edits: list[dict[str, str]]) -> dict:
    """Save a new copy with exact {node_id,old,new} replacements: one per paragraph, unique old text, same-format runs only. No overwrite."""
    return _run(_edit, path, output_path, revision, edits)


def docx_render(path: str, revision: str, output_dir: str, dpi: int = 144, timeout_seconds: int = 120) -> dict:
    """Render the given revision to PDF/page PNGs and a manifest. Mechanical success is not visual approval; use existing GUI to inspect pages."""
    return _run(_render, path, revision, output_dir, dpi, timeout_seconds)
