"""Minimal host operations for bare dogfood; no project planning or checkpoint rules."""
from .extensions import Contribution, Outcome, Tool, Control
from .sandbox import admit_shell_result, run_shell


class BasicHost:
    name = 'host'

    def __init__(self, workdir, *, network=False, timeout=120, output_limit=24000, shell_runner=run_shell):
        self.workdir = workdir
        self.network = network
        self.timeout = timeout
        self.output_limit = output_limit
        self.shell_runner = shell_runner

    def on_event(self, event):
        if event.kind != 'session_started':
            return None
        return Contribution(tools=(
            Tool('shell', 'Run one bounded foreground shell command in /work.', {
                'type': 'object', 'properties': {
                    'command': {'type': 'string', 'minLength': 1},
                    'network': {'type': 'boolean'}, 'gpu': {'type': 'boolean'},
                    'release_worker': {'type': 'boolean', 'const': False},
                    'timeout': {'type': 'integer', 'minimum': 1}},
                'required': ['command'], 'additionalProperties': False}, self.shell),
            Tool('finish', 'Finish this task and report results.', {
                'type': 'object', 'properties': {'summary': {'type': 'string', 'minLength': 1}},
                'required': ['summary'], 'additionalProperties': False},
                lambda args, context: Outcome({'status': 'finished', 'summary': args['summary']}, controls=(Control('stop', args['summary'], finished=True),))),
        ))

    def shell(self, args, context):
        if args.get('network', False) and not self.network:
            return Outcome({'error': 'network_not_granted'})
        options = {'network': args.get('network', False),
                   'timeout': min(args.get('timeout', self.timeout), self.timeout)}
        if args.get('gpu', False):
            options['gpu'] = True
        result = self.shell_runner(self.workdir, args['command'], **options)
        return Outcome(admit_shell_result(result, self.output_limit))
