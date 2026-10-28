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
            return Contribution(message='Use the private GUI tools; screenshots are transient observations. '
                'GUI network permission: '+str(self.allow_network),tools=tools)
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
