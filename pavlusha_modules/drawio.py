"""Native draw.io XML files and local Desktop rendering."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from xml.etree import ElementTree as ET

from pavlusha_agent.extensions import Contribution, Outcome
from pavlusha_modules.util import declare, TEXT
from pavlusha_modules.file_paths import work_file as _file


def _validate(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > 2_000_000:
        raise ValueError('XML must be text of at most 2 MB')
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        raise ValueError('DTD and entities are not supported')
    try:
        doc = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError('Invalid draw.io XML: ' + str(exc)) from exc
    if doc.tag != 'mxfile' or not doc.findall('diagram'):
        raise ValueError('Expected mxfile containing at least one diagram')
    if any(page.find('mxGraphModel/root') is None for page in doc.findall('diagram')):
        raise ValueError('Each diagram must contain an uncompressed mxGraphModel with root')
    return doc


class Drawio:
    name = 'drawio'

    def __init__(self, root, executable):
        self.root = Path(root).resolve(strict=True)
        self.executable = executable

    def on_event(self, event):
        if event.kind != 'session_started':
            return
        return Contribution(message=(
            'Create editable diagrams using native uncompressed draw.io XML. '
            'Use drawio_save then drawio_export and view_image to check the result. '
            'Keep XML simple: mxfile/diagram/mxGraphModel/root with mxCell vertices and edges; '
            'root cells 0 and 1, vertex parent 1, geometry as="geometry", '
            'and edge source/target refer to vertex ids. Place shapes with x/y/width/height.'),
            tools=(
                declare('drawio_save', 'Save a new editable .drawio file from complete uncompressed draw.io XML. No overwrite.',
                        {'output_path': TEXT, 'xml': TEXT}, ('output_path', 'xml'), self.save),
                declare('drawio_export', 'Export the first page of a local .drawio file to a new PNG using draw.io Desktop. Then use view_image to inspect it.',
                        {'path': TEXT, 'output_path': TEXT}, ('path', 'output_path'), self.export),
            ))

    def save(self, args, context):
        content = args['xml']
        doc = _validate(content)
        output = _file(self.root, args['output_path'], '.drawio')
        if not output.parent.is_dir():
            raise ValueError('Output parent directory does not exist')
        with output.open('x', encoding='utf-8') as stream:
            stream.write(content)
        return Outcome({'path': args['output_path'], 'pages': len(doc.findall('diagram')),
                        'revision': hashlib.sha256(content.encode('utf-8')).hexdigest()})

    def export(self, args, context):
        source = _file(self.root, args['path'], '.drawio')
        output = _file(self.root, args['output_path'], '.png')
        if output.exists():
            raise ValueError('Output already exists')
        if not output.parent.is_dir():
            raise ValueError('Output parent directory does not exist')
        if not source.is_file():
            raise ValueError('Diagram not found')
        executable = self.executable or shutil.which('drawio') or shutil.which('draw.io')
        if not executable or not os.access(executable, os.X_OK):
            raise ValueError('draw.io Desktop executable not found or not executable; configure drawio executable')
        with tempfile.TemporaryDirectory(prefix='.drawio-', dir=self.root) as tmp:
            tmpout = Path(tmp) / 'diagram.png'
            env = os.environ.copy()
            if str(executable).lower().endswith('.appimage'):
                env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
            try:
                result = subprocess.run([executable, '--export', '--format', 'png', '--output', str(tmpout), str(source)],
                                        capture_output=True, text=True, timeout=120, env=env)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('draw.io export timed out') from exc
            if result.returncode or not tmpout.is_file():
                raise ValueError('draw.io export failed: ' + (result.stderr or result.stdout)[-1500:])
            # Both files are on the workdir filesystem; publish only a complete PNG
            # and atomically refuse an output created by another process.
            os.link(tmpout, output)
        return Outcome({'path': args['output_path'], 'source': args['path'], 'bytes': output.stat().st_size})


def create_extension(*, settings, context):
    if set(settings) - {'executable'} or ('executable' in settings and not isinstance(settings['executable'], str)):
        raise ValueError('drawio accepts only executable (string)')
    return Drawio(context['workdir'], settings.get('executable'))
