"""Request a human response at the kernel's durable pause boundary."""
from pavlusha_agent.extensions import Contribution, Outcome, Control
from .util import declare, TEXT


class Interactive:
    name='interactive'
    def bind_interaction(self, interaction):
        self.interaction = interaction

    def on_event(self,event):
        if event.kind == 'key_pressed':
            key=event.data['key']
            if key == 'ctrl_z':
                self.interaction.request_pause(urgent=False)
            elif key == 'ctrl_backslash':
                self.interaction.request_pause(urgent=True)
            elif key in ('ctrl_c','eof'):
                self.interaction.request_pause(urgent=True)
            return None
        if event.kind == 'paused':
            if event.data.get('key') in ('ctrl_c','eof'):
                self.interaction.resume(None)
                return None
            text=self.interaction.read_user_input(event.data.get('reason',''),
                                                  require_message=event.data.get('require_message',False))
            self.interaction.resume(text)
            return None
        if event.kind == 'session_started':
            return Contribution(prefix={'instructions':'Use ask_user only when a user decision is required. The run pauses and resumes on their answer. '
                'Example: {"action":"ask_user","question":"Please review the result and confirm acceptance."}'},
                tools=(declare('ask_user','Pause this session and ask the user a question.',
                    {'question':TEXT},('question',),
                    lambda args,ctx: Outcome({'question':args['question']},
                        controls=(Control('pause',args['question']),))),))


def create_extension(*, settings, context):
    if settings: raise ValueError('Interactive accepts no settings')
    return Interactive()
