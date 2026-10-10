"""Retained numerical incident witness and measured-usage runtime regressions."""
import argparse
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pavlusha_agent.project_state import project_state_message
from pavlusha_agent.state_store import StateStore
from tests.test_context_preflight import (INCIDENT_TASK, INCIDENT_OPTIONS, incident_replies,
    legacy_estimate, prompt_size_bound, required)
from tests.test_runtime_lifecycle import run_script, review, measured_turn
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('context-guard-results/replay.json'))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        replies = incident_replies()
        replies[4] = turn({'action':'finish','summary':'done'})
        seen, result, error = run_script(root, replies, extra=INCIDENT_OPTIONS, task=INCIDENT_TASK)
        assert result == 0 and error is None and not required(seen[4])
        candidate = json.loads(json.dumps(seen[4]))
        state = StateStore(root/'state', INCIDENT_TASK).load()['project_state']
        candidate[2] = project_state_message(state, review_required=(
            'context usage reached the configured HIGH fraction of usable prompt context; '
            'review durable recovery information before the full history reset'))
        witness = legacy_estimate(SimpleNamespace(previous=seen[3],measured=22709), candidate)
        assert witness == 112021
        old = {'historical_fixture_estimate': witness, 'provider_usage':22709,
               'result':result, 'high_selected':False, 'calls':len(seen)}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reasoning = (Path(__file__).resolve().parents[1]/'tests/fixtures/step19_reasoning.txt').read_text()
        replies = [init_turn(reasoning='P'*83000),done('verified')]
        replies += [turn({'action':'shell','command':'inspect'}) for _ in range(15)]
        replies += [measured_turn({'action':'shell','command':'free -h'},reasoning='Q'*10000,tokens=27741),
                    measured_turn({'action':'shell','command':'start server'},reasoning=reasoning,tokens=31771),
                    turn({'action':'finish','summary':'done'})]
        seen, result, error = run_script(root,replies,extra=INCIDENT_OPTIONS[:4]+['--history-high','30'])
        assert result == 0 and error is None and len(seen)==20
        assert not required(seen[19]) and prompt_size_bound(seen[19])>109248
        second = {'provider_usage':31771,'diagnostic_byte_witness':prompt_size_bound(seen[19]),
                  'result':result,'high_selected':False,'calls':len(seen)}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        replies = [init_turn(),done('verified'),measured_turn({'action':'shell','command':'verify'},tokens=99660),
                   review('Measured HIGH completed.'),turn({'action':'finish','summary':'done'})]
        seen,result,error = run_script(root,replies,extra=INCIDENT_OPTIONS[:4]+['--history-high','30'])
        assert result==0 and error is None and required(seen[3])
        state = StateStore(root/'state','task').load()
        assert state['recovery_checkpoint']['handoff']=='Measured HIGH completed.'
        high = {'provider_usage':99660,'result':result,'handoff':state['recovery_checkpoint']['handoff'],
                'archived':(root/'state/history_archive.jsonl').exists()}
    report = {'context':117248,'max_tokens':8000,'threshold':99660,'previous_112021':old,
              'step19_lifecycle':second,'measured_high':high,'result':'PASS'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
