"""A small, optional owner of the ordinary finish action."""
from pavlusha_agent.extensions import Contribution, Control, Outcome
from .util import declare, TEXT


class Finish:
    name = 'finish'

    def on_event(self, event):
        if event.kind == 'session_started':
            return Contribution(tools=(declare(
                'finish', 'Finish the task and report what was done and verified.',
                {'summary': TEXT}, ('summary',), self.finish),))

    def finish(self, args, context):
        summary = args['summary'].strip()
        if not summary:
            return Outcome({'error': 'summary must not be blank'})
        return Outcome({'status': 'finished', 'summary': summary},
                       controls=(Control('stop', summary, finished=True),))


def create_extension(*, settings, context):
    if settings:
        raise ValueError('Finish accepts no settings')
    return Finish()
