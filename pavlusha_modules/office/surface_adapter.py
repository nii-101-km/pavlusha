"""Build a static 3D surface image from a Calc sheet, inside the shell sandbox."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import uuid

from pavlusha_agent.sandbox import run_shell
from pavlusha_modules.file_paths import work_file as _file


def create_surface(root, path, output_path, image_path, revision, sheet, cell_range, anchor,
                   title='Поверхность', x_title='X', y_title='Y', z_title='Z'):
    try:
        root=Path(root).resolve(strict=True)
        source=_file(root,path,'.ods');out=_file(root,output_path,'.ods');img=_file(root,image_path,'.png')
        if out.exists() or img.exists(): raise ValueError('Output already exists')
        if not out.parent.is_dir() or not img.parent.is_dir(): raise ValueError('Output parent directory does not exist')
        if (not isinstance(sheet,str) or not sheet or len(sheet)>200 or
            any(not isinstance(x,str) or len(x)>150 or '\0' in x for x in (title,x_title,y_title,z_title)) or
            not isinstance(cell_range,str) or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}:[A-Z]{1,3}[1-9][0-9]{0,6}',cell_range) or
            not isinstance(anchor,str) or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}',anchor)):
            raise ValueError('Invalid sheet, range, anchor or label')
        data=source.read_bytes()
        if len(data)>25_000_000: raise ValueError('Source file exceeds 25 MB')
        actual=hashlib.sha256(data).hexdigest()
        if actual!=revision: raise ValueError('Workbook revision is stale')
        snapshot=None
        token=uuid.uuid4().hex
        tmp_doc=root/('.surface-'+token+'.ods')
        tmp_img=root/('.surface-'+token+'.png')
        try:
            fd,snapshot=tempfile.mkstemp(prefix='.surface-source-',suffix='.ods',dir=root)
            with os.fdopen(fd,'wb') as stream: stream.write(data)
            args={'snapshot':Path(snapshot).name,'temp_image':tmp_img.name,'temp_document':tmp_doc.name,
                  'sheet':sheet,'cell_range':cell_range,'anchor':anchor,'title':title,'x_title':x_title,
                  'y_title':y_title,'z_title':z_title}
            worker=Path(__file__).with_name('_surface_sandbox_worker.py').read_bytes()
            runner='import base64;exec(base64.b64decode('+repr(base64.b64encode(worker).decode())+'))'
            command='/usr/bin/python3 -c '+shlex.quote(runner)+' '+shlex.quote(json.dumps(args,ensure_ascii=True))
            result=run_shell(root,command,network=False,timeout=60)
            if result.timed_out: raise ValueError('Surface rendering timed out')
            try: message=json.loads(result.stdout)
            except ValueError: raise ValueError((result.stderr or 'Surface worker failed')[:1000])
            if result.exit_code!=0 or not message.get('ok'): raise ValueError(str(message.get('error','Surface worker failed'))[:1000])
            os.link(tmp_doc,out)
            try: os.link(tmp_img,img)
            except OSError:
                out.unlink();raise
            message.update(path=output_path,image_path=image_path,source_revision=actual,
                           revision=hashlib.sha256(out.read_bytes()).hexdigest())
            return message
        finally:
            if snapshot: Path(snapshot).unlink(missing_ok=True)
            tmp_doc.unlink(missing_ok=True);tmp_img.unlink(missing_ok=True)
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'surface_error','message':str(exc)[:1000]}}


def inspect_ods(root, path, sheet=None, cell_range=None):
    try:
        root=Path(root).resolve(strict=True)
        source=_file(root,path,'.ods')
        if sheet is not None and (not isinstance(sheet,str) or not sheet or len(sheet)>200):
            raise ValueError('Invalid sheet')
        if cell_range is not None and (not sheet or not isinstance(cell_range,str) or
                not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}(:[A-Z]{1,3}[1-9][0-9]{0,6})?',cell_range)):
            raise ValueError('Use a sheet and bounded A1 range')
        data=source.read_bytes()
        if len(data)>25_000_000: raise ValueError('Source file exceeds 25 MB')
        fd,snapshot=tempfile.mkstemp(prefix='.surface-source-',suffix='.ods',dir=root)
        try:
            with os.fdopen(fd,'wb') as stream: stream.write(data)
            args={'action':'inspect','snapshot':Path(snapshot).name,'sheet':sheet,'cell_range':cell_range or 'A1:J10'}
            worker=Path(__file__).with_name('_surface_sandbox_worker.py').read_bytes()
            runner='import base64;exec(base64.b64decode('+repr(base64.b64encode(worker).decode())+'))'
            command='/usr/bin/python3 -c '+shlex.quote(runner)+' '+shlex.quote(json.dumps(args,ensure_ascii=True))
            result=run_shell(root,command,network=False,timeout=60)
            if result.timed_out: raise ValueError('ODS inspection timed out')
            try: message=json.loads(result.stdout)
            except ValueError: raise ValueError((result.stderr or 'ODS worker failed')[:1000])
            if result.exit_code!=0 or not message.get('ok'): raise ValueError(str(message.get('error','ODS worker failed'))[:1000])
            message['revision']=hashlib.sha256(data).hexdigest()
            if len(json.dumps(message,ensure_ascii=False))>20000: raise ValueError('Result too large; select a smaller range')
            return message
        finally:
            Path(snapshot).unlink(missing_ok=True)
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'ods_error','message':str(exc)[:1000]}}
