"""Independent observers for repetition in reasoning and completed actions."""
from pavlusha_agent.extensions import Contribution, Control, EventReply
from .bundled.reasoning_loop import ReasoningLoopDetector
from .bundled.operation_loop import OperationLoopDetector


class LoopDetection:
    name='loop_detection'
    instructions = {'instructions': 'Repeated reasoning may be interrupted; repeated tool results will be reported.'}
    def __init__(self, max_interruptions=2):
        self.max_interruptions=max_interruptions; self.consecutive=0
        self.reasoning=None; self.operations=OperationLoopDetector(); self.notice=''
    def on_event(self,event):
        if event.kind=='session_started':
            return Contribution(prefix=self.instructions)
        if event.kind=='prepare_turn':
            self.reasoning=ReasoningLoopDetector()
            return None
        if event.kind=='reasoning_fragment':
            if self.reasoning is not None and any(s.confirmed for s in self.reasoning.feed(event.data['text'])):
                self.consecutive+=1
                controls=[Control('interrupt','reasoning repetition detected')]
                if self.consecutive>self.max_interruptions:
                    controls.append(Control('stop','Repeated reasoning loop; task paused for inspection.'))
                return EventReply(Contribution(prefix=self.instructions, message='Reasoning repeated. Change approach and use concrete evidence.'),tuple(controls))
        if event.kind=='turn_completed':
            turn=event.data['turn']
            if turn['status']=='generation_interrupted': return None
            self.consecutive=0
            action=turn.get('action') or {}; result=turn.get('result') or {}
            if turn['status']!='completed' or not action:
                self.operations.clear(); return None
            match=self.operations.feed(action.get('action'),action,result,step=turn['turn_id'])
            if match:
                self.operations.clear()
                return Contribution(prefix=self.instructions, message=f'Repeated action/result cycle detected ({match.cycle_length} actions). '
                    'Stop repeating; inspect evidence and change approach.')
            return Contribution(prefix=self.instructions)
        if event.kind=='before_reset':
            self.operations.clear()


def create_extension(*, settings, context):
    if set(settings)-{'max_interruptions'} or type(settings.get('max_interruptions',2)) is not int or settings.get('max_interruptions',2)<0:
        raise ValueError('max_interruptions must be a nonnegative integer')
    return LoopDetection(settings.get('max_interruptions',2))
