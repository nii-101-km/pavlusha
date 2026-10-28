"""Project Map navigation snapshots through the public event interface."""
from pathlib import Path

from pavlusha_agent.core import AgentError
from pavlusha_agent.extensions import Contribution
from .index import ProjectMap as MapIndex


class ProjectMap:
    name = 'project_map'

    def __init__(self, workdir, *, indexer=None):
        self.workdir = Path(workdir).expanduser().resolve()
        self.indexer = indexer
        self.map = None
        self.epoch = None

    def snapshot(self):
        self.map.refresh()
        return Contribution(prefix={'navigation': self.map.text(), 'instructions': (
            'Project Map is a checkpoint navigation snapshot of Python files. '
            'Files may have changed since this snapshot; inspect current files before editing.')})

    def on_event(self, event):
        if event.kind == 'session_started':
            session_dir = Path(event.data['session_dir']).resolve()
            if session_dir.is_relative_to(self.workdir) or self.workdir.is_relative_to(session_dir):
                raise AgentError('Project Map workdir and session directory must be separate and non-nested')
            self.map = MapIndex(self.workdir,
                session_dir / 'modules' / self.name / 'map.json', indexer=self.indexer)
            self.epoch = event.context_epoch
            return self.snapshot()
        if event.kind == 'prepare_turn' and self.epoch != event.context_epoch:
            contribution = self.snapshot()
            self.epoch = event.context_epoch
            return contribution
        if event.kind == 'session_stopped':
            self.map = None
        return None


def create_extension(*, settings, context):
    if settings:
        raise ValueError('Project Map does not accept settings')
    return ProjectMap(context['workdir'])
