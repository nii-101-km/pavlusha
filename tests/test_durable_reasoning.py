"""Durable Project State replaces durable reasoning memory."""
import tempfile
import unittest
from pathlib import Path

from pavlusha_agent.core import AgentError
from pavlusha_agent.config import build_worker_system_prompt
from pavlusha_agent.project_state import (
    apply_project_changes,
    empty_project_state,
    initialize_project_state,
    normalize_evidence,
    validate_project_action,
)
from pavlusha_agent.state_store import StateStore


class DurableProjectStateTests(unittest.TestCase):
    def init_data(self):
        return {
            "design": [{"decision": "Keep provider boundary separate", "rationale": "Small interface"}],
            "work": [
                {"objective": "Implement worker loop", "status": "ACTIVE", "deliverables": ["agent.py"]},
                {"objective": "Write docs", "status": "PLANNED", "deliverables": ["README.md"]},
            ],
        }

    def test_project_state_is_durable_and_reasoning_memory_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            store = StateStore(path, "task")
            project = store.initialize_project(self.init_data(), step=1)
            self.assertEqual(project["recovery"]["active"], "W001")
            reopened = StateStore(path, "task")
            state = reopened.load()
            self.assertEqual(state["project_state"]["design"][0]["id"], "D001")
            self.assertEqual(state["project_state"]["work"][1]["id"], "W002")
            self.assertNotIn("reasoning_memory", state)

    def test_done_accepts_worker_selected_evidence_without_semantic_validation(self):
        project = initialize_project_state(empty_project_state(), self.init_data(), operation_count=0)
        project = apply_project_changes(project, [
            {"op": "update_work", "id": "W001", "status": "DONE",
             "evidence": ["FILE: agent.py", "SHELL RESULT: python3 -m unittest -> OK"]},
            {"op": "update_work", "id": "W002", "status": "ACTIVE"},
        ])
        self.assertEqual(project["work"][0]["evidence"],
                         ["FILE: agent.py", "SHELL RESULT: python3 -m unittest -> OK"])
        self.assertEqual(project["recovery"]["active"], "W002")
        # Core does not require or judge evidence sufficiency.
        project = apply_project_changes(project, [
            {"op": "update_work", "id": "W002", "status": "DONE"},
        ])
        self.assertIsNone(project["recovery"]["active"])

    def test_evidence_is_only_structurally_normalized(self):
        self.assertEqual(normalize_evidence([" FILE: x.py ", "SHELL RESULT: tests -> OK"]),
                         ["FILE: x.py", "SHELL RESULT: tests -> OK"])
        with self.assertRaisesRegex(AgentError, "evidence must be a list"):
            normalize_evidence("FILE: x.py")
        with self.assertRaisesRegex(AgentError, "must not contain duplicates"):
            normalize_evidence(["FILE: x.py", "FILE: x.py"])

    def test_blocked_requires_reason_but_not_evidence(self):
        project = initialize_project_state(empty_project_state(), self.init_data(), operation_count=0)
        with self.assertRaisesRegex(AgentError, "BLOCKED requires reason"):
            apply_project_changes(project, [
                {"op": "update_work", "id": "W001", "status": "BLOCKED"},
                {"op": "update_work", "id": "W002", "status": "ACTIVE"},
            ])
        project = apply_project_changes(project, [
            {"op": "update_work", "id": "W001", "status": "BLOCKED", "reason": "namespace denied"},
            {"op": "update_work", "id": "W002", "status": "ACTIVE"},
        ])
        self.assertEqual(project["work"][0]["status"], "BLOCKED")

    def test_superseded_requires_explicit_deviation(self):
        project = initialize_project_state(empty_project_state(), self.init_data(), operation_count=0)
        with self.assertRaisesRegex(AgentError, "deviation"):
            apply_project_changes(project, [
                {"op": "update_work", "id": "W001", "status": "SUPERSEDED"},
                {"op": "update_work", "id": "W002", "status": "ACTIVE"},
            ])
        project = apply_project_changes(project, [
            {"op": "record_deviation", "affects": ["W001"], "original": "X", "actual": "Y",
             "reason": "current environment"},
            {"op": "update_work", "id": "W001", "status": "SUPERSEDED", "deviation": "V001"},
            {"op": "update_work", "id": "W002", "status": "ACTIVE"},
        ])
        self.assertEqual(project["work"][0]["deviation"], "V001")


    def test_worker_prompt_defines_material_evidence_without_core_judgment(self):
        prompt = build_worker_system_prompt()
        self.assertIn("FILES", prompt)
        self.assertIn("SHELL OPERATIONS / RESULTS", prompt)
        self.assertIn("does not judge whether it is sufficient", prompt)
        self.assertNotIn("role=verification", prompt)
        self.assertNotIn("Grounding kinds", prompt)

    def test_project_update_schema_uses_evidence_not_grounding(self):
        kind, data = validate_project_action({
            "action": "project_update",
            "changes": [{"op": "update_work", "id": "W001", "status": "DONE",
                         "evidence": ["FILE: agent.py"]}],
        })
        self.assertEqual(kind, "project_update")
        self.assertEqual(data["changes"][0]["evidence"], ["FILE: agent.py"])
        with self.assertRaisesRegex(AgentError, "must change"):
            validate_project_action({
                "action": "project_update",
                "changes": [{"op": "update_work", "id": "W001", "grounding": []}],
            })


if __name__ == "__main__":
    unittest.main()
