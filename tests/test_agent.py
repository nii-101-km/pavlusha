import importlib.util
import io
import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def state_tool(name, **arguments):
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def patch_to_state_tools(patch):
    calls = []
    focus = patch.get("focus_update", {})
    if focus.get("set"):
        calls.append(state_tool("set_focus", value=focus.get("value", "")))
    for item in patch.get("verified_add", []):
        calls.append(state_tool("add_verified", text=item["text"], basis=item["basis"]))
    for item in patch.get("hypotheses_add", []):
        calls.append(state_tool("add_hypothesis", **item))
    for item in patch.get("hypotheses_update", []):
        calls.append(state_tool("update_hypothesis", **item))
    for item in patch.get("unresolved_add", []):
        calls.append(state_tool("add_unresolved", **item))
    for item in patch.get("unresolved_resolve", []):
        calls.append(state_tool("resolve_unresolved", **item))
    for item in patch.get("inspected_add", []):
        calls.append(state_tool("add_inspected", **item))
    for item in patch.get("changes_add", []):
        calls.append(state_tool("add_change", **item))
    for item in patch.get("failures_add", []):
        calls.append(state_tool("add_failure", **item))
    for item in patch.get("loop_signals_add", []):
        calls.append(state_tool("add_loop_signal", **item))
    return calls or [state_tool("no_state_change")]
spec = importlib.util.spec_from_file_location("agent", ROOT / "agent.py")
agent = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent
assert spec.loader is not None
spec.loader.exec_module(agent)


class ParseTests(unittest.TestCase):
    def test_clean_json(self):
        got = agent._extract_json_object('{"action":"finish","summary":"ok"}')
        self.assertEqual(got["action"], "finish")

    def test_fenced_json(self):
        got = agent._extract_json_object('```json\n{"action":"shell","command":"pwd"}\n```')
        self.assertEqual(got["command"], "pwd")

    def test_chatter_json(self):
        got = agent._extract_json_object('thinking... {"action":"shell","command":"ls"} trailing')
        self.assertEqual(got["command"], "ls")

    def test_timeout_is_clamped(self):
        kind, data = agent.validate_action(
            {"action": "shell", "command": "sleep 10", "timeout": 9999}, 30
        )
        self.assertEqual(kind, "shell")
        self.assertEqual(data["timeout"], 30)


class BwrapTests(unittest.TestCase):
    def test_offline_has_no_share_net(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = agent.build_bwrap_command(Path(tmp), "pwd", network=False)
        self.assertIn("--unshare-all", cmd)
        self.assertNotIn("--share-net", cmd)
        self.assertIn("--clearenv", cmd)
        self.assertIn("/work", cmd)

    def test_network_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = agent.build_bwrap_command(Path(tmp), "pwd", network=True)
        self.assertIn("--share-net", cmd)

    def test_home_is_not_bound(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            agent, "_existing_system_paths", return_value=["/usr", "/etc"]
        ):
            cmd = agent.build_bwrap_command(Path(tmp), "pwd", network=False)
        pairs = list(zip(cmd, cmd[1:]))
        self.assertNotIn(("--ro-bind", "/home"), pairs)
        self.assertNotIn(("--bind", "/home"), pairs)


class StateStoreTests(unittest.TestCase):
    def _patch(self):
        return {
            "focus_update": {"set": True, "value": "inspect provider CLI"},
            "verified_add": [{"text": "270 tests pass", "basis": "unittest output", "source": "transcript"}],
            "hypotheses_add": [{
                "text": "fenced JSON parsing edge case",
                "status": "uncertain",
                "basis": "failed reasoning identified it",
                "source": "reasoning",
            }],
            "hypotheses_update": [],
            "unresolved_add": [{"text": "find one concrete defect", "basis": "task not complete", "source": "transcript"}],
            "unresolved_resolve": [],
            "inspected_add": [{"path": "runtime_debugger/provider.py", "basis": "cat command", "source": "transcript"}],
            "changes_add": [],
            "failures_add": [],
            "loop_signals_add": [{"text": "provider budget logic revisited", "basis": "failed reasoning", "source": "reasoning"}],
        }

    def test_state_is_created_with_immutable_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "do thing")
            state = store.load()
            self.assertEqual(state["task"]["original"], "do thing")
            self.assertTrue(state["task"]["immutable"])
            self.assertEqual(state["version"], 0)

    def test_existing_state_rejects_different_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent.StateStore(Path(tmp), "task A")
            with self.assertRaises(agent.AgentError):
                agent.StateStore(Path(tmp), "task B")

    def test_patch_updates_state_without_replacing_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "do thing")
            new_state, applied = store.apply_patch(self._patch())
            self.assertEqual(new_state["task"]["original"], "do thing")
            self.assertEqual(new_state["version"], 1)
            self.assertEqual(new_state["current_focus"], "inspect provider CLI")
            self.assertEqual(new_state["verified"][0]["id"], "V0001")
            self.assertEqual(new_state["hypotheses"][0]["id"], "H0001")
            self.assertIn("H0001", applied)

    def test_patch_updates_hypothesis_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "do thing")
            store.apply_patch(self._patch())
            patch = self._patch()
            patch["focus_update"] = {"set": False, "value": ""}
            patch["verified_add"] = []
            patch["hypotheses_add"] = []
            patch["unresolved_add"] = []
            patch["inspected_add"] = []
            patch["loop_signals_add"] = []
            patch["hypotheses_update"] = [{"id": "H0001", "status": "rejected", "basis": "regression test disproved it"}]
            state, _ = store.apply_patch(patch)
            self.assertEqual(state["hypotheses"][0]["status"], "rejected")
            self.assertEqual(state["version"], 2)

    def test_invalid_reference_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "do thing")
            before = store.load()
            patch = self._patch()
            patch["hypotheses_update"] = [{"id": "H9999", "status": "rejected", "basis": "nope"}]
            with self.assertRaises(agent.AgentError):
                store.apply_patch(patch)
            after = store.load()
            self.assertEqual(before, after)

    def test_pending_reasoning_survives_until_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "do thing")
            store.add_pending_reasoning("loop loop", "length")
            got = store.load_pending_reasoning()
            self.assertEqual(got[0]["reasoning"], "loop loop")
            store.clear_pending_reasoning()
            self.assertEqual(store.load_pending_reasoning(), [])


    def test_operation_ledger_is_controller_owned_and_persistent(self):
        with tempfile.TemporaryDirectory() as td:
            store = agent.StateStore(Path(td) / "state", "task")
            op = store.record_operation({
                "command": "python -m unittest",
                "network": False,
                "exit_code": 0,
                "timed_out": False,
                "duration": 1.25,
                "stdout": "Ran 270 tests\nOK\n",
                "stderr": "",
            })
            self.assertEqual(op["id"], "OP0001")
            state = store.load()
            self.assertEqual(len(state["operations"]), 1)
            self.assertEqual(state["operations"][0]["command"], "python -m unittest")
            self.assertIn("270 tests", state["operations"][0]["result_excerpt"] )
            self.assertEqual(state["counters"]["operation"], 1)

    def test_schema_v1_migrates_with_empty_operation_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            store = agent.StateStore(state_dir, "task")
            state = store.load()
            state["schema_version"] = 1
            state.pop("operations", None)
            state["counters"].pop("operation", None)
            store._write_atomic(state)
            migrated = agent.StateStore(state_dir, "task").load()
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["operations"], [])
            self.assertEqual(migrated["counters"]["operation"], 0)


class ProviderUsageAndCycleTriggerTests(unittest.TestCase):
    def test_complete_turn_captures_provider_usage(self):
        provider = agent.ChatProvider(
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
        with mock.patch.object(agent.urllib.request, "urlopen", return_value=fake):
            turn = provider.complete_turn([{"role": "user", "content": "hi"}])
        self.assertEqual(turn.prompt_tokens, 28123)
        self.assertEqual(turn.completion_tokens, 612)
        self.assertEqual(turn.reasoning_tokens, 500)

    def test_complete_turn_sends_and_parses_native_tool_calls(self):
        provider = agent.ChatProvider(
            base_url="http://example.invalid/v1",
            model="test-model",
            api_key="",
            timeout=1.0,
            temperature=0.1,
            max_tokens=8192,
        )
        body = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "brief",
                    "tool_calls": [state_tool("add_verified", text="fact", basis="OP0001")],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return io.BytesIO(json.dumps(body).encode("utf-8"))

        with mock.patch.object(agent.urllib.request, "urlopen", side_effect=fake_urlopen):
            turn = provider.complete_turn(
                [{"role": "user", "content": "state this"}],
                tools=agent.STATE_MANAGER_TOOLS,
                tool_choice="required",
                thinking_budget_tokens=8192,
            )

        self.assertEqual(captured["payload"]["tools"], agent.STATE_MANAGER_TOOLS)
        self.assertEqual(captured["payload"]["tool_choice"], "required")
        self.assertNotIn("response_format", captured["payload"])
        self.assertEqual(turn.finish_reason, "tool_calls")
        self.assertEqual(turn.tool_calls[0]["function"]["name"], "add_verified")

    def test_context_pressure_triggers_at_seventy_percent(self):
        below = agent.ProviderTurn("{}", "", "stop", prompt_tokens=27999)
        at = agent.ProviderTurn("{}", "", "stop", prompt_tokens=28000)
        self.assertIsNone(agent._worker_context_pressure_reason(
            below, context_budget=40000, context_ratio=0.70
        ))
        reason = agent._worker_context_pressure_reason(
            at, context_budget=40000, context_ratio=0.70
        )
        self.assertIsNotNone(reason)
        self.assertIn("28000/40000", reason)

    def test_context_pressure_message_count_is_only_usage_fallback(self):
        with_usage = agent.ProviderTurn("{}", "", "stop", prompt_tokens=1000)
        without_usage = agent.ProviderTurn("{}", "", "stop")
        self.assertIsNone(agent._worker_context_pressure_reason(
            with_usage, context_budget=40000, context_ratio=0.70,
            recent_messages=100, fallback_message_limit=10,
        ))
        self.assertIsNotNone(agent._worker_context_pressure_reason(
            without_usage, context_budget=40000, context_ratio=0.70,
            recent_messages=10, fallback_message_limit=10,
        ))

    def test_reasoning_attractor_triggers_at_8192_or_length(self):
        at_budget = agent.ProviderTurn(
            "", "loop", "stop", reasoning_tokens=8192
        )
        by_length = agent.ProviderTurn(
            "", "loop", "length", reasoning_tokens=3000
        )
        normal = agent.ProviderTurn(
            "", "thinking", "stop", reasoning_tokens=7000
        )
        self.assertIsNotNone(agent._worker_reasoning_attractor_reason(at_budget, threshold=8192))
        self.assertIsNotNone(agent._worker_reasoning_attractor_reason(by_length, threshold=8192))
        self.assertIsNone(agent._worker_reasoning_attractor_reason(normal, threshold=8192))

    def test_default_fixed_iteration_cadence_is_disabled(self):
        parser = agent.build_parser()
        args = parser.parse_args(["task"])
        self.assertEqual(args.state_cycle_after, 0)
        self.assertIsNone(args.worker_context_budget)
        self.assertEqual(args.state_cycle_context_ratio, 0.70)
        self.assertEqual(args.raw_reasoning_limit, 20000)



class StateManagerTests(unittest.TestCase):
    class FakeProvider:
        def __init__(self, response=None, error=None):
            self.calls = []
            self.response = response
            self.error = error

        def stateless_completion(
            self, messages, *, response_format=None, thinking_budget_tokens=None,
            reasoning_effort=None, tools=None, tool_choice=None,
        ):
            self.calls.append((messages, response_format, thinking_budget_tokens, reasoning_effort, tools, tool_choice))
            if self.error:
                raise self.error
            return agent.ProviderTurn(
                content="", reasoning_content="", finish_reason="tool_calls",
                tool_calls=patch_to_state_tools(self.response),
            )

    @staticmethod
    def empty_patch():
        return {
            "focus_update": {"set": False, "value": ""},
            "verified_add": [],
            "hypotheses_add": [],
            "hypotheses_update": [],
            "unresolved_add": [],
            "unresolved_resolve": [],
            "inspected_add": [],
            "changes_add": [],
            "failures_add": [],
            "loop_signals_add": [],
        }

    def test_manager_uses_native_state_tools(self):
        provider = self.FakeProvider(self.empty_patch())
        state = agent.StateStore._new_state("task")
        patch = agent.propose_state_patch(
            provider,
            task="task",
            state=state,
            transcript=[{"role": "user", "content": "SHELL RESULT: ok"}],
            pending_reasoning=[],
        )
        self.assertEqual(patch["verified_add"], [])
        self.assertIsNone(provider.calls[0][1])
        self.assertEqual(provider.calls[0][2], agent.DEFAULT_STATE_REASONING_BUDGET)
        self.assertEqual(provider.calls[0][3], "medium")
        self.assertEqual(provider.calls[0][4], agent.STATE_MANAGER_TOOLS)
        self.assertEqual(provider.calls[0][5], "required")
        prompt = provider.calls[0][0][1]["content"]
        self.assertIn("CURRENT SEMANTIC STATE VIEW", prompt)
        self.assertIn("NEW EXECUTION TRANSCRIPT", prompt)

    def test_core_translates_semantic_state_tools_to_private_patch(self):
        patch = agent._state_patch_from_tool_calls([
            state_tool("set_focus", value="inspect provider"),
            state_tool("add_verified", text="270 tests pass", basis="OP0012"),
            state_tool(
                "add_hypothesis", text="edge case", status="uncertain",
                basis="worker reasoning", source="reasoning",
            ),
        ])
        self.assertEqual(patch["focus_update"], {"set": True, "value": "inspect provider"})
        self.assertEqual(patch["verified_add"][0]["source"], "transcript")
        self.assertEqual(patch["hypotheses_add"][0]["status"], "uncertain")

    def test_no_state_change_cannot_mix_with_mutations(self):
        with self.assertRaises(agent.AgentError):
            agent._state_patch_from_tool_calls([
                state_tool("no_state_change"),
                state_tool("add_verified", text="fact", basis="OP0001"),
            ])

    def test_unknown_state_tool_is_rejected(self):
        with self.assertRaises(agent.AgentError):
            agent._state_patch_from_tool_calls([state_tool("invent_state_field", value="x")])

    def test_manager_prompt_omits_operation_backlog(self):
        provider = self.FakeProvider(self.empty_patch())
        state = agent.StateStore._new_state("task")
        state["operations"] = [{
            "id": "OP0001",
            "command": "cat huge-file",
            "result_excerpt": "X" * 5000,
        }]
        agent.propose_state_patch(
            provider,
            task="task",
            state=state,
            transcript=[{"role": "user", "content": "SHELL RESULT (OP0002): ok"}],
            pending_reasoning=[],
        )
        prompt = provider.calls[0][0][1]["content"]
        self.assertNotIn("cat huge-file", prompt)
        self.assertNotIn("X" * 100, prompt)
        self.assertIn("OP0002", prompt)

    def test_manager_failure_keeps_state_and_pending_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            pending = store.add_pending_reasoning("important dead reasoning", "length")
            before = store.load()
            provider = self.FakeProvider(error=agent.AgentError("empty content"))
            ok = agent.assimilate_state(
                provider,
                store,
                task="task",
                transcript=[],
                verbose=False,
                pending_reasoning=[pending],
            )
            self.assertFalse(ok)
            self.assertEqual(before, store.load())
            self.assertEqual(store.load_pending_reasoning()[0]["reasoning"], "important dead reasoning")
            self.assertTrue(store.failed_assimilation_path.exists())

    def test_successful_assimilation_clears_pending_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            pending = store.add_pending_reasoning("loop", "length")
            patch = self.empty_patch()
            patch["loop_signals_add"] = [{"text": "revisited X", "basis": "reasoning repeated X", "source": "reasoning"}]
            provider = self.FakeProvider(patch)
            ok = agent.assimilate_state(
                provider,
                store,
                task="task",
                transcript=[],
                verbose=False,
                pending_reasoning=[pending],
            )
            self.assertTrue(ok)
            self.assertEqual(store.load_pending_reasoning(), [])
            self.assertEqual(store.load()["loop_signals"][0]["text"], "revisited X")

    def test_old_pending_reasoning_is_not_auto_replayed_into_later_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            store.add_pending_reasoning("old attractor reasoning", "length")
            provider = self.FakeProvider(self.empty_patch())
            ok = agent.assimilate_state(
                provider,
                store,
                task="task",
                transcript=[{"role": "user", "content": "SHELL RESULT (OP0002): ok"}],
                verbose=False,
            )
            self.assertTrue(ok)
            prompt = provider.calls[0][0][1]["content"]
            self.assertNotIn("old attractor reasoning", prompt)
            self.assertEqual(store.load_pending_reasoning()[0]["reasoning"], "old attractor reasoning")

    def test_worker_state_message_does_not_duplicate_original_task(self):
        state = agent.StateStore._new_state("secret exact task")
        msg = agent._state_message(state)
        self.assertNotIn("secret exact task", msg["content"])
        self.assertIn("persistent", msg["content"].lower())


class StatePathTests(unittest.TestCase):
    def test_default_state_dir_is_sibling(self):
        work = Path("/tmp/job")
        self.assertEqual(agent._default_state_dir(work), Path("/tmp/job.pavlusha-state"))


class GlobalStateCycleTests(unittest.TestCase):
    class SequenceProvider:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = []

        def stateless_completion(
            self, messages, *, response_format=None, thinking_budget_tokens=None,
            reasoning_effort=None, tools=None, tool_choice=None,
        ):
            self.calls.append((messages, response_format, thinking_budget_tokens, reasoning_effort, tools, tool_choice))
            if not self.outcomes:
                raise AssertionError("unexpected provider call")
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return agent.ProviderTurn(
                content="", reasoning_content="", finish_reason="tool_calls",
                tool_calls=patch_to_state_tools(outcome),
            )

    @staticmethod
    def empty_patch():
        return {
            "focus_update": {"set": False, "value": ""},
            "verified_add": [],
            "hypotheses_add": [],
            "hypotheses_update": [],
            "unresolved_add": [],
            "unresolved_resolve": [],
            "inspected_add": [],
            "changes_add": [],
            "failures_add": [],
            "loop_signals_add": [],
        }

    def test_cycle_processes_iterations_one_by_one_and_publishes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            store.enqueue_iteration(
                kind="shell",
                op_id="OP0001",
                transcript=[{"role": "user", "content": "TRANSCRIPT_ONE"}],
                worker_reasoning="first thoughts",
            )
            store.enqueue_iteration(
                kind="shell",
                op_id="OP0002",
                transcript=[{"role": "user", "content": "TRANSCRIPT_TWO"}],
                worker_reasoning="second thoughts",
            )

            p1 = self.empty_patch()
            p1["verified_add"] = [{
                "text": "FACT_ONE",
                "basis": "TRANSCRIPT_ONE",
                "source": "transcript",
            }]
            p2 = self.empty_patch()
            p2["hypotheses_add"] = [{
                "text": "HYP_TWO",
                "status": "uncertain",
                "basis": "second thoughts",
                "source": "reasoning",
            }]
            provider = self.SequenceProvider([p1, p2])

            result = agent.run_state_cycle(
                provider,
                store,
                task="task",
                verbose=False,
                reasoning_budget=7777,
            )

            self.assertEqual(result.processed_iterations, ["IT-OP0001", "IT-OP0002"])
            self.assertIsNone(result.failed_iteration)
            self.assertEqual(store.load_pending_iterations(), [])
            state = store.load()
            self.assertEqual(state["version"], 1, "two per-iteration patches must publish as one global version")
            self.assertEqual(state["verified"][0]["text"], "FACT_ONE")
            self.assertEqual(state["hypotheses"][0]["text"], "HYP_TWO")
            self.assertEqual(len(provider.calls), 2)
            first_prompt = provider.calls[0][0][1]["content"]
            second_prompt = provider.calls[1][0][1]["content"]
            self.assertIn("TRANSCRIPT_ONE", first_prompt)
            self.assertNotIn("TRANSCRIPT_TWO", first_prompt)
            self.assertIn("TRANSCRIPT_TWO", second_prompt)
            second_new = second_prompt.split("NEW EXECUTION TRANSCRIPT:\n", 1)[1].split("\n\nWORKER REASONING", 1)[0]
            self.assertNotIn("TRANSCRIPT_ONE", second_new)
            self.assertIn("FACT_ONE", second_prompt, "second iteration must see provisional state from first")
            self.assertEqual(provider.calls[0][2], 7777)
            self.assertEqual(provider.calls[1][2], 7777)

    def test_failed_iteration_is_quarantined_without_same_cycle_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            for n in range(1, 4):
                store.enqueue_iteration(
                    kind="shell",
                    op_id=f"OP{n:04d}",
                    transcript=[{"role": "user", "content": f"ITERATION_{n}"}],
                )

            p1 = self.empty_patch()
            p1["verified_add"] = [{
                "text": "first settled fact",
                "basis": "ITERATION_1",
                "source": "transcript",
            }]
            provider = self.SequenceProvider([p1, agent.AgentError("reasoning attractor")])

            result = agent.run_state_cycle(provider, store, task="task", verbose=False)

            self.assertEqual(result.processed_iterations, ["IT-OP0001"])
            self.assertEqual(result.failed_iteration, "IT-OP0002")
            self.assertEqual(result.quarantined_iterations, ["IT-OP0002"])
            self.assertEqual([x["id"] for x in store.load_pending_iterations()], ["IT-OP0003"])
            self.assertEqual(len(provider.calls), 2, "failed unit must not be retried in the same cycle")
            self.assertEqual(store.load()["verified"][0]["text"], "first settled fact")
            failed = [json.loads(line) for line in store.failed_assimilation_path.read_text().splitlines()]
            self.assertEqual(failed[-1]["iteration_id"], "IT-OP0002")
            self.assertIn("reasoning attractor", failed[-1]["error"])

    def test_core_semantic_rejection_is_corrected_on_second_manager_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            store.enqueue_iteration(
                kind="shell", op_id="OP0001",
                transcript=[{"role": "user", "content": "RESULT_1"}],
            )

            bad = self.empty_patch()
            bad["unresolved_resolve"] = [{
                "id": "F0003",
                "resolution": "wrong namespace",
                "basis": "RESULT_1",
            }]
            good = self.empty_patch()
            good["verified_add"] = [{
                "text": "corrected fact",
                "basis": "RESULT_1",
                "source": "transcript",
            }]
            provider = self.SequenceProvider([bad, good])

            result = agent.run_state_cycle(provider, store, task="task", verbose=False)

            self.assertEqual(result.processed_iterations, ["IT-OP0001"])
            self.assertIsNone(result.failed_iteration)
            self.assertEqual(result.quarantined_iterations, [])
            self.assertEqual(len(provider.calls), 2)
            retry_prompt = provider.calls[1][0][1]["content"]
            self.assertIn("CORE VALIDATION REJECTED", retry_prompt)
            self.assertIn("state patch references unknown unresolved item F0003", retry_prompt)
            self.assertEqual(store.load()["verified"][0]["text"], "corrected fact")
            self.assertFalse(store.failed_assimilation_path.exists())

    def test_core_semantic_rejection_quarantines_after_three_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            store.enqueue_iteration(
                kind="shell", op_id="OP0001",
                transcript=[{"role": "user", "content": "RESULT_1"}],
            )
            bad = self.empty_patch()
            bad["unresolved_resolve"] = [{
                "id": "F0003",
                "resolution": "still wrong",
                "basis": "RESULT_1",
            }]
            provider = self.SequenceProvider([bad, bad, bad])

            result = agent.run_state_cycle(provider, store, task="task", verbose=False)

            self.assertEqual(len(provider.calls), 3)
            self.assertEqual(result.processed_iterations, [])
            self.assertEqual(result.failed_iteration, "IT-OP0001")
            self.assertEqual(result.quarantined_iterations, ["IT-OP0001"])
            failed = [json.loads(line) for line in store.failed_assimilation_path.read_text().splitlines()]
            self.assertEqual(len(failed), 1)
            self.assertIn("state patch references unknown unresolved item F0003", failed[0]["error"])
            log = [json.loads(line) for line in store.log_path.read_text().splitlines()]
            rejected = [item for item in log if item.get("kind") == "state_iteration_core_rejected"]
            self.assertEqual([item["attempt"] for item in rejected], [1, 2, 3])

    def test_cycle_compacts_processed_operations_out_of_worker_prompt_but_not_disk_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            for n in range(1, 3):
                op = store.record_operation({
                    "command": f"echo {n}",
                    "network": False,
                    "exit_code": 0,
                    "timed_out": False,
                    "duration": 0.01,
                    "stdout": str(n),
                    "stderr": "",
                })
                store.enqueue_iteration(
                    kind="shell",
                    op_id=op["id"],
                    transcript=[{"role": "user", "content": f"RESULT_{n}"}],
                )

            provider = self.SequenceProvider([self.empty_patch(), self.empty_patch()])
            result = agent.run_state_cycle(provider, store, task="task", verbose=False)
            self.assertEqual(result.processed_iterations, ["IT-OP0001", "IT-OP0002"])
            state = store.load()
            self.assertEqual(len(state["operations"]), 2, "append-only factual ledger must stay intact")
            self.assertEqual(state["metadata"]["worker_ops_compacted_through"], 2)
            self.assertEqual(agent._state_for_worker(state)["operations"], [])

            store.record_operation({
                "command": "echo 3",
                "network": False,
                "exit_code": 0,
                "timed_out": False,
                "duration": 0.01,
                "stdout": "3",
                "stderr": "",
            })
            worker_ops = agent._state_for_worker(store.load())["operations"]
            self.assertEqual([op["id"] for op in worker_ops], ["OP0003"])

    def test_zero_iteration_threshold_does_not_run_cycle_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            store.enqueue_iteration(
                kind="shell", op_id="OP0001",
                transcript=[{"role": "user", "content": "RESULT"}],
            )
            provider = self.SequenceProvider([self.empty_patch()])
            result = agent._maybe_run_state_cycle(
                provider, store, task="task", threshold=0, force=False, verbose=False
            )
            self.assertIsNone(result)
            self.assertEqual(len(provider.calls), 0)
            self.assertEqual(len(store.load_pending_iterations()), 1)

    def test_worker_reasoning_is_one_iteration_context_not_verified_evidence(self):
        provider = self.SequenceProvider([self.empty_patch()])
        state = agent.StateStore._new_state("task")
        agent.propose_state_patch(
            provider,
            task="task",
            state=state,
            transcript=[{"role": "user", "content": "SHELL RESULT: ok"}],
            pending_reasoning=[],
            worker_reasoning="maybe provider.py is the defect",
        )
        prompt = provider.calls[0][0][1]["content"]
        self.assertIn("WORKER REASONING FROM THIS ITERATION", prompt)
        self.assertIn("NOT verified evidence", prompt)
        self.assertIn("maybe provider.py is the defect", prompt)

    def test_working_thoughts_message_is_explicitly_ephemeral(self):
        msg = agent._working_thoughts_message("keep looking at recovery.py")
        self.assertEqual(msg["role"], "user")
        self.assertIn("EPHEMERAL WORKING THOUGHTS", msg["content"])
        self.assertIn("not persistent truth", msg["content"])
        self.assertIn("keep looking at recovery.py", msg["content"])


if __name__ == "__main__":
    unittest.main()

class LiveModeTests(unittest.TestCase):
    def test_parser_exposes_live_without_changing_default(self):
        args = agent.build_parser().parse_args(["task"])
        self.assertFalse(args.live)
        args = agent.build_parser().parse_args(["--live", "task"])
        self.assertTrue(args.live)

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

        provider = agent.ChatProvider("http://example/v1", "model", "", 10, 0.0, 100)
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
