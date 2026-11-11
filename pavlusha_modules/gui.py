"""Private-display tools and screenshots exposed through the event interface."""
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from pavlusha_agent.extensions import Contribution, Observation, Outcome
from .bundled._gui import GuiRuntime, validate_gui_action
from .util import declare, TEXT, STRING, BOOL, INTEGER, NUMBER


class Gui:
    name='gui'
    def __init__(self, workdir, allow_network=False):
        self.workdir=Path(workdir); self.allow_network=allow_network; self.private=None; self.runtime=None
    def on_event(self,event):
        if event.kind == 'session_started':
            self.private=TemporaryDirectory(prefix='pavlusha-gui-')
            self.runtime=GuiRuntime(self.workdir,Path(self.private.name),network_allowed=self.allow_network)
            delay={'delay':NUMBER}; coord={'x':INTEGER,'y':INTEGER,**delay}
            specs=[('gui_start',{'command':TEXT,'network':BOOL,**delay},('command',)),
                ('view_gui',delay,()), ('click',coord,()),('double_click',coord,()),('right_click',coord,()),
                ('drag',{'x1':INTEGER,'y1':INTEGER,'x2':INTEGER,'y2':INTEGER,**delay},('x1','y1','x2','y2')),
                ('type_text',{'text':STRING,**delay},('text',)),
                ('press_key',{'key':TEXT,'modifiers':{'type':'array','items':TEXT},**delay},('key',)),
                ('hold_key',{'key':TEXT,'duration':NUMBER,**delay},('key','duration')),
                ('gui_close',delay,())]
            tools=tuple(declare(name,'Private GUI action '+name,props,
                ('x','y') if name in ('click','double_click','right_click') else required,
                lambda args,ctx,kind=name:self.call(kind,args)) for name,props,required in specs)
            instructions = (
                'PRIVATE GUI: The GUI uses its own 800x600 display in the /work sandbox; '
                'screenshots are transient observations. Use gui_start to start an application, '
                'view_gui to observe, and gui_close when finished. For a web UI use '
                'pavlusha-browser URL in gui_start. With network=false, the GUI has its own '
                'loopback: a server started by shell or process_start is not reachable there. '
                'Start the server and browser in the same gui_start command, for example '
                '"python3 -u server.py --host 127.0.0.1 --port 8000 > /tmp/server.log 2>&1 & '
                'exec pavlusha-browser http://127.0.0.1:8000/"; adapt the server command '
                'to the application. Keep the browser in the foreground. '
                'network=true uses the host network and requires the GUI network permission; '
                'it is not limited to localhost. GUI network permission: ' + str(self.allow_network) + '. '
                'If browser startup fails, report the error; do not guess a replacement browser.')
            return Contribution(prefix={'instructions': instructions}, tools=tools)
        if event.kind == 'session_stopped':
            if self.runtime is not None:
                self.runtime.close()
                self.runtime=None
            if self.private is not None:
                self.private.cleanup()
                self.private=None
    def call(self, kind, args):
        _, data=validate_gui_action({'action':kind,**args})
        result, image=self.runtime.execute(kind,data)
        if image is None: return Outcome(result)
        url='data:image/png;base64,'+base64.b64encode(image.observation_png).decode('ascii')
        return Outcome(result,observations=(Observation(text=image.message()['content'][0]['text'],image=url),))


def create_extension(*, settings, context):
    if set(settings)-{'allow_network'} or type(settings.get('allow_network',False)) is not bool:
        raise ValueError('Gui accepts boolean allow_network only')
    return Gui(context['workdir'],settings.get('allow_network',False))
