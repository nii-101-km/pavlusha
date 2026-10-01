"""Reasoning belongs to step history; Project State is the durable recovery checkpoint."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError, ProviderTurn, ShellResult
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore


def turn(action, *, reasoning="THOUGHT", tokens=1):
    return ProviderTurn(content=json.dumps(action), reasoning_content=reasoning,
                        finish_reason="stop", reasoning_tokens=tokens, prompt_tokens=200)


def init_turn(*, reasoning="INITIAL_PLAN", tokens=1):
    return turn({"action": "project_init", "design": [], "work": [
        {"objective": "Do work", "status": "ACTIVE", "deliverables": []},
        {"objective": "Verify work", "status": "PLANNED", "deliverables": []}
    ]}, reasoning=reasoning, tokens=tokens)


def material_evidence(command):
    return [f"SHELL RESULT: {command} -> exit 0"]


class ChronologicalReasoningProjectCheckpointTests(unittest.TestCase):
    def run_case(self, turns, *, extra=()):
        turns = iter(turns)
        workers = []

        def worker(provider, messages, **kwargs):
            workers.append(copy.deepcopy(messages))
            return next(turns)

        def no_manager(*args, **kwargs):
            raise AssertionError("State Manager/reasoning compactor must not be called")

        def shell(workdir, command, **kwargs):
            return ShellResult(command=command, network=False, exit_code=0, timed_out=False,
                               duration=0.01, stdout="OK " + command, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            argv = ["--workdir", tmp + "/work", "--state-dir", str(state_dir), "--model", "model",
                    "--worker-context-budget", "40000", "--max-steps", "30",
                    "--project-review-every", "0", *extra, "task"]
            error = None
            with patch("pavlusha_agent.runtime.shutil.which", return_value="/fake/bwrap"), \
                 patch("pavlusha_agent.provider.ChatProvider.worker_completion", worker), \
                 patch("pavlusha_agent.provider.ChatProvider.stateless_completion", no_manager), \
                 patch("pavlusha_agent.runtime.run_shell", side_effect=shell), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    code = run_agent(build_parser().parse_args(argv))
                except (AgentError, StopIteration) as exc:
                    code, error = None, str(exc)
            store = StateStore(state_dir, "task")
            state = store.load()
            logs = [json.loads(x) for x in store.log_path.read_text().splitlines()]
            archive = []
            if store.reasoning_archive_path.exists():
                archive = [json.loads(x) for x in store.reasoning_archive_path.read_text().splitlines()]
        return {"code": code, "error": error, "workers": workers, "state": state, "logs": logs, "archive": archive}

    def test_initial_plan_gate_rejects_single_item_permission_slip(self):
        turns = [
            turn({"action": "project_init", "design": [], "work": [
                {"objective": "Do everything", "status": "ACTIVE", "deliverables": []}
            ]}, reasoning="SHALLOW_PLAN"),
            init_turn(reasoning="REAL_PLAN"),
            turn({"action": "shell", "command": "echo verified"}),
            turn({"action": "project_update", "changes": [
                {"op": "update_work", "id": "W001", "status": "DONE",
                 "evidence": material_evidence("echo verified")},
                {"op": "update_work", "id": "W002", "status": "DONE",
                 "evidence": material_evidence("echo verified")},
            ]}),
            turn({"action": "finish", "summary": "done"}),
        ]
        result = self.run_case(turns, extra=["--raw-reasoning-limit", "999"])
        self.assertEqual(result["code"], 0)
        self.assertIn("at least two concrete plan items", str(result["workers"][1]))
        self.assertEqual(len(result["state"]["project_state"]["work"]), 2)
        self.assertEqual([op["command"] for op in result["state"]["operations"]], ["echo verified"])

    def test_project_evidence_is_not_checked_against_operation_ledger(self):
        turns = [
            init_turn(),
            turn({"action": "shell", "command": "echo verified"}),
            turn({"action": "project_update", "changes": [
                {"op": "update_work", "id": "W001", "status": "DONE",
                 "evidence": ["SHELL RESULT: a differently written command -> OK"]},
                {"op": "update_work", "id": "W002", "status": "DONE",
                 "evidence": ["FILE: whatever-the-worker-found.txt"]},
            ]}),
            turn({"action": "finish", "summary": "done"}),
        ]
        result = self.run_case(turns, extra=["--raw-reasoning-limit", "999"])
        self.assertEqual(result["code"], 0)
        self.assertNotIn("PROJECT UPDATE REJECTED", str(result["workers"][3]))
        self.assertEqual(result["state"]["project_state"]["work"][0]["evidence"],
                         ["SHELL RESULT: a differently written command -> OK"])

    def test_short_reasoning_remains_verbatim_and_no_compactor_exists(self):
        turns = [
            init_turn(reasoning="PLAN_A", tokens=2),
            turn({"action": "shell", "command": "echo one"}, reasoning="PLAN_B", tokens=2),
            turn({"action": "project_update", "changes": [{"op": "update_work", "id": "W001", "status": "DONE",
                  "evidence": material_evidence("echo one")}, {"op": "update_work", "id": "W002", "status": "DONE",
                  "evidence": material_evidence("echo one")}]}, reasoning="PLAN_C", tokens=2),
            turn({"action": "finish", "summary": "done"}, reasoning="PLAN_D", tokens=2),
        ]
        result = self.run_case(turns, extra=["--raw-reasoning-limit", "100"])
        self.assertEqual(result["code"], 0)
        self.assertIn("PLAN_A", str(result["workers"][1]))
        self.assertIn("PLAN_B", str(result["workers"][2]))
        self.assertNotIn("reasoning_memory", result["state"])
        self.assertFalse(result["archive"])
        self.assertFalse(any(e["kind"].startswith("state_cycle") for e in result["logs"]))

    def test_legacy_raw_limit_does_not_split_retention_or_force_review(self):
        turns = [
            init_turn(reasoning="R_INIT", tokens=100),
            turn({"action": "shell", "command": "echo one"}, reasoning="R_ONE", tokens=100),
            turn({"action": "shell", "command": "echo two"}, reasoning="R_TWO", tokens=100),
            turn({"action": "project_update", "changes": [
                {"op": "update_work", "id": name, "status": "DONE",
                 "evidence": material_evidence("echo two")} for name in ("W001", "W002")
            ]}),
            turn({"action": "finish", "summary": "done"}),
        ]
        result = self.run_case(turns, extra=["--raw-reasoning-limit", "1"])
        self.assertEqual(result["code"], 0)
        self.assertEqual([op["command"] for op in result["state"]["operations"]], ["echo one", "echo two"])
        self.assertFalse(result["archive"])
        self.assertFalse(any(e["kind"] == "project_review_completed" for e in result["logs"]))
        prompt = result["workers"][3]
        content = [m["content"] for m in prompt]
        pos = next(i for i, text in enumerate(content) if text.endswith("R_ONE"))
        self.assertEqual(json.loads(content[pos + 1])["command"], "echo one")
        self.assertTrue(content[pos + 2].startswith("SHELL RESULT"))
        self.assertTrue(content[pos + 3].endswith("R_TWO"))
        self.assertFalse(any("RECENT RAW REASONING" in text for text in content))

    def test_periodic_review_blocks_next_normal_action_until_acknowledged(self):
        turns = [
            init_turn(),
            turn({"action": "shell", "command": "echo one"}),
            turn({"action": "shell", "command": "echo two"}),
            turn({"action": "shell", "command": "echo blocked"}),
            turn({"action": "project_review_skip"}),
            turn({"action": "shell", "command": "echo three"}),
            turn({"action": "project_update", "changes": [{"op": "update_work", "id": "W001", "status": "DONE",
                  "evidence": material_evidence("echo three")}, {"op": "update_work", "id": "W002", "status": "DONE",
                  "evidence": material_evidence("echo three")}]}, tokens=1),
            turn({"action": "finish", "summary": "done"}),
        ]
        result = self.run_case(turns, extra=["--raw-reasoning-limit", "999", "--project-review-every", "2"])
        self.assertEqual(result["code"], 0)
        self.assertEqual([op["command"] for op in result["state"]["operations"]], ["echo one", "echo two", "echo three"])
        self.assertIn("PERIODIC PROJECT STATE REVIEW", str(result["workers"][3]))
        self.assertTrue(any(e["kind"] == "periodic_review_acknowledged" for e in result["logs"]))
        self.assertFalse(any(e["kind"] == "project_review_completed" for e in result["logs"]))

    def test_finish_is_rejected_until_project_work_is_done(self):
        turns = [
            init_turn(),
            turn({"action": "finish", "summary": "premature"}),
            turn({"action": "shell", "command": "echo verified"}),
            turn({"action": "project_update", "changes": [{"op": "update_work", "id": "W001", "status": "DONE",
                  "evidence": material_evidence("echo verified")}, {"op": "update_work", "id": "W002", "status": "DONE",
                  "evidence": material_evidence("echo verified")}]}, tokens=1),
            turn({"action": "finish", "summary": "done"}),
        ]
        result = self.run_case(turns, extra=["--raw-reasoning-limit", "999"])
        self.assertEqual(result["code"], 0)
        self.assertIn("FINISH REJECTED", str(result["workers"][2]))


if __name__ == "__main__":
    unittest.main()
