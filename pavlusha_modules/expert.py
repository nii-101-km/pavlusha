"""Optional stateless consultant with explicit endpoint and network grant."""
from types import SimpleNamespace
from pavlusha_agent.extensions import Contribution, Outcome
from .bundled._expert import Expert
from .util import declare, TEXT, STRING


class ExpertModule:
    name='expert'
    def __init__(self, settings): self.settings=settings; self.expert=None
    def on_event(self,event):
        if event.kind == 'session_started':
            if not self.settings:
                return Contribution(message='Expert consultation unavailable: configure endpoint and model.')
            args=SimpleNamespace(**{'expert_'+k:v for k,v in self.settings.items() if k!='allow_network'},
                                 network=self.settings.get('allow_network',False))
            self.expert=Expert(args)
            return Contribution(message='Ask the expert with explicit question and context; verify its advice.',
                tools=(declare('ask_expert','Ask a separate configured consultant.',
                    {'question':TEXT,'context':STRING},('question','context'),
                    lambda args,ctx:Outcome(self.expert.ask(args['question'],args['context']))),))
        if event.kind=='session_stopped': self.expert=None


def create_extension(*, settings, context):
    allowed={'base_url','model','api_key_env','max_tokens','reasoning_effort','max_calls','timeout','transport','allow_network'}
    if set(settings)-allowed or type(settings.get('allow_network',False)) is not bool:
        raise ValueError('invalid expert settings')
    return ExpertModule(settings)
