"""Authoritative prompt construction and provider-path regression tests."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.core import ProviderTurn, ShellResult
from pavlusha_agent.project_state import empty_project_state, project_state_message
from pavlusha_agent.provider import ChatProvider
from pavlusha_agent.runtime import build_worker_messages, run_agent
from pavlusha_agent.cli import build_parser
from pavlusha_agent.working_context import WorkingContext


def response(content="ok", *, stream=False):
    message = {"content": content, "reasoning_content": "thinking", "tool_calls": []}
    usage = {"prompt_tokens": 200, "completion_tokens": 10}
    if stream:
        chunks = [{"choices": [{"delta": message, "finish_reason": "stop"}]}, {"choices": [], "usage": usage}]
        return io.BytesIO(("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n").encode())
    return io.BytesIO(json.dumps({"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}).encode())


class PromptContractTests(unittest.TestCase):
    def setUp(self):
        self.system = {"role": "system", "content": "immutable system"}
        self.task = {"role": "user", "content": "TASK: authoritative"}
        self.project = empty_project_state()
        self.recent = WorkingContext()

    def build(self, *, notice=None):
        return build_worker_messages(
            self.system, self.task, self.recent,
            project_state_prompt=project_state_message(self.project), context_notice=notice,
        )

    def test_prompt_layers_are_explicit_and_authoritative(self):
        self.recent.append({"role": "user", "content": "step reasoning"}, kind="reasoning")
        self.recent.append({"role": "assistant", "content": "action"})
        self.recent.append({"role": "user", "content": "result"})
        prompt = self.build(notice={"role": "user", "content": "notice"})
        self.assertEqual(prompt[0:2], [self.system, self.task])
        self.assertIn("PROJECT STATE", prompt[2]["content"])
        self.assertEqual(prompt[3:6], self.recent.messages())
        self.assertFalse(any("RAW REASONING" in m["content"] for m in prompt))
        self.assertEqual(prompt[6]["content"], "notice")

    def test_append_only_history_preserves_existing_prefix_when_project_state_unchanged(self):
        self.recent.append({"role": "assistant", "content": "first"})
        before = self.build()
        self.recent.append({"role": "user", "content": "second"})
        after = self.build()
        self.assertEqual(before, after[:len(before)])

    def test_project_state_change_is_reflected_authoritatively(self):
        self.project["initialized"] = True
        before = self.build()
        self.project["revision"] = 7
        after = self.build()
        self.assertNotEqual(before[2], after[2])
        self.assertIn('"revision": 7', after[2]["content"])

    def test_new_task_changes_task_layer_without_restoring_old_history(self):
        self.recent.append({"role": "user", "content": "old history"})
        old = self.build()
        fresh = WorkingContext()
        new = build_worker_messages(
            self.system, {"role": "user", "content": "TASK: new"}, fresh,
            project_state_prompt=project_state_message(empty_project_state()),
        )
        self.assertIn("old history", str(old))
        self.assertNotIn("old history", str(new))


class ProviderPathTests(unittest.TestCase):
    def test_worker_stream_preserves_reasoning_and_full_prompt(self):
        seen = []
        provider = ChatProvider(base_url="http://example/v1", model="m", api_key="", timeout=1, temperature=0.1, max_tokens=10)
        messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]

        def send(request, **kwargs):
            seen.append(json.loads(request.data))
            return response('{"action":"finish","summary":"x"}', stream=True)

        deltas = []
        with patch("urllib.request.urlopen", side_effect=send):
            turn = provider.worker_completion(messages, on_delta=lambda kind, text: deltas.append((kind, text)))
        self.assertEqual(seen[0]["messages"], messages)
        self.assertEqual(turn.reasoning_content, "thinking")
        self.assertTrue(any(kind == "reasoning" for kind, _ in deltas))


class RuntimeArchitectureTests(unittest.TestCase):
    def test_active_runtime_never_calls_state_manager(self):
        turns = iter([
            ProviderTurn(json.dumps({"action": "project_init", "design": [], "work": [
                {"objective": "implement", "status": "ACTIVE", "deliverables": []},
                {"objective": "verify", "status": "PLANNED", "deliverables": []}
            ]}), "plan", "stop", reasoning_tokens=1, prompt_tokens=100),
            ProviderTurn(json.dumps({"action": "shell", "command": "echo ok"}), "work", "stop", reasoning_tokens=1, prompt_tokens=100),
            ProviderTurn(json.dumps({"action": "project_update", "changes": [{
                "op": "update_work", "id": "W001", "status": "DONE", "evidence": ["SHELL RESULT: echo ok -> exit 0"]
            }, {
                "op": "update_work", "id": "W002", "status": "DONE", "evidence": ["SHELL RESULT: echo ok -> exit 0"]
            }]}), "record", "stop", reasoning_tokens=1, prompt_tokens=100),
            ProviderTurn(json.dumps({"action": "finish", "summary": "done"}), "finish", "stop", reasoning_tokens=1, prompt_tokens=100),
        ])

        def worker(provider, messages, **kwargs):
            return next(turns)

        def manager(*args, **kwargs):
            raise AssertionError("reasoning compactor/State Manager was called")

        def shell(workdir, command, **kwargs):
            return ShellResult(command=command, network=False, exit_code=0, timed_out=False,
                               duration=0.01, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp, \
             patch("pavlusha_agent.runtime.shutil.which", return_value="/fake/bwrap"), \
             patch("pavlusha_agent.provider.ChatProvider.worker_completion", worker), \
             patch("pavlusha_agent.provider.ChatProvider.stateless_completion", manager), \
             patch("pavlusha_agent.runtime.run_shell", side_effect=shell), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            args = build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off', "--workdir", tmp + "/work", "--state-dir", tmp + "/state",
                                              "--model", "m", "--worker-context-budget", "40000",
                                              "--raw-reasoning-limit", "999", "--project-review-every", "0", "task"])
            self.assertEqual(run_agent(args), 0)
            state = json.loads((Path(tmp) / "state" / "state.json").read_text())
            self.assertNotIn("reasoning_memory", state)


if __name__ == "__main__":
    unittest.main()
