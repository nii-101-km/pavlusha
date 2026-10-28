"""Expert transport regression tests with an isolated, deterministic endpoint."""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pavlusha_modules.bundled._expert import Expert


class ExpertTransportTests(unittest.TestCase):
    def expert(self):
        return Expert(SimpleNamespace(expert_base_url='https://example.invalid/v1',
            expert_model='test-model', expert_transport='perplexity-agent', network=True))

    def test_perplexity_omits_empty_context_but_preserves_nonempty_context(self):
        response = {'status':'completed', 'output':[{'type':'message','role':'assistant',
            'status':'completed','content':[{'type':'output_text','text':'42'}]}]}
        for context in ('', '  \n  ', 'Relevant context'):
            with self.subTest(context=context):
                with patch('urllib.request.urlopen',
                           return_value=io.BytesIO(json.dumps(response).encode())) as endpoint:
                    result=self.expert().ask('What is 6 times 7?',context)
                request=endpoint.call_args.args[0]
                payload=json.loads(request.data)
                self.assertEqual(result['answer'],'42')
                self.assertEqual(payload['input'][0]['content'],'What is 6 times 7?')
                self.assertEqual(len(payload['input']), 2 if context.strip() else 1)
                if context.strip():
                    self.assertEqual(payload['input'][1]['content'],context)


if __name__ == '__main__':
    unittest.main()
