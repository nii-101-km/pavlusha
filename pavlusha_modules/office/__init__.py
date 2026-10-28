"""Fixed built-in DOCX and XLSX operations; no arbitrary function loader."""
import importlib.util
from pathlib import Path
from pavlusha_agent.extensions import Contribution, Outcome
from pavlusha_modules.util import declare, TEXT, STRING, INTEGER


class Office:
    name='office'
    def __init__(self, root, output_limit):
        self.root=root; self.output_limit=output_limit; self.modules={}
    def on_event(self,event):
        if event.kind=='session_started':
            # Isolate the legacy implementation's module globals per extension instance.
            for kind in ('docx','xlsx'):
                spec=importlib.util.spec_from_file_location('_pavlusha_builtin_'+kind,
                    Path(__file__).with_name(kind+'_impl.py'))
                impl=importlib.util.module_from_spec(spec); spec.loader.exec_module(impl)
                self.modules[kind]=impl
                impl._PAVLUSHA_SETUP({}, {'workdir':self.root,'output_limit':self.output_limit})
            tools=[]; unavailable=[]
            for kind,impl in self.modules.items():
                if impl._ET is None or (kind=='xlsx' and impl._xl is None):
                    unavailable.append(kind.upper()+' requires lxml'+(' and openpyxl' if kind=='xlsx' else ''))
                    continue
                for operation in ('inspect','read','edit','render'):
                    name=kind+'_'+operation; props,required=self.schema(kind,operation)
                    func=getattr(impl,name)
                    tools.append(declare(name,func.__doc__,props,required,
                        lambda args,ctx,fn=func:Outcome(fn(**args))))
            return Contribution(message='Built-in DOCX and XLSX tools read and edit files within the work directory. '
                'Render requires LibreOffice/Poppler; inspect generated page images. '+('; '.join(unavailable)),tools=tuple(tools))
        if event.kind=='session_stopped':
            for impl in self.modules.values(): impl._PAVLUSHA_CLOSE()
            self.modules={}
    @staticmethod
    def schema(kind, operation):
        if operation=='inspect':
            props={'path':TEXT,'cursor':STRING,'limit':INTEGER}
            if kind=='xlsx': props['sheet']=STRING
            return props,('path',)
        if operation=='read':
            props={'path':TEXT,'revision':TEXT,'cursor':STRING,'max_chars':INTEGER}
            if kind=='xlsx': props.update(sheet=TEXT,cell_range=TEXT); req=('path','revision','sheet','cell_range')
            else: props.update(node_ids={'type':'array','items':TEXT},contains=STRING); req=('path','revision')
            return props,req
        if operation=='edit':
            return {'path':TEXT,'output_path':TEXT,'revision':TEXT,
                    'edits':{'type':'array','items':{'type':'object'}}},('path','output_path','revision','edits')
        return {'path':TEXT,'revision':TEXT,'output_dir':TEXT,'dpi':INTEGER,'timeout_seconds':INTEGER},('path','revision','output_dir')


def create_extension(*, settings, context):
    if settings: raise ValueError('Office accepts no settings')
    return Office(context['workdir'],context.get('output_limit',24000))
