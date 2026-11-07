"""Render office documents through an isolated LibreOffice and Poppler."""
import hashlib
import ctypes
import errno
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile

from pavlusha_agent.sandbox import run_shell
from pavlusha_modules.file_paths import work_file as _file


def render(root, path, revision, output_dir, *, suffix, dpi=120, timeout_seconds=120):
    root=Path(root).resolve(strict=True)
    source=_file(root,path,suffix)
    if not isinstance(output_dir,str) or not output_dir:
        raise ValueError('Output directory must be inside workdir')
    relative=Path(output_dir)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Output directory must be inside workdir')
    target=(root/relative).resolve()
    if target==root or not target.is_relative_to(root) or target.exists() or (root/relative).is_symlink():
        raise ValueError('Output directory must be a new directory inside workdir')
    if not target.parent.is_dir(): raise ValueError('Output parent directory does not exist')
    if type(dpi) is not int or not 72<=dpi<=200 or type(timeout_seconds) is not int or not 1<=timeout_seconds<=180:
        raise ValueError('Invalid render resolution or timeout')
    data=source.read_bytes()
    if len(data)>25_000_000: raise ValueError('Document exceeds 25 MB')
    actual=hashlib.sha256(data).hexdigest()
    if actual!=revision: raise ValueError('Document revision is stale')
    staging=Path(tempfile.mkdtemp(prefix='.office-render-',dir=root))
    try:
        snapshot=staging/('source'+suffix);snapshot.write_bytes(data)
        (staging/'pages').mkdir()
        stage='/work/'+staging.name
        # An isolated profile prevents interference with an open desktop session.
        command=('/usr/bin/soffice -env:UserInstallation=file:///tmp/office-render-profile '
                 '--headless --norestore --nodefault --nofirststartwizard --convert-to pdf --outdir '
                 +shlex.quote(stage)+' '+shlex.quote(stage+'/source'+suffix)
                 +' && pdftoppm -png -r '+str(dpi)+' '+shlex.quote(stage+'/source.pdf')
                 +' '+shlex.quote(stage+'/pages/page'))
        result=run_shell(root,command,network=False,timeout=timeout_seconds)
        if result.timed_out: raise ValueError('Render timed out')
        try: pages=sorted((staging/'pages').glob('page-*.png'),key=lambda p:int(p.stem.split('-')[-1]))
        except ValueError as exc: raise ValueError('Invalid page filenames') from exc
        if result.exit_code or not pages: raise ValueError('Render failed: '+(result.stderr or result.stdout)[-1000:])
        if len(pages)>200 or not (staging/'source.pdf').read_bytes().startswith(b'%PDF-'):
            raise ValueError('Invalid PDF or too many pages')
        from PIL import Image
        for page in pages:
            with Image.open(page) as img: img.verify()
        manifest={'source_revision':actual,'page_count':len(pages),'dpi':dpi,
                  'pdf':'source.pdf','pages':['pages/'+p.name for p in pages],
                  'visual_verification':'not_performed'}
        (staging/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
        snapshot.unlink()
        # Rename only after all results exist; never replace an existing destination.
        libc=ctypes.CDLL(None,use_errno=True)
        rename=getattr(libc,'renameat2',None)
        if rename is None: raise ValueError('Atomic publication requires renameat2')
        rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        if rename(-100,os.fsencode(staging),-100,os.fsencode(target),1):
            code=ctypes.get_errno()
            if code==errno.EEXIST: raise ValueError('Output directory already exists')
            raise OSError(code,os.strerror(code))
        return {'ok':True,'revision':actual,'page_count':len(pages),'output_dir':output_dir,
                'manifest_path':str(relative/'manifest.json'),
                'pdf':str(relative/'source.pdf'),
                'pages':[str(relative/'pages'/p.name) for p in pages]}
    finally:
        if staging.exists(): shutil.rmtree(staging)
