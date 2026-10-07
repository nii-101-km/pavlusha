"""Actual interpreter/runtime fixture for abrupt-exit checkpoint recovery tests.

Provider replies and shell work are scripted; persistence, runtime lifecycle, Map,
process termination, and the second interpreter startup use the production code.
"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import ProviderTurn, ShellResult
from pavlusha_agent.runtime import run_agent
from tests.test_reasoning_window import init_turn
from tests.test_checkpoint_snapshots import done

root=Path(sys.argv[1]); phase=sys.argv[2]; boundary=sys.argv[3]

def turn(action):
    return ProviderTurn(json.dumps(action),'brief normal reasoning','stop',prompt_tokens=200)

def shell(work,command,**kwargs):
    if command=='create':
        (work/'persisted.py').write_text('def persisted(): return 1\n')
    elif command=='continue':
        (work/'newer.py').write_text('def newer(): return 2\n')
    elif command=='verify':
        assert (work/'persisted.py').exists() and (work/'newer.py').exists()
    return ShellResult(command,False,0,False,'OK','',0.01)

if phase=='create':
    replies=iter([init_turn(),turn({'action':'shell','command':'create'}),
                  turn({'action':'project_review_complete','handoff':'Verify persisted.py next.'}),
                  turn({'action':'shell','command':'continue'}),
                  turn({'action':'project_update','changes':[{'op':'add_design','decision':'UNCOMMITTED','rationale':'post-checkpoint change'}]})])
    history_high='2'
elif phase=='checkpoint':
    replies=iter([turn({'action':'project_update','changes':[{'op':'add_design','decision':'CANDIDATE_COMMITTED','rationale':'candidate finding'}]}),
                  turn({'action':'project_review_complete','handoff':'Resume candidate finding.'})])
    history_high='1'
else:
    replies=iter([turn({'action':'shell','command':'verify'}),done('verify'),turn({'action':'finish','summary':'resumed'})])
    history_high='99'

calls=0

def worker(provider,messages,**kwargs):
    global calls
    if phase=='resume' and calls==0:
        (root/'resumed-request.json').write_text(json.dumps(messages,ensure_ascii=False))
        assert not any('UNCOMMITTED' in m['content'] for m in messages)
    calls+=1
    try:
        return next(replies)
    except StopIteration:
        os._exit(71)

actual_open=open

def open_candidate(path,*args,**kwargs):
    handle=actual_open(path,*args,**kwargs)
    if phase!='checkpoint' or boundary not in ('partial_state','partial_handoff') or not Path(path).name.startswith('.state.json.tmp.'):
        return handle
    class Writer:
        def __enter__(self): return self
        def __exit__(self,*args): handle.close()
        def write(self,data):
            # Startup restoration writes the old generation too; interrupt only the candidate.
            if 'Resume candidate finding.' not in data:
                return handle.write(data)
            begin=data.index('"recovery_checkpoint"')
            field=data.index('"project_state"' if boundary=='partial_state' else '"handoff"',begin)
            handle.write(data[:field+25]); handle.flush(); os.fsync(handle.fileno())
            os._exit(72)
        def flush(self): handle.flush()
        def fileno(self): return handle.fileno()
    return Writer()

actual_replace=os.replace

def replace_candidate(source,destination):
    candidate=Path(source)
    matches=(phase=='checkpoint' and candidate.name.startswith('.state.json.tmp.')
             and 'Resume candidate finding.' in candidate.read_text())
    if matches and boundary=='before_replace': os._exit(72)
    actual_replace(source,destination)
    if matches and boundary=='after_replace': os._exit(72)

args=build_parser().parse_args(['--no-interactive','--no-live','--no-network',
    '--workdir',str(root/'work'),'--state-dir',str(root/'state'),
    '--model','scripted','--worker-context-budget','40000','--max-tokens','1024',
    '--history-high',history_high,'--project-review-every','0','--project-map','on',
    '--max-steps','-1','task'])
with patch('pavlusha_agent.runtime.shutil.which',return_value='/fake/bwrap'), \
     patch('pavlusha_agent.provider.ChatProvider.worker_completion',worker), \
     patch('pavlusha_agent.runtime.run_shell',shell), \
     patch('builtins.open',side_effect=open_candidate), \
     patch('pavlusha_agent.state_store.os.replace',side_effect=replace_candidate):
    raise SystemExit(run_agent(args))
