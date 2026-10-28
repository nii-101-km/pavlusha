"""Shell-only composition of the existing host; another module owns finish policy."""
from dataclasses import replace
from pavlusha_agent.basic_host import BasicHost


class Shell:
    name = 'shell'

    def __init__(self, workdir, **options):
        self.host = BasicHost(workdir, **options)

    def on_event(self, event):
        contribution = self.host.on_event(event)
        if contribution is None:
            return None
        return replace(contribution, tools=tuple(tool for tool in contribution.tools if tool.name == 'shell'))


def create_extension(*, settings, context):
    if settings:
        raise ValueError('Shell accepts no settings; permissions are supplied by the application')
    return Shell(context['workdir'], network=context.get('network', False),
                 timeout=context.get('command_timeout', 120),
                 output_limit=context.get('output_limit', 24000))
