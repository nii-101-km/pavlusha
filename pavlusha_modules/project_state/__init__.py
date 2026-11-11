"""Project State event handler, including module-owned review and finish policies."""
import copy
import hashlib
import json
import os
from pathlib import Path

from pavlusha_agent.extensions import Contribution, Outcome, EventReply, Control
from pavlusha_runtime.errors import AgentError
from . import model
from .schema import declarations


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ProjectState:
    name = 'project_state'

    def __init__(self, *, review_every=10, directory=None, combined_review=False):
        if type(combined_review) is not bool:
            raise ValueError("combined_review must be boolean")
        self.combined_review = combined_review
        if type(review_every) is not int or review_every < 0:
            raise ValueError('review_every must be a nonnegative integer')
        self.review_every = review_every
        self.directory = Path(directory) if directory is not None else None
        self.data = None
        self.epoch = None
        self.step = 0
        self.frozen_prefix = {}
        self.want_reset = False
        self.recovering = False
        self.recovery_epoch = None
        self.tools = declarations(self.invoke, combined_review=combined_review)
        self.read_turns = None

    def bind_event_journal(self, read_turns):
        self.read_turns = read_turns

    def save_generation(self):
        path = self.path.parent / ('generation-%020d.json' % self.data['last_processed_turn'])
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as handle:
            json.dump({'payload': self.data, 'sha256': digest(self.data)}, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        for old in sorted(self.path.parent.glob('generation-*.json'))[:-3]:
            old.unlink()

    def load_generation(self, path, task_hash, event):
        envelope = json.loads(path.read_text())
        data = envelope['payload']
        if (envelope.get('sha256') != digest(data) or data.get('version') != 1
                or data.get('task_sha256') != task_hash or data.get('session_id') != event.session_id):
            raise AgentError('Project State identity or digest mismatch')
        if data['project'].get('initialized'):
            model.validate_persisted_project_state(data['project'])
        elif data['project'] != model.empty_project_state():
            raise AgentError('invalid uninitialized Project State')
        checkpoint = data.get('checkpoint')
        if checkpoint is not None:
            model.validate_persisted_project_state(checkpoint['project'])
            model.validate_handoff(checkpoint['handoff'])
        if type(data['last_processed_turn']) is not int or not 0 <= data['last_processed_turn'] <= event.data['last_turn_id']:
            raise AgentError('Project State is ahead of the technical session; manual reconciliation required')
        return data

    def save(self, data):
        envelope = {'payload': data, 'sha256': digest(data)}
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as handle:
            json.dump(envelope, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.data = data

    def start(self, event):
        directory = self.directory or Path(event.data['session_dir']) / 'modules' / self.name
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'state.json'
        task_hash = hashlib.sha256(event.data['task'].encode()).hexdigest()
        candidates = ([self.path] if self.path.exists() else []) + sorted(directory.glob('generation-*.json'), reverse=True)
        if candidates:
            errors = []
            fallback = False
            for candidate in candidates:
                try:
                    data = self.load_generation(candidate, task_hash, event)
                    fallback = candidate != self.path
                    break
                except (AgentError, ValueError, OSError, KeyError, TypeError) as exc:
                    errors.append(str(exc))
            else:
                raise AgentError('no valid Project State generation: ' + '; '.join(errors))
            self.data = data
            missing = event.data['last_turn_id'] - data['last_processed_turn']
            if missing:
                if self.read_turns is None:
                    raise AgentError('Project State recovery requires the event journal')
                for turn in self.read_turns(data['last_processed_turn']):
                    self.replay_turn(turn)
            if fallback:
                self.save(copy.deepcopy(self.data))
                self.save_generation()
            if event.data['resumed'] and self.data.get('checkpoint'):
                if not fallback and not missing:
                    self.restore_checkpoint()
                self.want_reset = True
                self.recovering = True
                self.recovery_epoch = event.context_epoch
        else:
            if event.data['resumed'] and event.data['last_turn_id']:
                raise AgentError('Project State is missing for an existing session; refusing to invent recovery state')
            self.save({'version': 1, 'session_id': event.session_id, 'task_sha256': task_hash,
                       'project': model.empty_project_state(), 'operation_count': 0,
                       'last_processed_turn': 0, 'checkpoint': None, 'review_reason': None})
            self.save_generation()
        self.refresh_prefix()

    def replay_turn(self, turn):
        """Restore completed module actions from their persisted results, never re-execute them."""
        action = turn.get('action') or {}
        result = turn.get('result') or {}
        name = action.get('action')
        if name in ('project_init', 'project_update', 'project_review_complete') and (
                result.get('output_withheld') or turn.get('status') in ('execution_error', 'interrupted')):
            raise AgentError('cannot reconstruct uncertain Project State action from event journal')
        if name in ('project_init', 'project_update', 'project_review_complete') and result.get('status') == 'accepted':
            project = result.get('project_state')
            model.validate_persisted_project_state(project)
            updated = copy.deepcopy(self.data)
            updated['project'] = copy.deepcopy(project)
            if (name in ('project_init', 'project_review_complete') and
                    (updated.get('checkpoint') or {}).get('turn_id') != turn['turn_id']):
                self.step = turn['turn_id']
                self.checkpoint(updated, action.get('handoff', '') if name == 'project_review_complete' else '')
            self.save(updated)
        from pavlusha_agent.extensions import Event
        self.on_event(Event('turn_completed', self.data['session_id'], turn['turn_id'],
                            turn['context_epoch'], {'turn': turn}))

    def restore_checkpoint(self, *, overflow=False):
        updated = copy.deepcopy(self.data)
        checkpoint = updated.get('checkpoint')
        if not checkpoint:
            raise AgentError('Project State has no checkpoint to restore')
        if overflow and updated.get('last_overflow_generation') == checkpoint['generation']:
            raise AgentError('context overflow already recovered from this checkpoint')
        if overflow:
            updated['last_overflow_generation'] = checkpoint['generation']
        updated['project'] = copy.deepcopy(checkpoint['project'])
        updated['operation_count'] = checkpoint['operation_count']
        updated['review_reason'] = None
        self.save(updated)

    def refresh_prefix(self):
        project = self.data['project']
        # At session start the plan has not yet been generated. Keep this
        # snapshot accurate after project_init without changing the prefix:
        # the accepted plan and subsequent updates appear in turn history.
        state = (model.project_state_message(project)['content'] if project['initialized'] else
                 'PROJECT STATE: No plan was present at the start of this context. '
                 'Use the subsequent project_init result and later updates in history as the current state.')
        self.frozen_prefix = {'state': state}
        checkpoint = self.data['checkpoint']
        if checkpoint and checkpoint['handoff']:
            self.frozen_prefix['handoff'] = model.handoff_message(checkpoint['handoff'])['content']

    def contribution(self):
        initialized = self.data['project']['initialized']
        review = self.data['review_reason']
        if not initialized:
            names, exclusive, ready = ('project_init',), True, False
            message = 'Initialize Project State with project_init before ordinary work.'
        elif review:
            names, exclusive, ready = (('project_review_complete',) if self.combined_review else ('project_update', 'project_review_complete')), True, False
            message = ('PROJECT STATE REVIEW REQUIRED: ' + review + '. Ordinary tools are isolated. ' +
                       ('Call project_review_complete once with optional changes and a handoff describing the next action; shell will become available after review.' if self.combined_review else
                        'Record remaining durable changes, then project_review_complete with a brief handoff. Updating State alone does not release the review.'))
        else:
            names, exclusive, ready = ('project_update', 'finish'), False, True
            message = ''
        if self.recovering and self.data.get('checkpoint'):
            ready = True
        prefix = copy.deepcopy(self.frozen_prefix)
        rules = {
            'project_init': 'Initialize a concrete plan with at least two work items, exactly one ACTIVE and at least one PLANNED before ordinary work.',
            'project_update': 'Keep durable State current: record decisions, work status and evidence; exactly one item must be ACTIVE while work remains. BLOCKED requires a reason; SUPERSEDED requires a deviation.',
            'project_review_complete': 'For a required review, update durable State and provide a short nonempty handoff with the immediate next action and unfinished verification.',
            'finish': 'Finish only when all work is DONE or SUPERSEDED, backed by evidence.'}
        system_instructions = '\n'.join(rules.values())
        contribution = Contribution(prefix=prefix, system_instructions=system_instructions,
            message=message,
            tools=tuple(self.tools.values()), allowed_tools=names, exclusive=exclusive, reset_ready=ready)
        return EventReply(contribution, (Control('reset', 'Project State checkpoint ready'),) if self.want_reset else ())

    def on_event(self, event):
        if event.kind == 'session_started':
            self.start(event)
        elif event.kind == 'prepare_turn':
            self.step = event.data['next_turn_id']
            if self.recovering and self.recovery_epoch == event.context_epoch:
                raise AgentError('Project State recovery requires a completed context reset')
            if self.epoch != event.context_epoch:
                self.epoch = event.context_epoch
                self.want_reset = False
                self.recovering = False
                self.refresh_prefix()
        elif event.kind == 'before_reset':
            checkpoint = self.data['checkpoint']
            if event.data.get('mandatory') and checkpoint:
                self.restore_checkpoint(overflow=True)
                self.recovering = True
                self.recovery_epoch = event.context_epoch
                self.want_reset = False
                return self.contribution()
            if self.recovering and checkpoint:
                return self.contribution()
            if self.data['project']['initialized'] and checkpoint and checkpoint['turn_id'] >= event.turn_id:
                return self.contribution()
            updated = copy.deepcopy(self.data)
            updated['review_reason'] = 'reset: ' + event.data['reason']
            self.save(updated)
        elif event.kind == 'turn_completed':
            turn = event.data['turn']
            if turn['turn_id'] <= self.data['last_processed_turn']:
                return None
            updated = copy.deepcopy(self.data)
            updated['last_processed_turn'] = turn['turn_id']
            action = turn.get('action') or {}
            if (turn['status'] in ('completed', 'execution_error', 'interrupted')
                    and action.get('action') not in (*self.tools, 'finish', None)):
                updated['operation_count'] += 1
            if model.review_due(updated['project'], operation_count=updated['operation_count'], every=self.review_every):
                updated['review_reason'] = updated['review_reason'] or 'periodic'
            self.save(updated)
            self.save_generation()
        else:
            return None
        return self.contribution()

    def checkpoint(self, data, handoff):
        previous = data.get('checkpoint')
        data['checkpoint'] = {'generation': previous['generation'] + 1 if previous else 1,
                              'turn_id': self.step, 'project': copy.deepcopy(data['project']),
                              'operation_count': data['operation_count'], 'handoff': handoff}
        data['review_reason'] = None

    def invoke(self, name, arguments):
        try:
            if name == 'finish':
                unfinished = [item['id'] for item in self.data['project']['work']
                              if item['status'] not in ('DONE', 'SUPERSEDED')]
                if unfinished:
                    return Outcome({'error': 'unfinished_work', 'work_ids': unfinished})
                if self.data['review_reason'] or not self.data['project']['initialized']:
                    return Outcome({'error': 'review_required'})
                return Outcome({'status': 'finished', 'summary': arguments['summary']},
                               controls=(Control('stop', arguments['summary'], finished=True),))
            _, values = model.validate_project_action({'action': name, **arguments})
            updated = copy.deepcopy(self.data)
            if name == 'project_init':
                updated['project'] = model.initialize_project_state(updated['project'], values,
                    operation_count=updated['operation_count'])
                self.checkpoint(updated, '')
            elif name == 'project_update':
                updated['project'] = model.apply_project_changes(updated['project'], values['changes'])
            elif name == 'project_review_complete':
                if not updated['review_reason']:
                    raise AgentError('no review is required')
                handoff = model.validate_handoff(values['handoff'])
                if not handoff:
                    raise AgentError('a nonempty handoff is required')
                if values.get('changes'):
                    if not self.combined_review:
                        raise AgentError('combined review changes are not enabled')
                    updated['project'] = model.apply_project_changes(updated['project'], values['changes'])
                updated['project'] = model.complete_review(updated['project'], operation_count=updated['operation_count'])
                reset_requested = bool(updated['review_reason'].startswith('reset:'))
                self.checkpoint(updated, handoff)
            self.save(updated)
            if name == 'project_review_complete' and reset_requested:
                self.want_reset = True
            return Outcome({'status': 'accepted', 'project_state': copy.deepcopy(updated['project'])})
        except AgentError as exc:
            return Outcome({'error': 'project_state_rejected', 'detail': str(exc)})


def create_extension(*, settings, context):
    if set(settings) - {'review_every', 'combined_review'}:
        raise ValueError('Project State accepts review_every and combined_review only')
    return ProjectState(review_every=settings.get('review_every', context.get('review_every', 10)),
                        combined_review=settings.get('combined_review', False))
