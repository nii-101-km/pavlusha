"""Exercise explicitly loaded built-in modules through the actual runtime."""
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.extensions import Event, EventHost
from pavlusha_agent.runtime import Runtime
from pavlusha_modules.loader import load_modules
from tests.test_events import Provider, reply
from tests.test_project_state_module import initialize, review, done, finish


class LoadedCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.work=self.root/'work'; self.work.mkdir()
        self.manifest=Path(__file__).resolve().parents[1]/'examples/qwen.native.config.json'
    def modules(self, path=None):
        return load_modules(path or self.manifest,context={'workdir':str(self.work),
            'review_every':0,'network':False,'command_timeout':10,'output_limit':24000})
    def runtime(self,provider,modules=None,**kwargs):
        return Runtime(provider,task='task',session_dir=self.root/'session',
            extensions=modules or self.modules(),max_steps=20,**kwargs)
    def test_normal_turns_keep_instructions_in_stable_prefix(self):
        names=('shell','project_state','project_map','processes','browser','loop_detection','interactive')
        path=self.root/'modules.json'
        path.write_text(json.dumps({'modules':[{'factory':f'pavlusha_modules.{name}:create_extension'}
                                               for name in names]}))
        modules=self.modules(path)
        shell=next(m for m in modules if m.name=='shell')
        shell.host.shell_runner=lambda *a,**kw:ShellResult('verify',False,0,False,'OK','',0)
        provider=Provider([initialize(),reply({'action':'shell','command':'verify'}),
                           reply({'action':'shell','command':'verify'}),done(),finish()])
        self.runtime(provider,modules).run()
        first,second=(provider.requests[i]['messages'] for i in (1,2))
        def prefix(messages):
            return [m for m in messages if isinstance(m.get('content'),str)
                    and m['content'].startswith('EXTENSION ') and '/' in m['content'].split('\n',1)[0]]
        self.assertEqual(prefix(first),prefix(second))
        self.assertFalse(any(str(m.get('content','')).startswith('EXTENSION ') for m in first[-3:]))
        for name in ('processes','browser','interactive','loop_detection'):
            self.assertIn(f'MODULE {name}:', first[0]['content'])
    def test_tool_examples_match_browser_and_process_permissions(self):
        from pavlusha_modules.processes import Processes
        from pavlusha_modules.browser import Browser
        event=Event('session_started','s',0,0)
        for granted in (False,True):
            process=Processes(self.work,allow_network=granted)
            contribution=process.on_event(event)
            self.addCleanup(lambda p=process:p.on_event(Event('session_stopped','s',0,0)))
            instructions=contribution.prefix['instructions']
            for name in ('process_start','process_status','process_logs','process_stop'):
                self.assertIn('"action":"'+name+'"',instructions)
            self.assertIn('network=true' if granted else 'host network is not granted',instructions)
            self.assertEqual({t.name for t in contribution.tools},
                             {'process_start','process_status','process_logs','process_stop'})
        class Adapter:
            def describe(self):return {'target':'test'}
            def close(self):pass
        for enabled in (False,True):
            browser=Browser(self.root/'adapter.json',24000)
            with patch('pavlusha_modules.browser.load_browser_adapter',return_value=Adapter()), \
                 patch('pavlusha_modules.browser.evaluation_available',return_value=enabled):
                contribution=browser.on_event(event)
            instructions=contribution.prefix['instructions']
            names={t.name for t in contribution.tools}
            self.assertIn('"action":"browser_diagnostics"',instructions)
            self.assertEqual('browser_evaluate' in names,enabled)
            self.assertEqual('"action":"browser_evaluate"' in instructions,enabled)
            browser.on_event(Event('session_stopped','s',0,0))
    def test_loaded_default_images_review_reset_and_cleanup(self):
        from PIL import Image
        Image.new('RGB',(4,4),'red').save(self.work/'image.png')
        provider=Provider([initialize(),reply({'action':'view_image','path':'image.png'}),
                           review(),done(),finish()])
        modules=self.modules();runtime=self.runtime(provider,modules,history_high=2)
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(runtime.session.state['context_epoch'],1)
        self.assertNotIn('image_url',str(provider.requests[0]['messages']))
        # Review restricts tools but leaves the map and observation visible.
        self.assertNotIn('view_image',str(provider.requests[2]['response_format']))
        self.assertIn('image_url',str(provider.requests[2]['messages']))
        self.assertNotIn('image_url',str(provider.requests[3]['messages']))
        self.assertIn('project_map/navigation',str(provider.requests[3]['messages']))
        self.assertIsNone(next(m for m in modules if m.name=='processes').manager)
        self.assertIsNone(next(m for m in modules if m.name=='gui').runtime)
    def test_loaded_gpu_shell_uses_policy_and_preserves_permissions(self):
        modules=self.modules();gpu=next(m for m in modules if m.name=='gpu_shell');calls=[]
        gpu.host.shell_runner=lambda root,command,**kw: calls.append((command,kw)) or ShellResult(command,False,0,False,'ok','',0)
        @contextmanager
        def release(*,restore_guard):
            calls.append('released');yield;calls.append('restored')
        provider=Provider([initialize(),reply({'action':'shell_released','command':'python script.py','gpu':True}),done(),finish()])
        provider.released_worker=release
        self.runtime(provider,modules).run()
        self.assertEqual(calls,['released',('python script.py',{'network':False,'timeout':10,'gpu':True}),'restored'])
    def test_loaded_reasoning_code_runs_bounded_sandbox_command(self):
        modules=self.modules(); action=reply({'action':'write_code_from_reasoning','destination':'x.py','mode':'create'})
        action.reasoning_content='Here is code:\n```python\nprint(42)\n```\n'
        provider=Provider([initialize(),action,done(),finish()])
        with patch('pavlusha_modules.reasoning_code.run_shell',return_value=ShellResult('cmd',False,0,False,'ok','',0)) as shell:
            self.runtime(provider,modules).run()
        self.assertIn('cHJpbnQoNDIpCg==',shell.call_args.args[1])
        self.assertFalse(shell.call_args.kwargs['network'])
    def test_loaded_pause_and_resume_with_same_manifest(self):
        provider=Provider([initialize(),reply({'action':'ask_user','question':'Choose a color'})])
        runtime=self.runtime(provider)
        self.assertEqual(runtime.run(),'Choose a color')
        second=Provider([done(),finish()]);self.runtime(second,user_input='blue').run()
        self.assertIn('USER REPLY:',str(second.requests[0]['messages']))
        self.assertIn('blue',str(second.requests[0]['messages']))
    def test_default_disables_unconfigured_browser_and_expert(self):
        provider=Provider([initialize(),done(),finish()]);runtime=self.runtime(provider);runtime.run()
        self.assertNotIn('browser_evaluate',str(provider.requests[1]['response_format']))
        self.assertNotIn('ask_expert',str(provider.requests[1]['response_format']))
        self.assertIn('configure endpoint and model',str(provider.requests[1]['messages']))
    def test_repeated_action_results_generate_warning(self):
        modules=self.modules();shell=next(m for m in modules if m.name=='shell')
        shell.host.shell_runner=lambda *a,**k:ShellResult('echo',False,0,False,'same','',0)
        action=reply({'action':'shell','command':'echo'})
        provider=Provider([initialize(),action,action,action,done(),finish()])
        self.runtime(provider,modules).run()
        self.assertIn('Repeated action/result cycle detected',str(provider.requests[4]['messages']))
    def test_office_file_errors_do_not_stop_the_loaded_session(self):
        provider=Provider([initialize(),reply({'action':'docx_inspect','path':'missing.docx'}),
            reply({'action':'xlsx_inspect','path':'missing.xlsx'}),done(),finish()])
        runtime=self.runtime(provider)
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(runtime.session.state['status'],'finished')
        self.assertEqual(len(provider.requests),5)
        for index, path in ((2,'missing.docx'),(3,'missing.xlsx')):
            messages=str(provider.requests[index]['messages'])
            self.assertIn(path,messages)
            self.assertIn('file_not_found',messages)
    def test_released_shell_cycle_with_varying_duration_generates_warning(self):
        modules=self.modules();gpu=next(m for m in modules if m.name=='gpu_shell')
        durations=iter((.01,.02,.03));releases=[]
        gpu.host.shell_runner=lambda *a,**k:ShellResult('echo',False,0,False,'same','',next(durations))
        @contextmanager
        def release(*,restore_guard):
            releases.append('release');yield;releases.append('restore')
        action=reply({'action':'shell_released','command':'echo'})
        provider=Provider([initialize(),action,action,action,done(),finish()])
        provider.released_worker=release
        self.assertEqual(self.runtime(provider,modules).run(),'done')
        self.assertIn('Repeated action/result cycle detected',str(provider.requests[4]['messages']))
        self.assertEqual(releases,['release','restore']*3)
    def test_reasoning_loop_interrupts_before_execution(self):
        modules=self.modules();effects=[]
        shell=next(m for m in modules if m.name=='shell')
        shell.host.shell_runner=lambda *a,**k:effects.append(1)
        class Stream(Provider):
            def worker_completion(self,messages,**options):
                result=super().worker_completion(messages,**options)
                if len(self.requests)==2:
                    options['on_delta']('reasoning',('one two three four five six seven eight ' * 180))
                return result
        provider=Stream([initialize(),reply({'action':'shell','command':'must-not-run'}),done(),finish()])
        self.runtime(provider,modules).run()
        self.assertEqual(effects,[])
        self.assertIn('generation_interrupted',str(provider.requests[2]['messages']))
    def test_module_unavailable_dependencies_are_explicit(self):
        from pavlusha_modules.office import Office
        # Validate each Office extension instance has separate legacy state.
        first=Office(self.work,24000);second=Office(self.root/'other',24000)
        first.on_event(Event('session_started','a',0,0));second.on_event(Event('session_started','b',0,0))
        self.assertNotEqual(first.modules['docx']._ROOT,second.modules['docx']._ROOT)
        first.on_event(Event('session_stopped','a',0,0));second.on_event(Event('session_stopped','b',0,0))

    def test_builtin_office_read_and_edit_real_files(self):
        import docx
        import openpyxl
        document=docx.Document();document.add_paragraph('before');document.save(self.work/'sample.docx')
        workbook=openpyxl.Workbook();workbook.active['A1']='before';workbook.save(self.work/'sample.xlsx')
        modules=self.modules();office=next(m for m in modules if m.name=='office')
        contribution=office.on_event(Event('session_started','s',0,0))
        self.addCleanup(lambda:office.on_event(Event('session_stopped','s',0,0)))
        tools={t.name:t for t in contribution.tools}
        doc=tools['docx_inspect'].invoke({'path':'sample.docx'},None).result
        self.assertTrue(doc['ok']); node=doc['items'][0]['id']
        edited=tools['docx_edit'].invoke({'path':'sample.docx','output_path':'edited.docx',
            'revision':doc['revision'],'edits':[{'node_id':node,'old':'before','new':'after'}]},None).result
        self.assertTrue(edited['ok'],edited)
        self.assertEqual(docx.Document(self.work/'edited.docx').paragraphs[0].text,'after')
        book=tools['xlsx_inspect'].invoke({'path':'sample.xlsx'},None).result
        self.assertTrue(book['ok'])
        edited=tools['xlsx_edit'].invoke({'path':'sample.xlsx','output_path':'edited.xlsx',
            'revision':book['revision'],'edits':[{'sheet':'Sheet','cell':'A1','value':'after'}]},None).result
        self.assertTrue(edited['ok'],edited)
        self.assertEqual(openpyxl.load_workbook(self.work/'edited.xlsx').active['A1'].value,'after')
        self.assertEqual(docx.Document(self.work/'sample.docx').paragraphs[0].text,'before')
        self.assertEqual(openpyxl.load_workbook(self.work/'sample.xlsx').active['A1'].value,'before')

    def test_loaded_browser_target_and_expert_policy_are_explicit(self):
        import json
        config=self.root/'configured.json'
        config.write_text(json.dumps({'modules':[
            {'factory':'pavlusha_modules.browser:create_extension','settings':{'config':'adapter.json'}},
            {'factory':'pavlusha_modules.expert:create_extension','settings':{
                'base_url':'https://expert.invalid/v1','model':'consultant','allow_network':False}}]}))
        class Adapter:
            allow_evaluate=True
            def describe(self):return {'target':'test page'}
            def read(self,**kwargs):return {'events':[]}
            def clear(self):return {'cleared':0}
            def evaluate(self,**kwargs):return {'value':2}
            def close(self): self.closed=True
        # Mock the configured target, preserving the browser module's permission boundary.
        adapter=Adapter();adapter.closed=False
        with patch('pavlusha_modules.browser.load_browser_adapter',return_value=adapter), \
             patch('pavlusha_modules.browser.evaluation_available',return_value=True):
            modules=self.modules(config);host=EventHost(modules)
            host.emit(Event('session_started','s',0,0,{'session_dir':str(self.root/'session')}))
            view=host.snapshot();names={t.name for t in view.tools}
            self.assertIn('browser_evaluate',names);self.assertIn('ask_expert',names)
            expert=next(t for t in view.tools if t.name=='ask_expert')
            outcome=expert.invoke({'question':'q','context':'c'},None)
            self.assertEqual(outcome.result['error'],'network_not_granted')
            host.stop(Event('session_stopped','s',0,0))
        self.assertTrue(adapter.closed)

    def test_real_background_process_status_and_cleanup(self):
        import time
        modules=self.modules(); process=next(m for m in modules if m.name=='processes')
        contribution=process.on_event(Event('session_started','s',0,0))
        self.addCleanup(lambda:process.on_event(Event('session_stopped','s',0,0)))
        tools={t.name:t for t in contribution.tools}
        started=tools['process_start'].invoke({'command':'exec python3 -u -c "import time; print(123, flush=True); time.sleep(20)"'},None).result
        self.assertIn('process_id',started,started)
        process_id=started['process_id']
        for _ in range(40):
            logs=tools['process_logs'].invoke({'process_id':process_id},None).result
            if '123' in str(logs):break
            time.sleep(.05)
        self.assertIn('123',str(logs))
        stopped=tools['process_stop'].invoke({'process_id':process_id},None).result
        self.assertNotIn('error',stopped)

    def test_real_reasoning_code_writes_inside_sandbox(self):
        from pavlusha_agent.extensions import InvocationContext
        modules=self.modules(); code=next(m for m in modules if m.name=='reasoning_code')
        contribution=code.on_event(Event('session_started','s',0,0))
        tool=contribution.tools[0]
        outcome=tool.invoke({'destination':'created.py','mode':'create'},
            InvocationContext('s',1,0,{'reasoning_content':'```python\nprint(42)\n```\n'}))
        self.assertEqual(outcome.result['exit_code'],0,outcome.result)
        self.assertEqual((self.work/'created.py').read_text(),'print(42)\n')
        again=tool.invoke({'destination':'created.py','mode':'create'},
            InvocationContext('s',2,0,{'reasoning_content':'```python\nprint(1)\n```\n'}))
        self.assertNotEqual(again.result['exit_code'],0)
        self.assertEqual((self.work/'created.py').read_text(),'print(42)\n')

    def test_real_private_gui_start_observe_and_close(self):
        modules=self.modules();gui=next(m for m in modules if m.name=='gui')
        contribution=gui.on_event(Event('session_started','s',0,0))
        self.addCleanup(lambda:gui.on_event(Event('session_stopped','s',0,0)))
        tools={t.name:t for t in contribution.tools}
        result=tools['gui_start'].invoke({'command':'xmessage -buttons OK:0 Pavlusha',
                                          'delay':1.0},None)
        self.assertEqual(result.result.get('state'),'alive',result.result)
        self.assertEqual(len(result.observations),1)
        self.assertTrue(result.observations[0].image.startswith('data:image/png;base64,'))
        captured=tools['view_gui'].invoke({},None)
        self.assertEqual(len(captured.observations),1)
        tools['gui_close'].invoke({},None)

    def test_office_tools_are_callable_by_model_through_loader(self):
        import docx, openpyxl
        document=docx.Document();document.add_paragraph('office marker');document.save(self.work/'a.docx')
        book=openpyxl.Workbook();book.active['A1']='sheet marker';book.save(self.work/'a.xlsx')
        provider=Provider([initialize(),reply({'action':'docx_inspect','path':'a.docx'}),
            reply({'action':'xlsx_inspect','path':'a.xlsx'}),done(),finish()])
        self.runtime(provider).run()
        self.assertIn('office marker',str(provider.requests[2]['messages']))
        self.assertIn('a.xlsx',str(provider.requests[3]['messages']))
        self.assertIn('workbook',str(provider.requests[3]['messages']))

    def test_denied_gpu_network_is_rejected_before_model_release(self):
        modules=self.modules();events=[]
        @contextmanager
        def release(*,restore_guard):
            events.append('release');yield
        provider=Provider([initialize(),reply({'action':'shell_released','command':'true','network':True}),done(),finish()])
        provider.released_worker=release
        self.runtime(provider,modules).run()
        self.assertEqual(events,[])
        self.assertIn('invalid_action',str(provider.requests[2]['messages']))

    def test_reasoning_references_are_bounded_to_context_epoch(self):
        from pavlusha_agent.extensions import InvocationContext
        code=next(m for m in self.modules() if m.name=='reasoning_code')
        code.on_event(Event('session_started','s',0,0))
        reasoning='```python\nprint(7)\n```\n'
        contribution=code.on_event(Event('turn_completed','s',1,0,
            {'turn':{'turn_id':1,'response':{'reasoning_content':reasoning}}}))
        ref=next(iter(code.store.blocks))
        self.assertIn(ref,contribution.message)
        with patch('pavlusha_modules.reasoning_code.run_shell',
                   return_value=ShellResult('cmd',False,0,False,'ok','',0)) as runner:
            contribution.tools[0].invoke({'destination':'x.py','mode':'create','block_ref':ref},
                InvocationContext('s',2,0,{'reasoning_content':''}))
        self.assertIn('cHJpbnQoNykK',runner.call_args.args[1])
        code.on_event(Event('prepare_turn','s',2,1))
        self.assertNotIn(ref,code.store.blocks)
