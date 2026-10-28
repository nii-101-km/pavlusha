"""Write fenced reasoning blocks inside the sandbox, with bounded session references."""
from pavlusha_agent.extensions import Contribution, Outcome
from pavlusha_agent.sandbox import run_shell, admit_shell_result
from .bundled.reasoning_code import ReasoningCodeStore, resolve_reference, validate_reference
from .util import declare, TEXT


class ReasoningCode:
    name='reasoning_code'
    def __init__(self, root, timeout, output_limit):
        self.root=root; self.timeout=timeout; self.output_limit=output_limit
        self.store=ReasoningCodeStore(); self.labels={}; self.epoch=None
    def contribution(self):
        refs=', '.join(f'{ref} (turn {turn})' for ref,turn in self.labels.items() if ref in self.store.blocks)
        instruction=('To write a complete file, include a closed fenced code block in the current reasoning and call '
            'write_code_from_reasoning with destination and mode. For multiple blocks supply block_index. '
            'Alternatively choose a saved block_ref from the list; references expire after context reset or restart.')
        if refs: instruction+=' Saved block references in extraction order: '+refs
        return Contribution(message=instruction,tools=(declare('write_code_from_reasoning',
            'Write a current or saved reasoning code block inside /work.',
            {'destination':TEXT,'mode':{'enum':['create','overwrite']},
             'block_index':{'type':'integer','minimum':1},'block_ref':TEXT},
            ('destination','mode'),self.write),))
    def on_event(self,event):
        if event.kind=='session_started':
            self.epoch=event.context_epoch
            return self.contribution()
        if event.kind=='prepare_turn' and self.epoch!=event.context_epoch:
            self.epoch=event.context_epoch; self.store.clear(); self.labels.clear()
            return self.contribution()
        if event.kind=='turn_completed':
            turn=event.data['turn']; response=turn.get('response') or {}
            reasoning=response.get('reasoning_content') or ''
            if not reasoning: return None
            before=set(self.store.blocks)
            self.store.capture(reasoning)
            self.labels={ref:turn_id for ref,turn_id in self.labels.items() if ref in self.store.blocks}
            for ref in self.store.blocks:
                if ref not in before: self.labels[ref]=turn['turn_id']
            return self.contribution()
        if event.kind=='session_stopped':
            self.store.clear(); self.labels.clear()
    def write(self,args,context):
        validate_reference({'action':'write_code_from_reasoning',**args})
        command=resolve_reference(args,context.response['reasoning_content'],self.timeout,code_store=self.store)
        result=run_shell(self.root,command['command'],network=False,timeout=self.timeout)
        return Outcome(admit_shell_result(result,self.output_limit))


def create_extension(*, settings, context):
    if settings: raise ValueError('ReasoningCode accepts no settings')
    return ReasoningCode(context['workdir'],context.get('command_timeout',120),context.get('output_limit',24000))
