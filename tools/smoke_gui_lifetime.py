"""Real process lifetime evidence in the existing private Xvfb/bubblewrap contour."""
import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pavlusha_agent.gui import GuiRuntime
from pavlusha_agent.sandbox import run_shell


def snapshot(roots):
    rows = []
    for line in subprocess.check_output(['ps','-eo','pid,ppid,pgid,sid,stat,args'],text=True).splitlines()[1:]:
        fields = line.split(None,5)
        if len(fields)==6:
            rows.append(dict(zip(['pid','ppid','pgid','sid','stat','command'],fields)))
    owned = set(map(str, roots))
    while True:
        new = {r['pid'] for r in rows if r['ppid'] in owned}
        if new <= owned: break
        owned |= new
    return [r for r in rows if r['pid'] in owned]


def alive(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return stat[0] != 'Z'
    except FileNotFoundError: return False


def run(output, *, before=False, idle=125, ocr_work=None):
    output = Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    report = {'mode':'before' if before else 'after','idle_seconds':idle,'actions':[],'signals':[]}
    with tempfile.TemporaryDirectory(prefix='pavlusha-gui-lifetime-') as tmp:
        work = Path(tmp)
        if ocr_work:
            ocr_work = Path(ocr_work).resolve()
            report['app_sha256_before'] = hashlib.sha256((ocr_work/'app.py').read_bytes()).hexdigest()
            (work/'books').mkdir()
            for book in (ocr_work/'books').glob('*/book.json'):
                directory = work/'books'/book.parent.name
                directory.mkdir()
                shutil.copyfile(book,directory/'book.json')
            server_command = 'GLM_OCR_BOOKS_DIR=/work/books /ocr/.venv/bin/python -B /ocr/app.py --no-load'
        else:
            (work/'fixture.py').write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers()
  self.wfile.write(b"<h1>SESSION LIFETIME FIXTURE</h1><input style='position:absolute;left:80px;top:120px;font-size:20px' id='text'><button style='position:absolute;left:80px;top:180px;font-size:20px' onclick=\\"fetch('/',{method:'POST',body:document.getElementById('text').value})\\">Save</button>")
 def do_POST(self):
  Path('/work/typed.txt').write_bytes(self.rfile.read(int(self.headers['Content-Length'])))
  self.send_response(200); self.end_headers()
HTTPServer(('127.0.0.1',8000),Handler).serve_forever()
''')
            server_command = '/usr/bin/python3 -B /work/fixture.py'
        (work/'ready.py').write_text("""import time, urllib.request
end = time.monotonic()+60
while True:
 try:
  urllib.request.urlopen('http://127.0.0.1:8000',timeout=1).close()
  break
 except OSError:
  if time.monotonic()>end: raise
  time.sleep(.2)
""")
        command = server_command + ' > /work/server.log 2>&1 & /usr/bin/python3 /work/ready.py; exec pavlusha-browser http://127.0.0.1:8000'
        original = GuiRuntime._bwrap_argv
        def argv(gui,command,*,network):
            args = original(gui,command,network=network)
            if ocr_work:
                at = args.index('/bin/bash')
                args[at:at] = ['--ro-bind',str(ocr_work),'/ocr']
            return args
        original_killpg = os.killpg
        def killpg(pgid,sig):
            report['signals'].append({'at':time.monotonic(),'pgid':pgid,'signal':int(sig),
                                      'stack':'GuiRuntime._terminate_process -> os.killpg'})
            return original_killpg(pgid,sig)
        with patch.object(GuiRuntime,'_bwrap_argv',argv), patch('pavlusha_agent.gui.os.killpg',killpg):
            with GuiRuntime(work,output,network_allowed=False) as gui:
                result,obs = gui.start({'command':command,'network':False,'delay':10 if ocr_work else 5})
                report['actions'].append(result)
                assert result['state']=='alive' and obs is not None,result
                if ocr_work:
                    deadline = time.monotonic()+60
                    while '"GET /api/books HTTP/1.1" 200' not in (work/'server.log').read_text():
                        if time.monotonic()>deadline:
                            raise AssertionError((work/'server.log').read_text())
                        result,obs = gui.execute('view_gui',{'delay':1})
                        assert result['state']=='alive' and obs is not None,result
                (output/'start.png').write_bytes(obs.clean_png)
                process,xserver,socket = gui.process,gui.display.server,gui.display.socket_path
                roots = [process.pid,xserver.pid]
                report['start_processes'] = snapshot(roots)
                owned = [int(r['pid']) for r in report['start_processes']]
                essential = [int(r['pid']) for r in report['start_processes'] if
                             (r['command'].startswith('/ocr/.venv/bin/python -B /ocr/app.py') or
                              r['command'].startswith('/usr/bin/python3 -B /work/fixture.py') or
                              r['command'].startswith('/usr/bin/epiphany ')) and r['pid'] not in map(str,roots)]
                report['essential_original_pids'] = essential
                report['gui_pid'] = process.pid
                report['xvfb_pid'] = xserver.pid
                started = time.monotonic()
                for delay in [idle/2,idle/2]:
                    time.sleep(delay)
                    report.setdefault('idle_processes',[]).append(snapshot(roots))
                    if not before:
                        result,obs = gui.execute('view_gui',{'delay':0})
                        report['actions'].append(result)
                        assert result['state']=='alive' and obs is not None,result
                        assert gui.process is process and gui.display.server is xserver
                        shell = run_shell(work,"printf 'intervening shell'; test -s /work/server.log",network=False,timeout=3)
                        assert shell.exit_code==0,shell
                        report.setdefault('shell_turns',[]).append(shell.exit_code)
                report['elapsed_since_start_return'] = time.monotonic()-started
                result,obs = gui.execute('view_gui',{'delay':0})
                report['actions'].append(result)
                if before:
                    assert result['state']=='deadline' and obs is None,result
                    report['outer_exit_code'] = process.poll()
                    report['after_deadline_processes'] = snapshot(roots)
                    report['xvfb_survived_deadline'] = xserver.poll() is None
                    report['live_before_expired_poll'] = report['idle_processes'][-1]
                else:
                    assert result['state']=='alive' and obs is not None,result
                    (output/'after-idle.png').write_bytes(obs.clean_png)
                    # Coordinates selected from the fixture/OCR screenshots, no DOM targeting.
                    x,y = (300,225) if ocr_work else (150,200)
                    result,obs = gui.execute('click',{'x':x,'y':y,'delay':.3})
                    report['actions'].append(result)
                    assert obs is not None and obs.gesture['action']=='click',result
                    (output/'click.png').write_bytes(obs.observation_png)
                    text = 'AbC_XyZ-123'
                    result,obs = gui.execute('type_text',{'text':text,'delay':.3})
                    report['actions'].append(result)
                    assert result['state']=='alive',result
                    (output/'typed.png').write_bytes(obs.clean_png)
                    if not ocr_work:
                        result,obs = gui.execute('click',{'x':110,'y':255,'delay':.5})
                        report['actions'].append(result)
                        assert (work/'typed.txt').read_text()==text
                        report['actual_textbox_value'] = (work/'typed.txt').read_text()
                    assert gui.process is process and process.poll() is None and xserver.poll() is None
                    assert essential and all(alive(p) for p in essential),essential
                    report['after_actions_processes'] = snapshot(roots)
                    assert '"GET /api/books HTTP/1.1" 200' in (work/'server.log').read_text() if ocr_work else True
                owned = sorted(set(owned + [int(r['pid']) for r in snapshot(roots)]))
                report['tracked_cleanup_pids'] = owned
                result,obs = gui.execute('gui_close',{'delay':0})
                report['actions'].append(result)
                assert process.poll() is not None and xserver.poll() is not None and not socket.exists()
                deadline = time.monotonic()+3
                while any(alive(p) for p in owned) and time.monotonic()<deadline: time.sleep(.1)
                report['owned_live_after_close'] = [p for p in owned if alive(p)]
                assert not report['owned_live_after_close'],report
                report['server_log'] = (work/'server.log').read_text()
                if not before:
                    for cycle in range(2):
                        result,obs = gui.start({'command':command,'network':False,'delay':10 if ocr_work else 5})
                        assert result['state']=='alive' and obs is not None,result
                        process,xserver,socket = gui.process,gui.display.server,gui.display.socket_path
                        pids = [int(r['pid']) for r in snapshot([process.pid,xserver.pid])]
                        result,_ = gui.execute('gui_close',{'delay':0})
                        assert result['state']=='closed' and not socket.exists()
                        deadline=time.monotonic()+3
                        while any(alive(p) for p in pids) and time.monotonic()<deadline: time.sleep(.1)
                        assert not any(alive(p) for p in pids),pids
                    report['restart_cleanup_cycles'] = 2
        if ocr_work:
            report['app_sha256_after'] = hashlib.sha256((ocr_work/'app.py').read_bytes()).hexdigest()
            assert report['app_sha256_after']==report['app_sha256_before']
    report['result'] = 'REPRODUCED' if before else 'PASS'
    (output/'lifetime.json').write_text(json.dumps(report,indent=2)+'\n')
    print(report['result'],output/'lifetime.json',flush=True)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',action='store_true')
    parser.add_argument('--idle',type=float,default=125)
    parser.add_argument('--ocr-work',type=Path)
    parser.add_argument('--output',type=Path,default=Path('gui-lifetime-results'))
    args=parser.parse_args()
    run(args.output,before=args.before,idle=args.idle,ocr_work=args.ocr_work)
