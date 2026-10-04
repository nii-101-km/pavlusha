"""Bounded shell release uses the normal ledger/checkpoint and a cold Worker prompt."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.provider import ChatProvider
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.sandbox import run_shell, validate_action
from pavlusha_agent.state_store import StateStore
from pavlusha_agent.checkpoint import validate_checkpoint
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done


class ReleaseRuntimeTests(unittest.TestCase):
    def run_case(self, root, *, release=True, outcome=None, network=False,
                 restore_error=False, checkpoint_error=False, ledger_error=False, gpu=False):
        events, seen = [], []
        replies = iter([init_turn(reasoning='OLD_PLAN'), done('prior evidence'),
                        turn({'action':'shell', 'command':'selected command',
                              'release_worker':release, 'network':network, 'timeout':1, 'gpu':gpu},
                             reasoning='OLD_SHELL_THOUGHT'),
                        turn({'action':'finish','summary':'done'})])
        def worker(provider, messages, **kwargs):
            seen.append(copy.deepcopy(messages))
            return next(replies)
        @contextmanager
        def released(provider):
            state = StateStore(root/'state', 'task').load()
            checkpoint = validate_checkpoint(state, 'task')
            self.assertEqual(checkpoint['generation'], 2)
            self.assertEqual(checkpoint['operation_count'], 0)
            self.assertEqual(checkpoint['project_state'], state['project_state'])
            events.append('committed')
            try:
                events.append('release')
                yield
            finally:
                events.append('restore')
                if restore_error:
                    raise AgentError('restore failed')
        def shell(workdir, command, **kwargs):
            events.append('shell')
            self.assertEqual(kwargs['timeout'], 1)
            self.assertEqual(kwargs.get('gpu', False), gpu)
            (workdir/'current.py').write_text('CURRENT_WORLD = True\n')
            if isinstance(outcome, Exception):
                raise outcome
            return outcome or ShellResult(command, False, 0, False, 'OK', '', .01)
        args = build_parser().parse_args([
            '--workdir', str(root/'work'), '--state-dir', str(root/'state'),
            '--model', 'scripted', '--worker-context-budget', '40000',
            '--project-review-every', '0', '--max-steps', '4', '--project-map','on', 'task'])
        original = StateStore.complete_project_review
        original_record = StateStore.record_operation
        def commit(store, **kwargs):
            if checkpoint_error:
                raise OSError('checkpoint publication failed')
            return original(store, **kwargs)
        def record(store, payload):
            if ledger_error:
                raise OSError('ledger publication failed')
            return original_record(store, payload)
        with patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
             patch.object(ChatProvider, 'worker_completion', worker), \
             patch.object(ChatProvider, 'released_worker', released), \
             patch.object(StateStore, 'complete_project_review', commit), \
             patch.object(StateStore, 'record_operation', record), \
             patch('pavlusha_agent.runtime.run_shell', shell), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            error = None
            try:
                result = run_agent(args)
            except (AgentError, OSError) as exc:
                result, error = None, str(exc)
        state = StateStore(root/'state', 'task').load()
        return events, seen, state, result, error

    def test_default_and_explicit_false_preserve_history_and_checkpoint(self):
        base = {'action':'shell','command':'echo OK'}
        self.assertEqual(validate_action(base, 20), validate_action({**base,'release_worker':False}, 20))
        for requested, expected in ((7,7), (9999,20), (0,1)):
            kind, data = validate_action({**base, 'release_worker':True, 'timeout':requested}, 20)
            self.assertEqual(data['timeout'], expected)
            self.assertTrue(data['release_worker'])
        for value in (True, 1.5, '300', None):
            with self.subTest(timeout=value), self.assertRaises(AgentError):
                validate_action({**base,'release_worker':True,'timeout':value}, 20)
        for value in (None, 1, 'true', [], {}):
            with self.subTest(value=value), self.assertRaises(AgentError):
                validate_action({**base,'release_worker':value}, 20)
        with tempfile.TemporaryDirectory() as tmp:
            events, seen, state, result, error = self.run_case(Path(tmp), release=False)
            self.assertEqual(result, 0, error)
            self.assertEqual(events, ['shell'])
            self.assertEqual(state['recovery_checkpoint']['generation'], 1)
            self.assertIn('OLD_PLAN', str(seen[-1]))

    def test_success_nonzero_timeout_and_exceptions_restore_and_cold_resume(self):
        outcomes = [None, ShellResult('selected command',False,7,False,'','failed',.01),
                    ShellResult('selected command',False,-15,True,'partial','',1.1),
                    OSError('launch failed'), RuntimeError('execution failed')]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                events, seen, state, result, error = self.run_case(root, outcome=outcome)
                self.assertEqual(result, 0, error)
                self.assertEqual(events, ['committed','release','shell','restore'])
                self.assertNotIn('OLD_PLAN', str(seen[-1]))
                self.assertNotIn('OLD_SHELL_THOUGHT', str(seen[-1]))
                self.assertIn('current.py', str(seen[-1]))
                self.assertIn('SHELL RESULT (OP0001)', str(seen[-1]))
                self.assertEqual(state['counters']['operation'], 1)
                checkpoint = validate_checkpoint(state, 'task')
                self.assertEqual(checkpoint['generation'], 2)
                self.assertEqual(checkpoint['operation_count'], 0)
                self.assertEqual([w['status'] for w in checkpoint['project_state']['work']], ['DONE','DONE'])
                if isinstance(outcome, ShellResult):
                    self.assertEqual(state['operations'][0]['exit_code'], outcome.exit_code)
                    self.assertEqual(state['operations'][0]['timed_out'], outcome.timed_out)
                if isinstance(outcome, Exception):
                    self.assertIn(str(outcome), state['operations'][0]['result_excerpt'])
                recovered = StateStore(root/'state','task',cold_restart=True).load()
                self.assertEqual(recovered['project_state'], checkpoint['project_state'])
                self.assertEqual(recovered['operations'], state['operations'])
                self.assertTrue((root/'work/current.py').exists())

    def test_denied_network_and_failed_checkpoint_never_release_or_execute(self):
        for options in ({'network':True}, {'checkpoint_error':True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                events, seen, state, result, error = self.run_case(Path(tmp), **options)
                self.assertEqual(events, [])
                self.assertEqual(state['recovery_checkpoint']['generation'], 1)
                if options.get('network'):
                    self.assertIn('network_not_granted', str(seen[-1]))
                else:
                    self.assertIn('checkpoint publication failed', error)

    def test_failed_restore_stops_but_preserves_result_and_current_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events, seen, state, result, error = self.run_case(root, restore_error=True)
            self.assertIn('restore failed', error)
            self.assertEqual(len(seen), 3)
            self.assertEqual(state['operations'][0]['exit_code'], 0)
            self.assertTrue((root/'work/current.py').exists())
            self.assertEqual(validate_checkpoint(state,'task')['generation'],2)

    def test_gpu_is_independent_and_composes_with_worker_release(self):
        for release in (False, True):
            for outcome in (None, OSError('GPU device unavailable'),
                            ShellResult('selected command',False,-15,True,'','',1)):
                with self.subTest(release=release, outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                    events, seen, state, result, error = self.run_case(
                        Path(tmp), release=release, gpu=True, outcome=outcome)
                    self.assertEqual(result, 0, error)
                    self.assertEqual(events, ['committed','release','shell','restore'] if release else ['shell'])
                    self.assertEqual(state['recovery_checkpoint']['generation'], 2 if release else 1)

    def test_ledger_exception_after_execution_still_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            events, seen, state, result, error = self.run_case(Path(tmp), ledger_error=True)
            self.assertEqual(events, ['committed','release','shell','restore'])
            self.assertIn('ledger publication failed', error)

    def test_existing_process_group_timeout_runs_inside_release_scope(self):
        events=[]
        @contextmanager
        def released():
            events.append('release')
            try:
                yield
            finally:
                events.append('restore')
        with tempfile.TemporaryDirectory() as tmp, \
             patch('pavlusha_agent.sandbox.build_bwrap_command', return_value=['/bin/bash','-c','sleep 30 & wait']):
            with released():
                result=run_shell(Path(tmp),'sleep 30 & wait',network=False,timeout=1,output_limit=200)
                events.append('timeout' if result.timed_out else 'finished')
            self.assertEqual(events,['release','timeout','restore'])
            self.assertLess(result.duration, 5)


class ReleaseProviderTests(unittest.TestCase):
    def provider(self, *, config=None, failure=None):
        provider = ChatProvider('http://localhost:1234/v1','worker-instance','',3,0,100)
        instance = {'id':'worker-instance', 'config':config if config is not None else {'context_length':40000,'flash_attention':True}}
        loaded=[{'type':'llm','key':'worker-key','loaded_instances':[copy.deepcopy(instance)]}]
        calls=[]
        def request(path,payload=None):
            calls.append((path,copy.deepcopy(payload)))
            if path=='':
                return {'models':copy.deepcopy(loaded)}
            if path=='/unload':
                loaded[0]['loaded_instances']=[]
                if failure=='unload':
                    raise AgentError('lost unload response')
                return {'instance_id':instance['id']}
            if path=='/load':
                if failure=='load':
                    raise AgentError('reload unavailable')
                restored={'id':'new-worker-instance','config':{
                    k:copy.deepcopy(v) for k,v in payload.items()
                    if k not in {'model','echo_load_config'}}}
                loaded[0]['loaded_instances']=[restored]
                return {'instance_id':restored['id'],'status':'loaded','load_config':restored['config']}
            self.fail(path)
        provider._model_lifecycle_request=request
        return provider, calls, loaded

    def test_real_adapter_contract_and_restore_after_body_exceptions(self):
        for error in (None, RuntimeError('execution exception'), KeyboardInterrupt()):
            with self.subTest(error=error):
                provider,calls,loaded=self.provider()
                try:
                    with provider.released_worker():
                        self.assertEqual(loaded[0]['loaded_instances'], [])
                        if error is not None:
                            raise error
                except BaseException as caught:
                    self.assertIs(caught,error)
                self.assertEqual(provider.model,'new-worker-instance')
                self.assertEqual([p for p,_ in calls],['','/unload','','','/load',''])
                self.assertEqual(calls[-2][1],{'model':'worker-key','context_length':40000,
                                             'flash_attention':True,'echo_load_config':True})

    def test_lost_unload_response_still_restores_without_running_shell(self):
        provider,calls,loaded=self.provider(failure='unload')
        with self.assertRaisesRegex(AgentError,'lost unload response'):
            with provider.released_worker():
                self.fail('shell must not run')
        self.assertEqual(provider.model,'new-worker-instance')
        self.assertEqual(calls[-2][0],'/load')

    def test_failed_restore_is_explicit(self):
        provider,calls,loaded=self.provider(failure='load')
        with self.assertRaisesRegex(AgentError,'Worker restoration failed; runtime stopped'):
            with provider.released_worker():
                pass

    def test_failed_status_after_release_does_not_skip_reload_attempt(self):
        provider,calls,loaded=self.provider()
        request=provider._model_lifecycle_request
        def intermittent(path,payload=None):
            if path=='' and len(calls)==3:
                raise AgentError('status unavailable')
            return request(path,payload)
        provider._model_lifecycle_request=intermittent
        with provider.released_worker():
            pass
        self.assertEqual(calls[-2][0],'/load')
        self.assertEqual(provider.model,'new-worker-instance')

    def test_still_loaded_instance_does_not_launch_shell_or_duplicate_model(self):
        provider,calls,loaded=self.provider()
        request=provider._model_lifecycle_request
        def no_unload(path,payload=None):
            if path=='/unload':
                calls.append((path,payload))
                return {'instance_id':'worker-instance'}
            return request(path,payload)
        provider._model_lifecycle_request=no_unload
        with self.assertRaisesRegex(AgentError,'remains loaded'):
            with provider.released_worker():
                self.fail('shell must not run')
        self.assertNotIn('/load',[p for p,_ in calls])

    def test_ambiguous_worker_still_refused_before_unload(self):
        provider,calls,loaded=self.provider()
        loaded[0]['loaded_instances'].append(copy.deepcopy(loaded[0]['loaded_instances'][0]))
        with self.assertRaisesRegex(AgentError,'exactly one'):
            with provider.released_worker():
                self.fail('shell must not run')
        self.assertEqual([p for p,_ in calls],[''])

    def test_supported_fields_preserved_and_unknown_fields_use_defaults(self):
        config={'context_length':117248,'eval_batch_size':512,'flash_attention':True,
                'num_experts':4,'offload_kv_cache_to_gpu':True,'parallel':4,
                'physical_batch_size':512,'context_checkpoints':32,'reasoning_budget_message':'',
                'speculative_draft_mtp':True,'speculative_draft_simple':False,
                'speculative_draft_model':'','speculative_draft_max_tokens':3,
                'speculative_draft_min_tokens':0,'speculative_draft_min_continue_probability':0,
                'prompt_template':{'type':'jinja','template':'{{ messages }}','stop_strings':[]},
                'ttl_seconds':300,'unknown_setting':{'performance':'deployment setting'}}
        provider,calls,loaded=self.provider(config=config)
        with provider.released_worker():
            self.assertEqual(loaded[0]['loaded_instances'], [])
        self.assertEqual(provider.model,'new-worker-instance')
        self.assertEqual(calls[-2],('/load',{
            'model':'worker-key','echo_load_config':True,
            **{k:v for k,v in config.items() if k!='unknown_setting'}}))

    def test_empty_or_only_unsupported_config_does_not_block_restore(self):
        for config in ({}, {'unknown_setting':42}):
            with self.subTest(config=config):
                provider,calls,loaded=self.provider(config=config)
                with provider.released_worker():
                    pass
                self.assertEqual(calls[-2],('/load',{'model':'worker-key','echo_load_config':True}))
                self.assertEqual(provider.model,'new-worker-instance')

    def test_configuration_differences_or_missing_echo_do_not_block_restore(self):
        for echo in (None, {}, {'context_length':20000,'physical_batch_size':128,
                               'speculative_draft_mtp':False,'parallel':1}):
            with self.subTest(echo=echo):
                provider,calls,loaded=self.provider(config={
                    'context_length':40000,'physical_batch_size':512,
                    'speculative_draft_mtp':True,'parallel':4})
                request=provider._model_lifecycle_request
                def different_config(path,payload=None):
                    reply=request(path,payload)
                    if path=='/load':
                        reply.pop('load_config')
                        if echo is not None:
                            reply['load_config']=echo
                        loaded[0]['loaded_instances'][0]['config']=echo or {}
                    return reply
                provider._model_lifecycle_request=different_config
                with provider.released_worker():
                    pass
                self.assertEqual(provider.model,'new-worker-instance')

    def test_wrong_model_key_or_missing_instance_still_fails_restore(self):
        for change in ('key','instance'):
            with self.subTest(change=change):
                provider,calls,loaded=self.provider()
                request=provider._model_lifecycle_request
                def wrong_model(path,payload=None):
                    reply=request(path,payload)
                    if path=='/load':
                        if change=='key':
                            loaded[0]['key']='other-model-key'
                        else:
                            loaded[0]['loaded_instances']=[]
                    return reply
                provider._model_lifecycle_request=wrong_model
                with self.assertRaisesRegex(AgentError,'did not confirm the same model key'):
                    with provider.released_worker():
                        pass

    def test_same_model_with_changed_instance_still_fails_exclusive_use_check(self):
        provider,calls,loaded=self.provider()
        with self.assertRaisesRegex(AgentError,'instance changed during release'):
            with provider.released_worker():
                loaded[0]['loaded_instances']=[{'id':'external-instance','config':{}}]

    def test_http_endpoint_auth_and_timeout(self):
        provider=ChatProvider('http://localhost:1234/v1','worker','secret',7,0,100)
        class Response(io.BytesIO):
            pass
        with patch('pavlusha_agent.provider.urllib.request.urlopen',return_value=Response(b'{"instance_id":"worker"}')) as urlopen:
            provider._model_lifecycle_request('/unload',{'instance_id':'worker'})
        request=urlopen.call_args.args[0]
        self.assertEqual(request.full_url,'http://localhost:1234/api/v1/models/unload')
        self.assertEqual(request.get_header('Authorization'),'Bearer secret')
        self.assertEqual(json.loads(request.data),{'instance_id':'worker'})
        self.assertEqual(urlopen.call_args.kwargs['timeout'],7)
