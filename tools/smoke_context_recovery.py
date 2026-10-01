"""Real local provider overflow -> runtime cold recovery -> real successful continuation.

Temporary State/workdir only. The first request is deliberately inflated by this
probe wrapper; production runtime never adds that text. No model config changes.
"""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pavlusha_agent.cli import build_parser
from pavlusha_agent.provider import ChatProvider
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore
from tests.test_reasoning_window import init_turn
from tests.test_checkpoint_snapshots import done


def main():
    task = 'The work is already verified. Reply with exactly {"action":"finish","summary":"Recovered successfully"}.'
    original = ChatProvider.worker_completion
    calls = []
    output = io.StringIO()
    with tempfile.TemporaryDirectory(prefix='pavlusha-real-overflow-') as tmp:
        root = Path(tmp)
        work = root/'work'
        work.mkdir()
        (work/'current.py').write_text('def current_world(): return 42\n')
        store = StateStore(root/'state', task)
        store.initialize_project(json.loads(init_turn().content), step=1)
        store.update_project(json.loads(done('verified').content)['changes'], step=2)
        store.complete_project_review(step=3, handoff='Report completion after inspecting current.py.')
        before = store.load()
        def send(provider, messages, **kwargs):
            calls.append({'messages':len(messages), 'current_map': 'current.py' in str(messages),
                          'contains_probe': ' x x x x' in str(messages)})
            if len(calls) == 1:
                messages = messages + [{'role':'user','content':' x'*180000}]
            return original(provider, messages, reasoning_effort='low', **kwargs)
        args = build_parser().parse_args(['--workdir',str(work),'--state-dir',str(root/'state'),
            '--model','qwen/qwen3.8-27b','--worker-context-budget','117248','--max-tokens','8000',
            '--project-map','on','--live','--max-steps','4',task])
        with patch.object(ChatProvider,'worker_completion',send), redirect_stdout(output), redirect_stderr(output):
            result = run_agent(args)
        events = [json.loads(l) for l in (root/'state/experiment.jsonl').read_text().splitlines()]
        recoveries = [e for e in events if e['kind']=='context_overflow_recovery']
        turns = [e for e in events if e['kind']=='worker_turn']
        after = store.load()
        assert result == 0 and len(calls) == 2 and len(recoveries) == 1
        assert turns[0]['prompt_tokens'] is not None
        assert before['recovery_checkpoint'] == after['recovery_checkpoint']
        assert before['operations'] == after['operations']
        assert (work/'current.py').read_text() == 'def current_world(): return 42\n'
        assert not (root/'state/history_archive.jsonl').exists()
        report = {'result':'PASS', 'calls':calls, 'recoveries':recoveries, 'successful_turns':turns,
                  'context':117248,'max_tokens':8000,'checkpoint_unchanged':True,'work_preserved':True,
                  'live_output':output.getvalue()}
    destination = Path('context-guard-results/real-overflow-recovery.json')
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(output.getvalue())
    print('PASS:',destination.resolve())

if __name__=='__main__': main()
