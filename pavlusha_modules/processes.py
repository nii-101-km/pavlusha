"""Foreground sandboxed processes owned and cleaned up by the module."""
from pathlib import Path
from pavlusha_agent.extensions import Contribution, Outcome
from .bundled._processes import ProcessManager, validate_process_action
from .util import declare, TEXT, BOOL, INTEGER


class Processes:
    name='processes'
    def __init__(self, workdir, allow_network=False, output_limit=8000):
        self.workdir=Path(workdir); self.allow_network=allow_network
        self.output_limit=output_limit; self.manager=None
    def on_event(self, event):
        if event.kind == 'session_started':
            self.manager = ProcessManager(self.workdir, allow_network=self.allow_network)
            specs = (
                ('process_start', 'Start a foreground service across turns.',
                 {'command':TEXT,'network':BOOL,'lifetime_seconds':INTEGER}, ('command',)),
                ('process_status', 'Check an owned process.', {'process_id':TEXT}, ('process_id',)),
                ('process_logs', 'Read bounded logs of an owned process.',
                 {'process_id':TEXT,'limit':INTEGER}, ('process_id',)),
                ('process_stop', 'Stop an owned process and its children.', {'process_id':TEXT}, ('process_id',)))
            tools=tuple(declare(name, desc, props, req,
                lambda args, ctx, kind=name: self.call(kind,args)) for name,desc,props,req in specs)
            instructions = ('PROCESS ACTIONS: Use these direct JSON actions when available; '
                'do not translate them into shell commands:\n'
                '{"action":"process_start","command":"exec python3 -u -m http.server 8890 --bind 127.0.0.1","network":false,"lifetime_seconds":3600}\n'
                '{"action":"process_status","process_id":"P_RETURNED_ID"}\n'
                '{"action":"process_logs","process_id":"P_RETURNED_ID","limit":4000}\n'
                '{"action":"process_stop","process_id":"P_RETURNED_ID"}\n'
                'Replace P_RETURNED_ID with the ID returned by process_start. Processes persist '
                'across turns within this session, not across restarts. Use foreground commands; '
                'a returned ID does not prove readiness: check status and logs. If an owned '
                'server must be restarted, stop its process ID and start a fresh process. '
                'An isolated process has a separate network from an isolated GUI; '
                'to inspect a local server without host network, start server and browser together in gui_start. '
                'Process network permission: '+str(self.allow_network)+'. '
                + ('process_start may opt into host network with network=true when needed.'
                   if self.allow_network else 'Use network=false; host network is not granted.'))
            return Contribution(prefix={'instructions': instructions}, tools=tools)
        if event.kind == 'session_stopped' and self.manager is not None:
            self.manager.close()
            self.manager=None
    def call(self, kind, args):
        _, values=validate_process_action({'action':kind,**args})
        return Outcome(self.manager.call(kind,values,output_limit=self.output_limit))


def create_extension(*, settings, context):
    if set(settings)-{'allow_network'} or type(settings.get('allow_network',False)) is not bool:
        raise ValueError('Processes accepts boolean allow_network only')
    return Processes(context['workdir'],settings.get('allow_network',False),context.get('output_limit',8000))
