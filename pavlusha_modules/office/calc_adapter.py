"""Backend-neutral spreadsheet contract and LibreOffice Calc adapter."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from typing import Protocol


class SpreadsheetAdapter(Protocol):
    def read(self, path: str, revision: str, sheet: str, cell_range: str) -> dict: ...
    def edit(self, path: str, output_path: str, revision: str, edits: list[dict]) -> dict: ...


class OfficeAdapterError(Exception):
    """An adapter operation could not be completed."""


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class LibreOfficeCalcAdapter:
    root: Path
    python: str = '/usr/bin/python3'
    soffice: str = '/usr/bin/soffice'
    timeout: int = 60

    def __post_init__(self):
        self.root = Path(self.root).resolve(strict=True)
        if not self.root.is_dir():
            raise OfficeAdapterError('Work directory must be a directory')

    def _path(self, value: str, suffix: str = '.ods') -> Path:
        if not isinstance(value, str) or not value or '\0' in value:
            raise OfficeAdapterError('Invalid path')
        p = Path(value)
        if p.is_absolute() or '..' in p.parts:
            raise OfficeAdapterError('Path must be relative to the work directory')
        p = (self.root / p).resolve()
        if p == self.root or not p.is_relative_to(self.root) or p.suffix.lower() != suffix:
            raise OfficeAdapterError('Path must point to a ' + suffix + ' file inside the work directory')
        return p

    @staticmethod
    def _range(value: str, single: bool = False):
        match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?', value or '')
        if not match or (single and match.group(3)):
            raise OfficeAdapterError('Invalid cell address or range')
        def column(s):
            n = 0
            for c in s:
                n = n * 26 + ord(c) - 64
            return n
        a, b = column(match.group(1)), int(match.group(2))
        c, d = column(match.group(3) or match.group(1)), int(match.group(4) or match.group(2))
        if a > c or b > d or c > 16384 or d > 1048576 or (c-a+1)*(d-b+1) > 4096:
            raise OfficeAdapterError('Cell range is outside bounds or too large')
        return a, b, c, d

    def inspect(self, path: str) -> dict:
        source = self._path(path)
        result = self._run(path, revision(source), {'action':'inspect'})
        result['revision'] = result['source_revision']
        return result

    def create(self, output_path: str, sheet: str = 'Sheet1') -> dict:
        destination=self._path(output_path)
        if destination.exists() or (self.root/output_path).is_symlink():
            raise OfficeAdapterError('Output already exists')
        if not destination.parent.is_dir(): raise OfficeAdapterError('Output parent directory must exist')
        if not isinstance(sheet,str) or not 1<=len(sheet)<=31 or any(c in sheet for c in '[]:*?/\\'):
            raise OfficeAdapterError('Invalid sheet name')
        try:
            with tempfile.TemporaryDirectory(prefix='pavlusha-calc-') as tmp:
                result_path=Path(tmp)/'result.ods'
                request={'action':'create','sheet':sheet,'output':str(result_path),'soffice':self.soffice}
                proc=subprocess.Popen([self.python,str(Path(__file__).with_name('_uno_calc_worker.py'))],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
                try: stdout,stderr=proc.communicate(json.dumps(request),timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL);proc.communicate();raise
                if proc.returncode: raise OfficeAdapterError('LibreOffice adapter failed: '+stderr.strip()[-1000:])
                result=json.loads(stdout)
                fd,staged=tempfile.mkstemp(prefix='.pavlusha-',suffix='.ods',dir=destination.parent)
                try:
                    with os.fdopen(fd,'wb') as stream: stream.write(result_path.read_bytes())
                    os.link(staged,destination)
                finally: os.unlink(staged)
                result.update(path=output_path,revision=revision(destination))
                return result
        except (OSError,subprocess.TimeoutExpired,ValueError) as exc:
            raise OfficeAdapterError(str(exc)) from exc

    def _run(self, path, expected, request, output=None, output_suffix='.ods'):
        source = self._path(path)
        if output is not None:
            destination = self._path(output, output_suffix)
            if destination.exists() or (self.root / output).is_symlink():
                raise OfficeAdapterError('Output already exists')
            if not destination.parent.is_dir():
                raise OfficeAdapterError('Output directory does not exist')
        try:
            # Snapshot the bytes so revision validation and LibreOffice see the same file.
            data = source.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise OfficeAdapterError('Workbook revision is stale')
            with tempfile.TemporaryDirectory(prefix='pavlusha-calc-') as tmp:
                snapshot = Path(tmp) / 'source.ods'
                snapshot.write_bytes(data)
                result_path = Path(tmp) / ('result' + output_suffix)
                request.update(source=str(snapshot), output=str(result_path), soffice=self.soffice)
                proc = subprocess.Popen([self.python, str(Path(__file__).with_name('_uno_calc_worker.py'))],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
                try:
                    stdout, stderr = proc.communicate(json.dumps(request), timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    raise
                if proc.returncode:
                    raise OfficeAdapterError('LibreOffice adapter failed: ' + stderr.strip()[-1000:])
                result = json.loads(stdout)
                result['source_revision'] = actual
                if output is not None:
                    # Stage on the target filesystem, then publish without overwriting.
                    fd, staged = tempfile.mkstemp(prefix='.pavlusha-', suffix=output_suffix, dir=destination.parent)
                    try:
                        with os.fdopen(fd, 'wb') as f:
                            f.write(result_path.read_bytes())
                        os.link(staged, destination)
                    finally:
                        os.unlink(staged)
                    result.update(path=output, revision=revision(destination))
                return result
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise OfficeAdapterError(str(exc)) from exc

    def read(self, path: str, revision: str, sheet: str, cell_range: str) -> dict:
        self._range(cell_range)
        return self._run(path, revision, {'action': 'read', 'sheet': sheet, 'range': cell_range})

    def chart(self, path: str, output_path: str, revision: str, sheet: str, cell_range: str,
              anchor: str, name: str, type: str, title: str = '', x_title: str = '',
              y_title: str = '', legend: bool = True, headers: bool = True) -> dict:
        a, b, c, d = self._range(cell_range)
        self._range(anchor, single=True)
        if (type not in ('scatter', 'line', 'column', 'pie') or not isinstance(sheet, str) or not sheet
                or not isinstance(name, str) or not 1 <= len(name) <= 80
                or c-a < 1 or (type == 'pie' and c-a != 1) or d-b < (1 if not headers else 2)
                or any(not isinstance(v,str) or len(v)>200 for v in (title,x_title,y_title))
                or legend.__class__ is not bool or headers.__class__ is not bool):
            raise OfficeAdapterError('Invalid chart settings')
        return self._run(path, revision, {'action':'chart','sheet':sheet,'cell_range':cell_range,
            'anchor':anchor,'name':name,'type':type,'title':title,'x_title':x_title,'y_title':y_title,
            'legend':legend,'headers':headers}, output_path)

    def chart_list(self, path: str, revision: str, sheet: str) -> dict:
        if not isinstance(sheet,str) or not sheet:
            raise OfficeAdapterError('Invalid sheet name')
        return self._run(path, revision, {'action':'chart_list','sheet':sheet})

    def chart_export(self, path: str, output_path: str, revision: str, sheet: str, chart_index: int = 1) -> dict:
        if not isinstance(sheet,str) or not sheet or chart_index.__class__ is not int or not 1<=chart_index<=100:
            raise OfficeAdapterError('Invalid sheet name or chart index')
        return self._run(path, revision, {'action':'chart_export','sheet':sheet,'chart_index':chart_index}, output_path, '.png')

    def edit(self, path: str, output_path: str, revision: str, edits: list[dict]) -> dict:
        if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
            raise OfficeAdapterError('Supply 1 to 64 edits')
        seen = set()
        for item in edits:
            if not isinstance(item, dict) or set(item) not in ({'sheet', 'cell', 'value'}, {'sheet', 'cell', 'formula'}, {'sheet','cell','date'}):
                raise OfficeAdapterError('Each edit needs sheet, cell, and value, formula or date')
            if not isinstance(item['sheet'], str) or not isinstance(item['cell'], str):
                raise OfficeAdapterError('Invalid sheet or cell')
            self._range(item['cell'], single=True)
            key = (item['sheet'], item['cell'])
            if key in seen:
                raise OfficeAdapterError('Duplicate cell')
            seen.add(key)
            if 'formula' in item:
                if not isinstance(item['formula'], str) or not item['formula'].startswith('='):
                    raise OfficeAdapterError('Formula must start with =')
            elif 'date' in item:
                from datetime import date
                try:
                    if not isinstance(item['date'],str): raise ValueError()
                    date.fromisoformat(item['date'])
                except ValueError as exc: raise OfficeAdapterError('Date must be ISO YYYY-MM-DD') from exc
            elif item['value'] is not None and type(item['value']) not in (str, int, float, bool):
                raise OfficeAdapterError('Value must be a JSON scalar or null')
        return self._run(path, revision, {'action': 'edit', 'edits': edits}, output_path)
