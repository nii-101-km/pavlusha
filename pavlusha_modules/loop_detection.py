"""Independent observers for repetition in reasoning and completed actions."""
from pavlusha_agent.extensions import Contribution, Control, EventReply
from .bundled.reasoning_loop import ReasoningLoopDetector
from .bundled.operation_loop import OperationLoopDetector


class LoopDetection:
    name='loop_detection'
    instructions = {'instructions': 'Repeated reasoning may be interrupted; repeated tool results will be reported.'}
    def __init__(self, max_interruptions=2, *, reasoning_window_words=240, reasoning_stride_words=40, reasoning_min_lag_words=480):
        self.reasoning_options = {"window_words": reasoning_window_words, "stride_words": reasoning_stride_words, "min_lag_words": reasoning_min_lag_words}
        ReasoningLoopDetector(**self.reasoning_options)
        self.max_interruptions=max_interruptions; self.consecutive=0
        self.reasoning=None; self.operations=OperationLoopDetector(); self.notice=''
        self.removed_turns=set()
        self.active_cycles={}
    def on_event(self,event):
        if event.kind=='session_started':
            return Contribution(prefix=self.instructions)
        if event.kind=='prepare_turn':
            self.reasoning=ReasoningLoopDetector(**self.reasoning_options)
            return None
        if event.kind=='reasoning_fragment':
            if self.reasoning is not None and any(s.confirmed for s in self.reasoning.feed(event.data['text'])):
                self.consecutive+=1
                controls=[Control('interrupt','reasoning repetition detected')]
                if self.consecutive>self.max_interruptions:
                    controls.append(Control('stop','Repeated reasoning loop; task paused for inspection.'))
                contribution = Contribution(prefix=self.instructions, message='Reasoning repeated. Change approach and use concrete evidence.')
                return EventReply(contribution,tuple(controls))
        if event.kind=='turn_completed':
            turn=event.data['turn']
            if turn['status']=='generation_interrupted': return None
            self.consecutive=0
            action=turn.get('action') or {}; result=turn.get('result') or {}
            if turn['status']!='completed' or not action:
                self.operations.clear(); self.active_cycles.clear(); return None
            digest = self.operations.fingerprint(action.get('action'), action, result)
            if digest in self.active_cycles:
                self.removed_turns.add(self.active_cycles[digest])
                self.active_cycles[digest] = turn['turn_id']
            else:
                self.active_cycles.clear()
            match=self.operations.feed(action.get('action'),action,result,step=turn['turn_id'])
            if match:
                by_step = {step: fingerprint for fingerprint, step in self.operations.history}
                length = match.cycle_length
                self.removed_turns.update(match.steps[:-length])
                self.active_cycles = {by_step[step]: step for step in match.steps[-length:]}
                self.operations.clear()
                command = str(action.get('command', action.get('action', '')))[:500]
                exit_code = result.get('exit_code')
                observed = result.get('stderr') or result.get('stdout') or result.get('error') or ''
                observed = str(observed)[-700:]
                note = (f'Repeated action/result cycle detected ({length} actions, repeated three times). '
                        f'Last action: {command!r}.')
                if exit_code is not None:
                    note += f' Exit code: {exit_code}.'
                if observed:
                    note += f' Last observed output or error: {observed}'
                note += (' The result repeated; the file state has not been checked by the detector. '
                         'Inspect the evidence and choose a different next action.')
                return Contribution(prefix=self.instructions, message=note)
            return Contribution(prefix=self.instructions)
        if event.kind=='before_reset':
            self.operations.clear()
            self.removed_turns.clear()
            self.active_cycles.clear()

    def turns_to_remove(self):
        return tuple(sorted(self.removed_turns))


def create_extension(*, settings, context):
    if set(settings)-{'max_interruptions', 'reasoning_window_words', 'reasoning_stride_words', 'reasoning_min_lag_words'}:
        raise ValueError('unknown LoopDetection settings')
    value = settings.get('max_interruptions', 2)
    if type(value) is not int or value < 0:
        raise ValueError('max_interruptions must be a nonnegative integer')
    return LoopDetection(value,
        reasoning_window_words=settings.get('reasoning_window_words', 240),
        reasoning_stride_words=settings.get('reasoning_stride_words', 40),
        reasoning_min_lag_words=settings.get('reasoning_min_lag_words', 480))
