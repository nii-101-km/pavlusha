"""Core parsing, sandbox construction, persistence, and provider transport checks."""
import io
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError, ShellResult, _extract_json_object
from pavlusha_agent.provider import ChatProvider
from pavlusha_agent.sandbox import build_bwrap_command, validate_action
from pavlusha_agent.state_store import StateStore


class ParseTests(unittest.TestCase):
    def test_clean_json(self):
        got = _extract_json_object('{"action":"finish","summary":"ok"}')
        self.assertEqual(got["action"], "finish")

    def test_fenced_json(self):
        got = _extract_json_object('```json\n{"action":"shell","command":"pwd"}\n```')
        self.assertEqual(got["command"], "pwd")

    def test_chatter_json(self):
        got = _extract_json_object('thinking... {"action":"shell","command":"ls"} trailing')
        self.assertEqual(got["command"], "ls")

    def test_malformed_outer_action_never_extracts_nested_objects(self):
        for nested in ({"op": "add_design", "decision": "observed", "rationale": "evidence"},
                       {"action": "shell", "command": "must not execute"}):
            malformed = json.dumps({"action": "project_update", "changes": [nested]})[:-1]
            for raw in (malformed, 'thinking... ' + malformed, '```json\n' + malformed + '\n```'):
                with self.subTest(raw=raw), self.assertRaisesRegex(AgentError, 'malformed JSON action'):
                    _extract_json_object(raw)

    def test_timeout_is_clamped(self):
        kind, data = validate_action(
            {"action": "shell", "command": "sleep 10", "timeout": 9999}, 30
        )
        self.assertEqual(kind, "shell")
        self.assertEqual(data["timeout"], 30)


class BwrapTests(unittest.TestCase):
    def test_offline_has_no_share_net(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = build_bwrap_command(Path(tmp), "pwd", network=False)
        self.assertIn("--unshare-all", cmd)
        self.assertNotIn("--share-net", cmd)
        self.assertIn("--clearenv", cmd)
        self.assertIn("/work", cmd)

    def test_network_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = build_bwrap_command(Path(tmp), "pwd", network=True)
        self.assertIn("--share-net", cmd)

    def test_home_is_not_bound(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "pavlusha_agent.sandbox._existing_system_paths", return_value=["/usr", "/etc"]
        ):
            cmd = build_bwrap_command(Path(tmp), "pwd", network=False)
        pairs = list(zip(cmd, cmd[1:]))
        self.assertNotIn(("--ro-bind", "/home"), pairs)
        self.assertNotIn(("--bind", "/home"), pairs)


class StateStoreTests(unittest.TestCase):

    def test_state_is_created_with_immutable_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), "do thing")
            state = store.load()
            self.assertEqual(state["task"]["original"], "do thing")
            self.assertTrue(state["task"]["immutable"])
            self.assertEqual(state["version"], 0)

    def test_existing_state_rejects_different_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            StateStore(Path(tmp), "task A")
            with self.assertRaises(AgentError):
                StateStore(Path(tmp), "task B")


    def test_operation_ledger_is_controller_owned_and_persistent(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "state", "task")
            result = ShellResult(
                command="python -m unittest", network=False, exit_code=0, timed_out=False,
                duration=1.25, stdout="Ran 270 tests\nOK\n", stderr="")
            op = store.record_operation(result.as_dict())
            self.assertEqual(op["id"], "OP0001")
            state = store.load()
            self.assertEqual(len(state["operations"]), 1)
            self.assertEqual(state["operations"][0]["command"], "python -m unittest")
            self.assertIn("270 tests", state["operations"][0]["result_excerpt"] )
            self.assertEqual(state["counters"]["operation"], 1)
            self.assertEqual(state["operations"][0]["duration_seconds"], 1.25)
            event = json.loads(store.log_path.read_text().splitlines()[-1])
            self.assertEqual(event["operation"]["duration_seconds"], 1.25)

    def test_old_state_schemas_are_rejected_without_rewriting_files(self):
        for schema in (1, 2):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / "state"
                store = StateStore(directory, "task")
                state = store.load()
                state["schema_version"] = schema
                store._write_atomic(state)
                before = store.state_path.read_bytes()
                with self.assertRaises(AgentError):
                    StateStore(directory, "task")
                self.assertEqual(store.state_path.read_bytes(), before)
                with self.assertRaisesRegex(AgentError, "RECOVERY FAILED"):
                    StateStore(directory, "task", cold_restart=True)
                self.assertEqual(store.state_path.read_bytes(), before)



class ProviderUsageTests(unittest.TestCase):
    def test_complete_turn_captures_provider_usage(self):
        provider = ChatProvider(
            base_url="http://example.invalid/v1",
            model="test-model",
            api_key="",
            timeout=1.0,
            temperature=0.1,
            max_tokens=8192,
        )
        body = {
            "choices": [{
                "message": {"content": '{"action":"finish","summary":"ok"}', "reasoning_content": "think"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 28123,
                "completion_tokens": 612,
                "completion_tokens_details": {"reasoning_tokens": 500},
            },
        }
        fake = io.BytesIO(json.dumps(body).encode("utf-8"))
        with mock.patch.object(urllib.request, "urlopen", return_value=fake):
            turn = provider.complete_turn([{"role": "user", "content": "hi"}])
        self.assertEqual(turn.prompt_tokens, 28123)
        self.assertEqual(turn.completion_tokens, 612)
        self.assertEqual(turn.reasoning_tokens, 500)


class LiveModeTests(unittest.TestCase):
    def test_parser_exposes_live_default_and_opt_out(self):
        args = build_parser().parse_args(["task"])
        self.assertTrue(args.live)
        args = build_parser().parse_args(["--live", "task"])
        self.assertTrue(args.live)
        self.assertFalse(build_parser().parse_args(["--no-live", "task"]).live)

    def test_streaming_provider_reconstructs_turn_and_emits_deltas(self):
        import io
        from unittest.mock import patch

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self):
                chunks = [
                    {"choices": [{"delta": {"reasoning_content": "think "}, "finish_reason": None}]},
                    {"choices": [{"delta": {"reasoning_content": "more"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"content": '{"action":"finish",'}, "finish_reason": None}]},
                    {"choices": [{"delta": {"content": '"summary":"ok"}'}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 17,
                     "completion_tokens_details": {"reasoning_tokens": 9}}},
                ]
                for chunk in chunks:
                    yield ("data: " + json.dumps(chunk) + "\n").encode()
                yield b"data: [DONE]\n"

        provider = ChatProvider("http://example/v1", "model", "", 10, 0.0, 100)
        deltas = []
        with patch("urllib.request.urlopen", return_value=Response()):
            turn = provider.complete_turn_stream(
                [{"role": "user", "content": "x"}],
                on_delta=lambda kind, text: deltas.append((kind, text)),
            )
        self.assertEqual(turn.reasoning_content, "think more")
        self.assertEqual(turn.content, '{"action":"finish","summary":"ok"}')
        self.assertEqual(turn.prompt_tokens, 123)
        self.assertEqual(turn.reasoning_tokens, 9)
        self.assertEqual(deltas[0], ("reasoning", "think "))
        self.assertEqual(deltas[-1], ("content", '"summary":"ok"}'))


class BigLidDoorTests(unittest.TestCase):
    def test_small_shell_output_passes_through_unchanged(self):
        from pavlusha_agent.core import ShellResult
        from pavlusha_agent.sandbox import admit_shell_result
        result = ShellResult(command="printf ok", network=False, exit_code=0, timed_out=False,
                             stdout="ok\n", stderr="", duration=0.01)
        payload = admit_shell_result(result, 1000)
        self.assertEqual(payload["stdout"], "ok\n")
        self.assertEqual(payload["stderr"], "")
        self.assertNotIn("output_withheld", payload)

    def test_oversized_shell_output_is_withheld_wholesale(self):
        from pavlusha_agent.core import ShellResult
        from pavlusha_agent.sandbox import admit_shell_result
        result = ShellResult(command="cat huge", network=False, exit_code=7, timed_out=False,
                             stdout="x" * 700, stderr="e" * 400, duration=0.01)
        payload = admit_shell_result(result, 1000)
        self.assertEqual(payload["stdout"], "")
        self.assertEqual(payload["stderr"], "")
        self.assertTrue(payload["output_withheld"])
        self.assertEqual(payload["output_chars"], 1100)
        self.assertEqual(payload["output_limit_chars"], 1000)
        self.assertEqual(payload["exit_code"], 7)
        self.assertIn("OUTPUT_WITHHELD", payload["output_notice"])
        self.assertIn("narrower", payload["output_notice"])


if __name__ == "__main__":
    unittest.main()
