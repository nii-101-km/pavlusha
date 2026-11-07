"""Bounded, revision-based Writer append operations on ODT copies."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import tempfile
import uuid
from io import BytesIO
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree
from pavlusha_agent.sandbox import run_shell
from pavlusha_modules.file_paths import work_file as _file


def inspect_writer(root, path, revision=None):
    """Read top-level Writer structure through UNO from an isolated snapshot."""
    root=Path(root).resolve(strict=True)
    source=_file(root,path,'.odt')
    data=source.read_bytes()
    if len(data)>25_000_000: raise ValueError('Document exceeds 25 MB')
    actual=hashlib.sha256(data).hexdigest()
    if revision is not None and revision!=actual: raise ValueError('Document revision is stale')
    fd,snapshot=tempfile.mkstemp(prefix='.writer-inspect-',suffix='.odt',dir=root)
    try:
        with os.fdopen(fd,'wb') as stream: stream.write(data)
        request={'action':'inspect','snapshot':Path(snapshot).name}
        worker=Path(__file__).with_name('_writer_edit_sandbox_worker.py').read_bytes()
        runner='import base64;exec(base64.b64decode('+repr(base64.b64encode(worker).decode())+'))'
        command='/usr/bin/python3 -c '+shlex.quote(runner)+' '+shlex.quote(json.dumps(request,ensure_ascii=True))
        result=run_shell(root,command,network=False,timeout=60)
        if result.timed_out: raise ValueError('Writer timed out')
        try: message=json.loads(result.stdout)
        except ValueError: raise ValueError((result.stderr or 'Writer failed')[:1000])
        if result.exit_code!=0 or not message.get('ok'): raise ValueError(str(message.get('error','Writer failed'))[:1000])
        message.update(path=path,revision=actual)
        return message
    finally:
        Path(snapshot).unlink(missing_ok=True)


def replace_writer_text(root,path,output_path,revision,edits):
    if not isinstance(edits,list) or not 1<=len(edits)<=64:
        raise ValueError('Supply 1 to 64 text replacements')
    normalized=[]; seen=set()
    for edit in edits:
        if not isinstance(edit,dict) or set(edit)!={'node_id','old','new'}:
            raise ValueError('Each edit requires node_id, old, new')
        node_id,old,new=edit['node_id'],edit['old'],edit['new']
        if (not isinstance(node_id,str) or not node_id.startswith('paragraph:')
            or not node_id[10:].isdigit() or int(node_id[10:])<1
            or not isinstance(old,str) or not old or not isinstance(new,str)
            or len(old)+len(new)>16000 or any(c in old+new for c in '\n\r\t')):
            raise ValueError('Invalid paragraph or replacement')
        index=int(node_id[10:])-1
        if index in seen: raise ValueError('Only one edit per paragraph is supported')
        seen.add(index); normalized.append({'index':index,'old':old,'new':new})
    return _execute(root,path,output_path,revision,{'action':'replace_text','edits':normalized})


def _check_replace_target(data, index):
    ns={'office':'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
        'table':'urn:oasis:names:tc:opendocument:xmlns:table:1.0'}
    try:
        with ZipFile(BytesIO(data)) as package:
            entries=[e for e in package.infolist() if e.filename=='content.xml']
            if len(entries)!=1 or entries[0].file_size>15_000_000:
                raise ValueError('Invalid or oversized document content')
            body=ElementTree.fromstring(package.read(entries[0])).find('office:body/office:text',ns)
    except (BadZipFile,ElementTree.ParseError,KeyError) as exc:
        raise ValueError('Invalid ODT document body') from exc
    if body is None: raise ValueError('Missing document body')
    tables=body.findall('table:table',ns)
    if index>len(tables): raise ValueError('Table index not found')
    table=tables[index-1]
    span='{%s}'%ns['table']
    if (table.findall('.//table:covered-table-cell',ns) or table.findall('.//table:table',ns)
            or any(int(cell.get(span+'number-columns-spanned','1'))>1
                   or int(cell.get(span+'number-rows-spanned','1'))>1
                   for cell in table.findall('.//table:table-cell',ns))):
        raise ValueError('Merged, nested or irregular tables cannot be replaced safely')


def _execute(root, path, output_path, revision, request, image_path=None):
    root=Path(root).resolve(strict=True)
    source=_file(root,path,'.odt'); out=_file(root,output_path,'.odt')
    if out.exists(): raise ValueError('Output already exists')
    if not out.parent.is_dir(): raise ValueError('Output parent directory does not exist')
    data=source.read_bytes()
    if len(data)>25_000_000: raise ValueError('Document exceeds 25 MB')
    actual=hashlib.sha256(data).hexdigest()
    if actual!=revision: raise ValueError('Document revision is stale')
    if request['action']=='replace_table': _check_replace_target(data,request['table_index'])
    image_data=None
    if image_path is not None:
        if not isinstance(image_path,str): raise ValueError('Invalid image path')
        suffix=Path(image_path).suffix.lower()
        if suffix not in ('.png','.jpg','.jpeg'): raise ValueError('Image must be PNG or JPEG')
        image_data=_file(root,image_path,suffix).read_bytes()
        if len(image_data)>15_000_000: raise ValueError('Image exceeds 15 MB')
    token=uuid.uuid4().hex; tmp_output=root/('.writer-'+token+'.odt'); snapshots=[]
    try:
        fd,path_tmp=tempfile.mkstemp(prefix='.writer-source-',suffix='.odt',dir=root)
        snapshots.append(Path(path_tmp))
        with os.fdopen(fd,'wb') as stream: stream.write(data)
        request['snapshot']=Path(path_tmp).name
        if image_data is not None:
            fd,path_img=tempfile.mkstemp(prefix='.writer-image-',suffix=suffix,dir=root)
            snapshots.append(Path(path_img))
            with os.fdopen(fd,'wb') as stream: stream.write(image_data)
            request['image_snapshot']=Path(path_img).name
        request['temp_output']=tmp_output.name
        worker=Path(__file__).with_name('_writer_edit_sandbox_worker.py').read_bytes()
        runner='import base64;exec(base64.b64decode('+repr(base64.b64encode(worker).decode())+'))'
        command='/usr/bin/python3 -c '+shlex.quote(runner)+' '+shlex.quote(json.dumps(request,ensure_ascii=True))
        result=run_shell(root,command,network=False,timeout=60)
        if result.timed_out: raise ValueError('Writer timed out')
        try: message=json.loads(result.stdout)
        except ValueError: raise ValueError((result.stderr or 'Writer failed')[:1000])
        if result.exit_code!=0 or not message.get('ok'): raise ValueError(str(message.get('error','Writer failed'))[:1000])
        os.link(tmp_output,out)
        message.update(path=output_path,source_revision=actual,revision=hashlib.sha256(out.read_bytes()).hexdigest())
        return message
    finally:
        for snapshot in snapshots: snapshot.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)


def _check_anchor(value):
    if value is not None and (not isinstance(value,str) or not 1<=len(value)<=8000 or '\0' in value or '\n' in value or '\r' in value):
        raise ValueError('Anchor must be the exact text of one nonempty top-level paragraph')


def _check_rows(rows,header,alignment):
    if (not isinstance(rows,list) or not 1<=len(rows)<=50 or not isinstance(rows[0],list)
            or not 1<=len(rows[0])<=10 or any(not isinstance(row,list) or len(row)!=len(rows[0]) for row in rows)
            or any(not isinstance(c,str) or len(c)>1000 or '\0' in c for row in rows for c in row)
            or len(json.dumps(rows,ensure_ascii=True))>60000
            or header.__class__ is not bool or alignment not in ('left','center','right')):
        raise ValueError('Supply 1..50 equal-length rows with 1..10 text cells and valid formatting')


def add_table(root,path,output_path,revision,rows,header=True,alignment='left',caption='',after_text=None):
    try:
        _check_rows(rows,header,alignment);_check_anchor(after_text)
        if not isinstance(caption,str) or len(caption)>300 or '\0' in caption: raise ValueError('Invalid caption')
        return _execute(root,path,output_path,revision,{'action':'table','rows':rows,'header':header,'alignment':alignment,'caption':caption,'after_text':after_text})
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}


def add_image(root,path,output_path,revision,image_path,width_cm=12,alignment='center',caption='',alt_text='',after_text=None):
    try:
        _check_anchor(after_text)
        if (type(width_cm) not in (int,float) or not math.isfinite(width_cm) or not 1<=width_cm<=16
                or alignment not in ('left','center','right')
                or any(not isinstance(s,str) or len(s)>300 or '\0' in s for s in (caption,alt_text))):
            raise ValueError('Invalid image width, alignment, caption or alternative text')
        return _execute(root,path,output_path,revision,{'action':'image','width_cm':width_cm,
            'alignment':alignment,'caption':caption,'alt_text':alt_text,'after_text':after_text},image_path=image_path)
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}


def replace_table(root,path,output_path,revision,table_index,rows,header=True,alignment='left'):
    try:
        _check_rows(rows,header,alignment)
        if type(table_index) is not int or not 1<=table_index<=200: raise ValueError('Invalid table index')
        return _execute(root,path,output_path,revision,{'action':'replace_table','table_index':table_index,
            'rows':rows,'header':header,'alignment':alignment})
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}


def insert_paragraph(root,path,output_path,revision,text,after_text=None,role='body',alignment='justify'):
    try:
        _check_anchor(after_text);_check_anchor(text)
        if role not in ('body','title') or alignment not in ('left','right','center','justify'): raise ValueError('Invalid formatting')
        return _execute(root,path,output_path,revision,{'action':'paragraph','text':text,'after_text':after_text,'role':role,'alignment':alignment})
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}


def insert_list(root,path,output_path,revision,items,kind='bullet',after_text=None,alignment='left'):
    try:
        _check_anchor(after_text)
        if not isinstance(items,list) or not 1<=len(items)<=50 or kind not in ('bullet','numbered') or alignment not in ('left','right','center','justify'):
            raise ValueError('Invalid list settings')
        for item in items:
            if item is None: raise ValueError('List items must be nonempty text')
            _check_anchor(item)
        if len(json.dumps(items,ensure_ascii=True))>60000: raise ValueError('List is too large')
        return _execute(root,path,output_path,revision,{'action':'list','items':items,'kind':kind,'after_text':after_text,'alignment':alignment})
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}


def format_paragraph(root,path,output_path,revision,match_text,alignment=None,role=None):
    try:
        _check_anchor(match_text)
        if match_text is None or (alignment is None and role is None) or alignment not in (None,'left','right','center','justify') or role not in (None,'body','title'):
            raise ValueError('Supply paragraph text and alignment or role')
        return _execute(root,path,output_path,revision,{'action':'format','match_text':match_text,'alignment':alignment,'role':role})
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}


def normalize_document(root,path,output_path,revision,font='Liberation Serif',size=14,alignment='justify'):
    try:
        if (not isinstance(font,str) or not 1<=len(font)<=100 or '\0' in font
                or type(size) not in (int,float) or not math.isfinite(size) or not 8<=size<=72
                or alignment not in ('left','right','center','justify')):
            raise ValueError('Invalid document font, size or alignment')
        return _execute(root,path,output_path,revision,{'action':'normalize','font':font,'size':size,'alignment':alignment})
    except (OSError,ValueError) as exc:
        return {'ok':False,'error':{'code':'writer_error','message':str(exc)[:1000]}}
