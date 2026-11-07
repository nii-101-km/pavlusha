"""Executed inside Pavlusha's shell sandbox, not on the controller host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def main(request):
    pipe = 'pavlusha_' + uuid.uuid4().hex
    profile = Path('/tmp/profile-' + uuid.uuid4().hex)
    profile.mkdir()
    proc = subprocess.Popen(['/usr/bin/soffice', '-env:UserInstallation=' + profile.as_uri(),
        '--headless', '--norestore', '--nodefault', '--nofirststartwizard',
        '--accept=pipe,name=' + pipe + ';urp;StarOffice.ComponentContext'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    doc = None
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
        for _ in range(150):
            try:
                ctx = resolver.resolve('uno:pipe,name=' + pipe + ';urp;StarOffice.ComponentContext')
                break
            except NoConnectException:
                if proc.poll() is not None:
                    raise RuntimeError('LibreOffice failed to start')
                time.sleep(.1)
        else:
            raise RuntimeError('LibreOffice startup timed out')
        desktop = ctx.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', ctx)
        doc = desktop.loadComponentFromURL('private:factory/scalc', '_blank', 0, (prop('Hidden', True),))
        if doc is None:
            raise RuntimeError('Could not create Calc workbook')
        libraries = doc.BasicLibraries
        if not libraries.hasByName('Standard'):
            libraries.createLibrary('Standard')
        if not libraries.isLibraryLoaded('Standard'):
            libraries.loadLibrary('Standard')
        library = libraries.getByName('Standard')
        library.insertByName('Pavlusha', request['code'])
        script = doc.getScriptProvider().getScript('vnd.sun.star.script:Standard.Pavlusha.Main?language=Basic&location=document')
        script.invoke((), (), ())
        doc.calculateAll()
        output = Path('/work') / request['output']
        temporary = output.with_name('.pavlusha-' + uuid.uuid4().hex + '.ods')
        try:
            doc.storeAsURL(temporary.as_uri(), (prop('FilterName', 'calc8'), prop('Overwrite', False)))
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return {'ok': True, 'path': request['output'], 'format': 'ods', 'macro': 'Standard.Pavlusha.Main'}
    finally:
        if doc is not None:
            try:
                doc.close(True)
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == '__main__':
    try:
        print(json.dumps(main(json.loads(sys.argv[1])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
