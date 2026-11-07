"""Create a formatted Writer document inside Pavlusha's shell sandbox."""
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
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK


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
        doc = desktop.loadComponentFromURL('private:factory/swriter', '_blank', 0, (prop('Hidden', True),))
        if doc is None:
            raise RuntimeError('Could not create Writer document')
        text = doc.Text
        cursor = text.createTextCursor()
        alignments = {'left':0, 'right':1, 'justify':2, 'center':3}
        spacing = uno.createUnoStruct('com.sun.star.style.LineSpacing')
        spacing.Mode = 0
        spacing.Height = 100
        for i, paragraph in enumerate(request['paragraphs']):
            if i:
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            cursor.ParaStyleName = 'Title' if paragraph.get('role') == 'title' else 'Standard'
            cursor.CharFontName = request['font']
            cursor.CharFontNameComplex = request['font']
            cursor.CharFontNameAsian = request['font']
            cursor.CharHeight = float(request['size'])
            cursor.CharHeightComplex = float(request['size'])
            cursor.CharHeightAsian = float(request['size'])
            cursor.CharWeight = 150.0 if paragraph.get('role') == 'title' else 100.0
            cursor.CharPosture = uno.Enum('com.sun.star.awt.FontSlant','NONE')
            cursor.ParaAdjust = alignments[paragraph.get('align', 'center' if paragraph.get('role') == 'title' else request['alignment'])]
            cursor.ParaLineSpacing = spacing
            cursor.ParaTopMargin = 0
            cursor.ParaBottomMargin = round(request['paragraph_after_pt'] * 2540 / 72)
            cursor.ParaLeftMargin = 0
            cursor.ParaRightMargin = 0
            cursor.ParaFirstLineIndent = 0 if paragraph.get('role') == 'title' else round(request['first_line_indent_cm'] * 1000)
            text.insertString(cursor, paragraph['text'], False)
        output = Path('/work') / request['output']
        temporary = output.with_name('.pavlusha-' + uuid.uuid4().hex + '.odt')
        try:
            doc.storeAsURL(temporary.as_uri(), (prop('FilterName', 'writer8'), prop('Overwrite', False)))
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return {'ok': True, 'path': request['output'], 'paragraphs': len(request['paragraphs']), 'font': request['font'], 'size_pt': request['size']}
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
