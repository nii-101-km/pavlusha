from __future__ import annotations

import io
import unittest

from pavlusha_agent.live import LiveConsoleRenderer


class ReasoningDiagnosticsTests(unittest.TestCase):
    def test_default_is_compact_and_diagnostics_are_opt_in(self):
        from pavlusha_agent.reasoning_loop import LoopSignal
        suspected = LoopSignal(800, 560, 0, .8, 560, 1, False)
        confirmed = LoopSignal(840, 600, 40, .85, 560, 2, True)
        for diagnostics in (False, True):
            stream = io.StringIO()
            renderer = LiveConsoleRenderer(stream=stream, color=False,
                                           reasoning_loop_diagnostics=diagnostics)
            renderer.reasoning_loop(suspected, mode="recover", attempt=0)
            if not diagnostics:
                self.assertEqual(stream.getvalue(), "")
            renderer.reasoning_loop(confirmed, mode="recover", attempt=0)
            self.assertIn("REASONING LOOP", stream.getvalue())
            self.assertEqual("similarity" in stream.getvalue(), diagnostics)
            self.assertEqual("distance" in stream.getvalue(), diagnostics)


class ProjectStateLiveRenderingTests(unittest.TestCase):
    def test_project_update_renders_worker_selected_evidence(self):
        stream = io.StringIO()
        live = LiveConsoleRenderer(stream=stream, color=False)
        live.action("project_update", {"changes": [{
            "op": "update_work",
            "id": "W003",
            "status": "DONE",
            "evidence": [
                "FILE: pavlusha_agent/sandbox.py",
                "SHELL RESULT: python3 -m unittest -> OK",
            ],
        }]})
        rendered = stream.getvalue()
        self.assertIn("update_work W003 DONE", rendered)
        self.assertIn("EVIDENCE  FILE: pavlusha_agent/sandbox.py", rendered)
        self.assertIn("EVIDENCE  SHELL RESULT: python3 -m unittest -> OK", rendered)

    def test_project_update_without_evidence_keeps_compact_output(self):
        stream = io.StringIO()
        live = LiveConsoleRenderer(stream=stream, color=False)
        live.action("project_update", {"changes": [{
            "op": "update_work", "id": "W002", "status": "ACTIVE"
        }]})
        rendered = stream.getvalue()
        self.assertIn("update_work W002 ACTIVE", rendered)
        self.assertNotIn("EVIDENCE", rendered)



class PrefixChangedLiveTests(unittest.TestCase):
    def test_prefix_changed_checkpoint_marker(self):
        stream = io.StringIO()
        renderer = LiveConsoleRenderer(stream=stream, color=False)
        renderer.prefix_changed("checkpoint snapshot refresh")
        self.assertIn("PREFIX CHANGED · checkpoint snapshot refresh", stream.getvalue())

# Presentation data remains separate from canonical runtime text.
import copy
import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch
from tests.test_runtime_lifecycle import run_script, review
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done
from tests.test_expert import EXTRA

class TtyStream(io.StringIO):
    def isatty(self):
        return True

ANSI = re.compile(r'\x1b\[[0-9;]*m')
PROSE = '# Result\n\n**Verified** and *readable* with `literal [red]` code.\n\n- first\n- second\n\n1. numbered\n2. another\n\n> quote\n\n---\n\n```python\nprint("exact")\n```'

class LivePresentationTests(unittest.TestCase):
    def renderer(self, stream=None, **kwargs):
        return LiveConsoleRenderer(stream=stream if stream is not None else TtyStream(), **kwargs)

    def test_interactive_semantic_styles(self):
        with patch.dict(os.environ, {'TERM':'xterm'}, clear=True):
            live=self.renderer()
            live.step(1,5)
            live.action('project_update',{'changes':[{'op':'update_work','id':'W001','status':'DONE'}]})
            live.invalid('bad action')
            text=live.stream.getvalue()
            self.assertIn('\x1b[1m',text)
            self.assertIn('\x1b[1;32mDONE',text)
            self.assertIn('\x1b[1;31mINVALID ACTION',text)

    def test_no_color_present_even_empty_and_non_tty_never_force_ansi(self):
        for no_color in ('','1'):
            with patch.dict(os.environ, {'NO_COLOR':no_color,'TERM':'xterm'}, clear=True):
                live=self.renderer(color=True)
                live.step(1,3)
                live.complete(PROSE)
                self.assertNotIn('\x1b',live.stream.getvalue())
                self.assertNotIn('# Result',live.stream.getvalue())
        with patch.dict(os.environ, {'FORCE_COLOR':'1','TERM':'xterm'}, clear=True):
            live=self.renderer(stream=io.StringIO(),color=True)
            live.complete(PROSE)
            self.assertNotIn('\x1b',live.stream.getvalue())

    def test_dumb_terminal_and_explicit_plain_mode(self):
        with patch.dict(os.environ, {'TERM':'dumb'}, clear=True):
            live=self.renderer()
            live.complete(PROSE)
            self.assertNotIn('\x1b',live.stream.getvalue())
        with patch.dict(os.environ, {'TERM':'xterm'}, clear=True):
            live=self.renderer(color=False)
            live.complete(PROSE)
            self.assertNotIn('\x1b',live.stream.getvalue())

    def test_markdown_preserves_words_and_code(self):
        with patch.dict(os.environ, {'TERM':'xterm'}, clear=True):
            live=self.renderer()
            live.complete(PROSE)
            text=ANSI.sub('',live.stream.getvalue())
            for raw in ('# Result','**Verified**','*readable*','```python'):
                self.assertNotIn(raw,text)
            for content in ('Result','Verified','readable','literal [red]','first','second','numbered','another','quote','print("exact")'):
                self.assertIn(content,text)
            self.assertIn('\x1b',live.stream.getvalue())

    def test_shell_preserves_literal_whitespace_commands_and_output(self):
        command='printf "[red]**text**[/red]"\n# shell comment'
        result={'stdout':'  [red]stdout[/red]  \n\n\t**literal**\n\n',
                'stderr':'  # not Markdown  \n\n','exit_code':0,'duration_seconds':0.01}
        original=copy.deepcopy(result)
        live=self.renderer(color=False)
        live.action('shell',{'command':command})
        live.operation('OP0001',result)
        self.assertIn(command,live.stream.getvalue())
        self.assertIn(result['stdout'],live.stream.getvalue())
        self.assertIn(result['stderr'],live.stream.getvalue())
        self.assertEqual(result,original)

    def test_expert_progress_markdown_error_and_resume_do_not_modify_data(self):
        result={'answer':'## Advice\n\n**Check** `actual files`.', 'duration_seconds':1,
                'prompt_tokens':12,'completion_tokens':30}
        original=copy.deepcopy(result)
        live=self.renderer(color=False)
        live.action('ask_expert',{'question':'PRIVATE_QUESTION','context':'PRIVATE_CONTEXT'})
        live.expert_event({'event':'started','model':'consultant','calls':1,'max_calls':5})
        live.expert_result(result)
        live.begin_worker()
        live.expert_result({'error':'expert_http_error','http_status':401})
        text=live.stream.getvalue()
        self.assertLess(text.index('requested'),text.index('request started'))
        self.assertLess(text.index('returned'),text.index('WORKER'))
        self.assertIn('failed · expert_http_error',text)
        self.assertIn('HTTP 401',text)
        self.assertNotIn('## Advice',text)
        self.assertNotIn('PRIVATE_',text)
        self.assertEqual(result,original)

    def test_state_events_leave_canonical_data_untouched(self):
        data={'changes':[{'op':'update_work','id':'W001','status':status,'evidence':['FILE: **raw**']} for status in ('ACTIVE','PLANNED','DONE','BLOCKED')]}
        original=copy.deepcopy(data)
        with patch.dict(os.environ, {'TERM':'xterm'}, clear=True):
            live=self.renderer()
            live.action('project_update',data)
            self.assertIn('\x1b[1;33mBLOCKED',live.stream.getvalue())
            self.assertIn('FILE: **raw**',live.stream.getvalue())
        self.assertEqual(data,original)

    def test_streamed_reasoning_is_immediate_literal_and_chronological(self):
        live=self.renderer(color=False)
        live.delta('reasoning','# partial **')
        self.assertIn('# partial **',live.stream.getvalue())
        live.delta('reasoning','thought** [red]')
        self.assertIn('# partial **thought** [red]',live.stream.getvalue())
        live.step(2,3)
        self.assertLess(live.stream.getvalue().index('thought'),live.stream.getvalue().index('STEP 2/3'))

    def test_missing_rich_falls_back_without_breaking_live(self):
        with patch.dict('sys.modules',{'rich.console':None}):
            live=self.renderer(color=False)
            live.complete(PROSE)
            self.assertIn(PROSE,live.stream.getvalue())
            self.assertNotIn('\x1b',live.stream.getvalue())

    def test_live_and_nonlive_have_identical_prompts_logs_history_and_checkpoint(self):
        fixed='2026-10-01T12:00:00+00:00'
        expert_result={'answer':'## Advice\n\n**Check** `files`.', 'model':'consultant',
                       'calls':1,'max_calls':5,'prompt_tokens':12,'completion_tokens':30,'duration_seconds':0.01}
        def expert(self,question,context,telemetry=None):
            if telemetry:
                telemetry({'event':'started','model':'consultant','calls':1,'max_calls':5})
                telemetry({k:v for k,v in expert_result.items() if k!='answer'} | {'event':'completed'})
            return copy.deepcopy(expert_result)
        script=[init_turn(),turn({'action':'ask_expert','question':'QUESTION','context':'CONTEXT'}),
            turn({'action':'shell','command':'verify'}),done('verified'),review('## Handoff\n\nContinue **verification**.'),
            turn({'action':'finish','summary':PROSE})]
        snapshots=[]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'TERM':'xterm'}, clear=True):
            for enabled in (False,True):
                root=Path(tmp)/str(enabled)
                renderer=self.renderer()
                with patch.dict(os.environ, {'TERM':'xterm'}, clear=True), \
                     patch('pavlusha_agent.runtime.LiveConsoleRenderer',return_value=renderer), \
                     patch('pavlusha_agent.expert.Expert.ask',expert), \
                     patch('pavlusha_agent.state_store._now',return_value=fixed), \
                     patch('pavlusha_agent.checkpoint._now',return_value=fixed), \
                     patch('pavlusha_agent.experiment._now',return_value=fixed):
                    seen,result,error=run_script(root,script,extra=[*EXTRA,'--history-high','4',*(['--live'] if enabled else [])])
                self.assertEqual(result,0,error)
                files={p.name:p.read_bytes() for p in (root/'state').rglob('*') if p.is_file()}
                for name in ('state.json','history_archive.jsonl','experiment.jsonl'):
                    self.assertIn(name,files)
                    self.assertNotIn(b'\x1b',files[name])
                    self.assertNotIn(b'\\u001b',files[name])
                snapshots.append((seen,files))
                if enabled:
                    self.assertIn('EXPERT',renderer.stream.getvalue())
                    self.assertIn('COMPLETE',renderer.stream.getvalue())
                    entries=[json.loads(line) for line in files['history_archive.jsonl'].decode().splitlines()]
                    archived=next(item['message']['content'] for item in entries if item['kind']=='expert_result')
                    self.assertEqual(json.loads(archived.split('\n',1)[1]),expert_result)
            self.assertEqual(snapshots[0],snapshots[1])

class StartupBannerTests(unittest.TestCase):
    def test_startup_banner_has_real_configuration_and_aligned_emoji_frame(self):
        from rich.cells import cell_len
        stream=io.StringIO()
        live=LiveConsoleRenderer(stream=stream,color=False)
        live.start(model='qwen/qwen3.8-27b',task='task',max_steps=60,
                   context=117248,expert_status='available')
        lines=stream.getvalue().splitlines()
        bottom=next(index for index,line in enumerate(lines) if line.startswith('╰'))
        frame=lines[:bottom+1]
        self.assertEqual({cell_len(line) for line in frame},{64})
        title_index=next(index for index,line in enumerate(frame) if 'P.A.V.L.U.S.H.A.' in line)
        self.assertIn('P.A.V.L.U.S.H.A.☝ 😐',frame[title_index])
        for value in ('qwen/qwen3.8-27b','117248','available','Shell-Handling Agent','PAVLUSHA LIVE','max steps 60'):
            self.assertIn(value,stream.getvalue())
        self.assertNotIn('\x1b',stream.getvalue())

    def test_long_model_and_narrow_terminal_keep_frame_aligned(self):
        from rich.cells import cell_len
        stream=io.StringIO()
        live=LiveConsoleRenderer(stream=stream,color=False)
        live._console.width=40
        live.start(model='provider/'+('long-model-'*12),task='task',max_steps=8)
        lines=stream.getvalue().splitlines()
        bottom=next(index for index,line in enumerate(lines) if line.startswith('╰'))
        self.assertEqual({cell_len(line) for line in lines[:bottom+1]},{40})
        self.assertIn('unknown',stream.getvalue())
        self.assertIn('off',stream.getvalue())

if __name__ == "__main__":
    unittest.main()
