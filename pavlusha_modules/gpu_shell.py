"""Run one sandboxed workload while the selected Worker is released."""
from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.extensions import Contribution, ExecutionPolicy
from .util import declare, TEXT, BOOL, INTEGER


class GpuShell:
    name='gpu_shell'
    def __init__(self, root, *, network=False, timeout=120, output_limit=24000):
        self.host=BasicHost(root,network=network,timeout=timeout,output_limit=output_limit)
        self.allow_network=network
    def on_event(self,event):
        if event.kind == 'session_started':
            return Contribution(message='To unload the Worker for a command, call the separate action '
                '{\"action\":\"shell_released\",\"command\":\"YOUR COMMAND\"}. '
                'The shell action never unloads the Worker: its release_worker field is fixed false. '
                'Set gpu=true on shell_released when the command requires GPU access; network is separately granted.', tools=(
                declare('shell_released','Run a sandboxed command with the Worker model temporarily unloaded.',
                    {'command':TEXT,'network':BOOL if self.allow_network else {'const':False},'gpu':BOOL,'timeout':INTEGER},('command',),
                    self.host.shell, execution=ExecutionPolicy(release_model=True)),))


def create_extension(*, settings, context):
    if settings: raise ValueError('GpuShell accepts no settings')
    return GpuShell(context['workdir'],network=context.get('network',False),
                    timeout=context.get('command_timeout',120),output_limit=context.get('output_limit',24000))
