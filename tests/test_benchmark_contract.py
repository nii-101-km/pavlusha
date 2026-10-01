from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import agent
import bench


class ContextPressureTokenLimitTests(unittest.TestCase):
    def test_explicit_token_limit_overrides_ratio_and_triggers_at_boundary(self):
        below = agent.ProviderTurn("{}", "", "stop", prompt_tokens=11999)
        at = agent.ProviderTurn("{}", "", "stop", prompt_tokens=12000)
        self.assertIsNone(agent._worker_context_pressure_reason(
            below, context_budget=130000, context_ratio=0.70, token_limit=12000
        ))
        reason = agent._worker_context_pressure_reason(
            at, context_budget=130000, context_ratio=0.70, token_limit=12000
        )
        self.assertIsNotNone(reason)
        self.assertIn("token_limit=12000", reason)

    def test_threshold_helper_prefers_explicit_limit(self):
        self.assertEqual(agent._worker_context_threshold(
            context_budget=130000, context_ratio=0.70, token_limit=12000
        ), 12000)
        self.assertEqual(agent._worker_context_threshold(
            context_budget=40000, context_ratio=0.70, token_limit=None
        ), 28000)

    def test_parser_exposes_meter_without_changing_default_behavior(self):
        args = agent.build_parser().parse_args(["task"])
        self.assertEqual(args.context_meter, "hidden")
        self.assertIsNone(args.state_cycle_token_limit)

    def test_visible_meter_is_neutral_telemetry(self):
        msg = agent.context_meter_message(
            previous_prompt_tokens=6000, context_budget=130000, compaction_threshold=12000
        )["content"]
        self.assertIn("6000 prompt tokens", msg)
        self.assertIn("12000 tokens", msg)
        self.assertNotIn("do not", msg.lower())
        self.assertNotIn("avoid", msg.lower())
        self.assertNotIn("conserve", msg.lower())


class BenchmarkFixtureTests(unittest.TestCase):
    def test_fixture_copies_bench_and_project_map_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "work"
            bench._copy_project(dst)
            self.assertTrue((dst / "bench.py").is_file())
            self.assertTrue((dst / "requirements.txt").is_file())
            self.assertTrue((dst / "pavlusha_agent/project_map.py").is_file())


class ReplacementScoringTests(unittest.TestCase):
    def test_score_case_counts_tombstones_and_compactions_without_losing_legacy_drop_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "maintenance_gate"
            state_dir = case / "state"
            work = case / "work"
            state_dir.mkdir(parents=True)
            work.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                json.dumps({"run": {"status": "finished"}}), encoding="utf-8"
            )
            events = [
                {
                    "kind": "worker_action", "action": "drop_context",
                    "items": [{"id": "W0001", "reason": "navigation complete"}],
                },
                {
                    "kind": "context_replacement_applied", "mode": "tombstone",
                    "ids": ["W0001"], "approx_tokens_before": 1000,
                    "approx_tokens_after": 20, "approx_tokens_freed": 980,
                },
                {
                    "kind": "worker_action", "action": "compact_context",
                    "items": [{"id": "W0002", "summary": "timeout invariant established"}],
                },
                {
                    "kind": "context_replacement_applied", "mode": "compacted",
                    "ids": ["W0002"], "approx_tokens_before": 800,
                    "approx_tokens_after": 40, "approx_tokens_freed": 760,
                },
            ]
            (state_dir / "experiment.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )

            score = bench.score_case(case)
            self.assertEqual(score["context_drop_actions"], 1)
            self.assertEqual(score["context_compact_actions"], 1)
            self.assertEqual(score["context_replacements_applied"], 2)
            self.assertEqual(score["tombstone_replacements_applied"], 1)
            self.assertEqual(score["compact_replacements_applied"], 1)
            self.assertEqual(score["replaced_context_items"], 2)
            self.assertEqual(score["replacement_tokens_before"], 1800)
            self.assertEqual(score["replacement_tokens_after"], 60)
            self.assertEqual(score["replacement_tokens_freed"], 1740)
            # Legacy drop fields continue to represent the tombstone side only.
            self.assertEqual(score["context_drop_applied"], 1)
            self.assertEqual(score["dropped_context_items"], 1)
            self.assertEqual(score["dropped_approx_tokens"], 980)
            self.assertEqual(score["drop_reasons"], ["navigation complete"])
            self.assertEqual(score["compact_summaries"], ["timeout invariant established"])


if __name__ == "__main__":
    unittest.main()
