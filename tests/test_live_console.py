"""Human-facing progress leaves actions and technical persistence unchanged."""
import io
import tempfile
import unittest
from pathlib import Path

from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.core import ShellResult
from pavlusha_agent.live import LiveConsoleRenderer
from pavlusha_agent import __version__
from pavlusha_agent.runtime import Runtime
from tests.test_events import Provider, reply


class LiveConsoleTests(unittest.TestCase):
    def test_start_banner_shows_current_release(self):
        for plain in (False, True):
            stream=io.StringIO(); renderer=LiveConsoleRenderer(stream=stream,color=False)
            if plain:
                renderer._console=None
            renderer.start(model='example',task='example',max_steps=1)
            self.assertEqual(__version__, '0.8.0')
            self.assertIn(__version__,stream.getvalue())

    def test_streamed_reasoning_shell_and_result_are_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); stream=io.StringIO(); renderer=LiveConsoleRenderer(stream=stream,color=False)
            class StreamProvider(Provider):
                def worker_completion(self,messages,**options):
                    options['on_delta']('reasoning','Проверяю результат. ')
                    return super().worker_completion(messages,**options)
            provider=StreamProvider([reply({'action':'shell','command':'printf ok'}),
                                     reply({'action':'finish','summary':'done'})])
            runner=lambda *a,**kw:ShellResult('printf ok',False,0,False,'ok\n','',.01)
            runtime=Runtime(provider,task='check',session_dir=root/'session',
                            extensions=[BasicHost(root,shell_runner=runner)],live=True,renderer=renderer)
            self.assertEqual(runtime.run(),'done')
            output=stream.getvalue()
            self.assertIn('STEP 1/',output)
            self.assertIn('Проверяю результат.',output)
            self.assertIn('SHELL',output)
            self.assertIn('printf ok',output)
            self.assertIn('ok\n',output)
            self.assertIn('FINISH',output)
            self.assertNotIn('"action": "shell"',output)
            self.assertTrue((root/'session/generation.jsonl').is_file())

    def test_unknown_function_and_error_remain_visible(self):
        stream=io.StringIO();renderer=LiveConsoleRenderer(stream=stream,color=False)
        renderer.action('browser_evaluate',{'action':'browser_evaluate','expression':'document.title'})
        renderer.turn_result(1,{'action':'browser_evaluate'},{'error':'denied','detail':'no access'})
        self.assertIn('browser_evaluate',stream.getvalue())
        self.assertIn('document.title',stream.getvalue())
        self.assertIn('denied: no access',stream.getvalue())
