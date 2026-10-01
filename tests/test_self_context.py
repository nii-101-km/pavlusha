from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import agent


class WorkingContextTests(unittest.TestCase):
    def test_only_explicitly_removable_messages_receive_handles(self):
        ctx = agent.WorkingContext()
        self.assertIsNone(ctx.append({"role": "assistant", "content": "small action"}))
        handle = ctx.append(
            {"role": "user", "content": "x" * 4000},
            removable=True,
            kind="shell_result",
            op_id="OP0001",
            step=1,
            label="cat big.py",
        )
        self.assertEqual(handle, "W0001")
        items = ctx.removable_items()
        self.assertEqual([item.handle for item in items], ["W0001"])
        self.assertEqual(items[0].op_id, "OP0001")
        self.assertGreaterEqual(items[0].approx_tokens, 1000)

    def test_tiny_raw_message_gets_no_cleanup_capability(self):
        ctx = agent.WorkingContext()
        handle = ctx.append({"role": "user", "content": "ok"}, removable=True)
        self.assertIsNone(handle)
        self.assertEqual(ctx.removable_items(), [])
        stats = ctx.telemetry()
        self.assertEqual(stats["removable_approx_tokens"], 0)
        self.assertGreater(stats["protected_approx_tokens"], 0)

    def test_drop_removes_only_selected_raw_message(self):
        # Legacy destructive helper remains available for compatibility; runtime drop_context now
        # uses in-place tombstones instead.
        ctx = agent.WorkingContext()
        action = {"role": "assistant", "content": '{"action":"shell"}'}
        result = {"role": "user", "content": "large result" * 500}
        ctx.append(action)
        handle = ctx.append(result, removable=True, kind="shell_result", op_id="OP0001")
        dropped = ctx.drop([handle])
        self.assertEqual([item.handle for item in dropped], [handle])
        self.assertEqual(ctx.messages(), [action])
        with self.assertRaises(agent.AgentError):
            ctx.drop([handle])

    def test_tombstone_replaces_raw_message_in_same_chronological_slot(self):
        ctx = agent.WorkingContext()
        ctx.append({"role": "assistant", "content": "before"})
        raw = {"role": "user", "content": "VERY_RAW_PAYLOAD " * 400}
        handle = ctx.append(raw, removable=True, kind="shell_result", op_id="OP0007", step=7)
        ctx.append({"role": "assistant", "content": "after"})

        before_len = len(ctx.messages())
        replacements = ctx.tombstone([handle], "inspection complete")
        messages = ctx.messages()

        self.assertEqual(len(messages), before_len)
        self.assertEqual(messages[0]["content"], "before")
        self.assertEqual(messages[2]["content"], "after")
        self.assertIn("CONTEXT TOMBSTONE W0001 OP0007", messages[1]["content"])
        self.assertIn("inspection complete", messages[1]["content"])
        self.assertNotIn("VERY_RAW_PAYLOAD", messages[1]["content"])
        self.assertEqual(replacements[0].original_message, raw)
        self.assertEqual(ctx.removable_items(), [])
        self.assertEqual(ctx.telemetry()["replacement_items"], 1)
        with self.assertRaises(agent.AgentError):
            ctx.tombstone([handle], "again")

    def test_batch_tombstones_keep_individual_reasons_in_each_original_slot(self):
        ctx = agent.WorkingContext()
        h1 = ctx.append(
            {"role": "user", "content": "first raw " * 300},
            removable=True, kind="shell_result", op_id="OP0001",
        )
        ctx.append({"role": "assistant", "content": "between"})
        h2 = ctx.append(
            {"role": "user", "content": "second raw " * 300},
            removable=True, kind="shell_result", op_id="OP0002",
        )
        planned = ctx.plan_tombstone_items([
            {"id": h1, "reason": "directory navigation superseded"},
            {"id": h2, "reason": "failed probe no longer relevant"},
        ])
        ctx.apply_replacements(planned)
        messages = ctx.messages()
        self.assertIn("directory navigation superseded", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "between")
        self.assertIn("failed probe no longer relevant", messages[2]["content"])
        self.assertNotEqual(planned[0].note, planned[1].note)

    def test_stale_batch_is_rejected_before_any_slot_is_mutated(self):
        ctx = agent.WorkingContext()
        h1 = ctx.append({"role": "user", "content": "first raw " * 300}, removable=True)
        h2 = ctx.append({"role": "user", "content": "second raw " * 300}, removable=True)
        planned = ctx.plan_tombstone_items([
            {"id": h1, "reason": "first obsolete"},
            {"id": h2, "reason": "second obsolete"},
        ])
        before = [dict(message) for message in ctx.messages()]
        # Make only the second plan stale after planning. Atomic apply must not mutate the first.
        ctx.messages()[1]["content"] += " changed"
        with self.assertRaises(agent.AgentError):
            ctx.apply_replacements(planned)
        self.assertEqual(ctx.messages()[0], before[0])
        self.assertNotIn("CONTEXT TOMBSTONE", ctx.messages()[0]["content"])

    def test_compact_replaces_raw_message_in_place_and_must_actually_shrink(self):
        ctx = agent.WorkingContext()
        handle = ctx.append(
            {"role": "user", "content": "large evidence " * 1000},
            removable=True, kind="shell_result", op_id="OP0008", step=8,
        )
        limit = ctx.removable_items()[0].max_summary_chars
        self.assertGreater(limit, 100)
        replacements = ctx.compact([
            {"id": handle, "summary": "Tests require timeout <= max_command_timeout."}
        ])
        self.assertEqual(len(ctx.messages()), 1)
        self.assertIn("CONTEXT COMPACTED W0001 OP0008", ctx.messages()[0]["content"])
        self.assertIn("timeout <= max_command_timeout", ctx.messages()[0]["content"])
        self.assertGreater(replacements[0].approx_tokens_freed, 0)
        self.assertEqual(ctx.removable_items(), [])

        large = agent.WorkingContext()
        large_handle = large.append(
            {"role": "user", "content": "raw evidence " * 2000},
            removable=True, kind="shell_result", op_id="OP0009", step=9,
        )
        long_summary = "s" * 1200
        large_replacement = large.compact([
            {"id": large_handle, "summary": long_summary}
        ])[0]
        self.assertEqual(large_replacement.note, long_summary)
        self.assertGreater(large_replacement.approx_tokens_freed, 0)

        tiny = agent.WorkingContext()
        tiny_handle = tiny.append({"role": "user", "content": "x"}, removable=True)
        with self.assertRaises(agent.AgentError):
            tiny.compact([{"id": tiny_handle, "summary": "still too large"}])

    def test_tail_trim_is_core_owned_and_can_remove_any_old_raw_message(self):
        ctx = agent.WorkingContext()
        for n in range(6):
            ctx.append({"role": "user", "content": str(n)}, removable=(n % 2 == 0))
        ctx.retain_tail_messages(2)
        self.assertEqual([m["content"] for m in ctx.messages()], ["4", "5"])

    def test_inventory_is_metadata_not_raw_result(self):
        ctx = agent.WorkingContext()
        ctx.append(
            {"role": "user", "content": "SECRET_RAW_PAYLOAD" * 100},
            removable=True,
            kind="shell_result",
            op_id="OP0007",
            label="sed -n '1,400p' large.py",
        )
        msg = agent.context_inventory_message(ctx)
        self.assertIsNotNone(msg)
        text = msg["content"]
        self.assertIn("W0001", text)
        self.assertIn("OP0007", text)
        self.assertIn("sed -n", text)
        self.assertNotIn("SECRET_RAW_PAYLOAD", text)


class ContextDropContractTests(unittest.TestCase):
    def test_drop_action_is_disabled_by_default(self):
        with self.assertRaises(agent.AgentError):
            agent.validate_action(
                {"action": "drop_context", "ids": ["W0001"], "intent": "done"},
                120,
            )

    def test_drop_action_accepts_only_current_controller_handles(self):
        kind, data = agent.validate_action(
            {"action": "drop_context", "ids": ["W0002"], "intent": "inspection complete"},
            120,
            allow_context_drop=True,
            available_context_ids={"W0001", "W0002"},
        )
        self.assertEqual(kind, "drop_context")
        self.assertEqual(data["ids"], ["W0002"])
        with self.assertRaises(agent.AgentError):
            agent.validate_action(
                {"action": "drop_context", "ids": ["SYSTEM"], "intent": "remove invariant"},
                120,
                allow_context_drop=True,
                available_context_ids={"W0001", "W0002"},
            )

    def test_drop_action_supports_per_item_reasons(self):
        kind, data = agent.validate_action(
            {
                "action": "drop_context",
                "items": [
                    {"id": "W0001", "reason": "initial listing superseded"},
                    {"id": "W0002", "reason": "failed dependency probe no longer useful"},
                ],
            },
            120,
            allow_context_drop=True,
            available_context_ids={"W0001", "W0002"},
        )
        self.assertEqual(kind, "drop_context")
        self.assertEqual(data["items"][0]["reason"], "initial listing superseded")
        self.assertEqual(data["items"][1]["reason"], "failed dependency probe no longer useful")
        self.assertEqual(data["ids"], ["W0001", "W0002"])

    def test_drop_requires_small_persistent_intent(self):
        with self.assertRaises(agent.AgentError):
            agent.validate_action(
                {"action": "drop_context", "ids": ["W0001"], "intent": ""},
                120,
                allow_context_drop=True,
                available_context_ids={"W0001"},
            )

    def test_compact_action_is_validated_per_handle_and_disabled_by_default(self):
        action = {
            "action": "compact_context",
            "items": [{"id": "W0002", "summary": "retain this fact"}],
        }
        with self.assertRaises(agent.AgentError):
            agent.validate_action(action, 120)
        kind, data = agent.validate_action(
            action,
            120,
            allow_context_drop=True,
            available_context_ids={"W0001", "W0002"},
            available_context_summary_limits={"W0001": 10, "W0002": 100},
        )
        self.assertEqual(kind, "compact_context")
        self.assertEqual(data["items"][0]["id"], "W0002")
        with self.assertRaises(agent.AgentError):
            agent.validate_action(
                {"action": "compact_context", "items": [{"id": "W0002", "summary": "x" * 101}]},
                120,
                allow_context_drop=True,
                available_context_ids={"W0002"},
                available_context_summary_limits={"W0002": 100},
            )
        # There is no arbitrary global summary cap: a summary longer than the old 800-char
        # ceiling is valid when the specific raw item is large enough to shrink.
        kind, data = agent.validate_action(
            {
                "action": "compact_context",
                "items": [
                    {"id": "W0001", "summary": "valid"},
                    {"id": "W0002", "summary": "x" * 801},
                ],
            },
            120,
            allow_context_drop=True,
            available_context_ids={"W0001", "W0002"},
            available_context_summary_limits={"W0001": 100, "W0002": 900},
        )
        self.assertEqual(kind, "compact_context")
        self.assertEqual(len(data["items"][1]["summary"]), 801)
        with self.assertRaisesRegex(
            agent.AgentError,
            r"compact_context summary for W0002 is 101 characters; maximum is 100 for this raw item",
        ):
            agent.validate_action(
                {"action": "compact_context", "items": [{"id": "W0002", "summary": "x" * 101}]},
                120,
                allow_context_drop=True,
                available_context_ids={"W0002"},
                available_context_summary_limits={"W0002": 100},
            )
        # Tombstone reasons also have no arbitrary global cap; the per-item shrink limit is
        # the resource invariant when controller limits are supplied.
        kind, data = agent.validate_action(
            {"action": "drop_context", "ids": ["W0001"], "intent": "x" * 401},
            120,
            allow_context_drop=True,
            available_context_ids={"W0001"},
            available_context_tombstone_limits={"W0001": 500},
        )
        self.assertEqual(kind, "drop_context")
        self.assertEqual(len(data["items"][0]["reason"]), 401)

    def test_worker_prompt_exposes_capability_without_changing_default_contract(self):
        off = agent.build_worker_system_prompt("off")
        drop = agent.build_worker_system_prompt("drop")
        self.assertIn("Available actions", off)
        self.assertNotIn("drop_context", off)
        self.assertEqual(drop, off)
        self.assertNotIn("drop_context", drop)
        self.assertNotIn("compact_context", drop)

    def test_context_drop_intent_persists_while_operation_ledger_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            op = store.record_operation({
                "command": "cat big.py",
                "network": False,
                "exit_code": 0,
                "timed_out": False,
                "duration": 0.01,
                "stdout": "payload",
                "stderr": "",
            })
            record = store.record_context_drop(
                handles=["W0001"],
                intent="inspection complete",
                dropped_items=[{
                    "handle": "W0001",
                    "op_id": op["id"],
                    "approx_tokens": 2500,
                    "chars": 10000,
                }],
            )
            state = store.load()
            self.assertEqual(record["id"], "D0001")
            self.assertEqual(state["context_dispositions"][-1]["intent"], "inspection complete")
            self.assertEqual(state["context_dispositions"][-1]["op_ids"], ["OP0001"])
            self.assertEqual(len(state["operations"]), 1)
            projected = agent._state_for_worker(state)
            self.assertNotIn("context_dispositions", projected)
            log = [json.loads(line) for line in store.log_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(item.get("kind") == "context_dropped" for item in log))

    def test_context_replacement_archives_exact_original_without_prompt_duplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = agent.StateStore(Path(tmp), "task")
            ctx = agent.WorkingContext()
            raw = {"role": "user", "content": "FULL_RAW_SECRET " * 300}
            handle = ctx.append(raw, removable=True, kind="shell_result", op_id="OP0009", step=9)
            planned = ctx.plan_tombstones([handle], "navigation complete")
            payload = [{
                "handle": item.handle,
                "mode": item.mode,
                "kind": item.kind,
                "op_id": item.op_id,
                "step": item.step,
                "label": item.label,
                "note": item.note,
                "original_message": item.original_message,
                "replacement_message": item.replacement_message,
                "original_approx_tokens": item.original_approx_tokens,
                "original_chars": item.original_chars,
                "replacement_approx_tokens": item.replacement_approx_tokens,
                "replacement_chars": item.replacement_chars,
            } for item in planned]
            record = store.record_context_replacement(
                mode="tombstone", replacements=payload, note="navigation complete"
            )
            ctx.apply_replacements(planned)

            self.assertEqual(record["id"], "D0001")
            self.assertGreater(record["approx_tokens_freed"], 0)
            archive = [json.loads(line) for line in store.context_archive_path.read_text().splitlines()]
            self.assertEqual(archive[-1]["items"][0]["original_message"], raw)
            self.assertIn("CONTEXT TOMBSTONE", archive[-1]["items"][0]["replacement_message"]["content"])
            self.assertNotIn("context_dispositions", agent._state_for_worker(store.load()))
            self.assertNotIn("FULL_RAW_SECRET", json.dumps(agent._state_for_worker(store.load())))

    def test_cli_default_keeps_feature_off(self):
        args = agent.build_parser().parse_args(["task"])
        self.assertEqual(args.worker_context_control, "off")


class ProgressiveContextPressureTests(unittest.TestCase):
    @staticmethod
    def _context() -> agent.WorkingContext:
        ctx = agent.WorkingContext()
        ctx.append(
            {"role": "user", "content": "RESULT_A" * 800},
            removable=True,
            kind="shell_result",
            op_id="OP0001",
            step=1,
            label="sed -n '1,200p' a.py",
        )
        ctx.append(
            {"role": "user", "content": "RESULT_B" * 500},
            removable=True,
            kind="shell_result",
            op_id="OP0002",
            step=2,
            label="python3 -m unittest -q",
        )
        return ctx

    def test_pressure_levels_escalate_at_exact_boundaries(self):
        kwargs = dict(managed_budget=6000, soft_ratio=0.50, strong_ratio=0.75, urgent_ratio=0.90)
        self.assertEqual(agent.context_pressure_level(managed_tokens=2999, **kwargs), "silent")
        self.assertEqual(agent.context_pressure_level(managed_tokens=3000, **kwargs), "soft")
        self.assertEqual(agent.context_pressure_level(managed_tokens=4499, **kwargs), "soft")
        self.assertEqual(agent.context_pressure_level(managed_tokens=4500, **kwargs), "strong")
        self.assertEqual(agent.context_pressure_level(managed_tokens=5399, **kwargs), "strong")
        self.assertEqual(agent.context_pressure_level(managed_tokens=5400, **kwargs), "urgent")

    def test_progressive_message_is_one_ephemeral_snapshot_and_never_duplicates_raw_payload(self):
        ctx = self._context()
        msg, level = agent.context_pressure_message(
            ctx,
            managed_budget=3000,
            soft_ratio=0.50,
            strong_ratio=0.75,
            urgent_ratio=0.90,
        )
        self.assertEqual(level, "strong")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["role"], "user")
        text = msg["content"]
        self.assertIn("CURRENT MANAGED CONTEXT STATUS", text)
        self.assertIn("REPLACEABLE RAW CONTEXT HANDLES", text)
        self.assertIn("W0001", text)
        self.assertIn("W0002", text)
        self.assertIn("drop_context", text)
        self.assertIn("compact_context", text)
        self.assertNotIn("RESULT_A", text)
        self.assertNotIn("RESULT_B", text)
        # Building the ephemeral message must not mutate the retained raw transcript.
        self.assertEqual(len(ctx.messages()), 2)

    def test_below_soft_threshold_preserves_neutral_capability_inventory(self):
        ctx = self._context()
        msg, level = agent.context_pressure_message(
            ctx,
            managed_budget=12000,
        )
        self.assertEqual(level, "silent")
        self.assertIsNotNone(msg)
        text = msg["content"]
        self.assertIn("WORKING CONTEXT INVENTORY", text)
        self.assertNotIn("Context pressure is high", text)
        self.assertNotIn("Context is becoming substantial", text)

    def test_urgent_notice_warns_before_core_emergency_threshold(self):
        ctx = self._context()
        msg, level = agent.context_pressure_message(
            ctx,
            managed_budget=2800,
        )
        self.assertEqual(level, "urgent")
        self.assertIn("pressure is very high", msg["content"])
        self.assertIn("drop_context", msg["content"])
        self.assertIn("compact_context", msg["content"])

    def test_parser_defaults_keep_progressive_guidance_off(self):
        args = agent.build_parser().parse_args(["task"])
        self.assertEqual(args.context_pressure_guidance, "off")
        self.assertEqual(args.context_pressure_soft_ratio, 0.50)
        self.assertEqual(args.context_pressure_strong_ratio, 0.75)
        self.assertEqual(args.context_pressure_urgent_ratio, 0.90)
        self.assertIsNone(args.context_maintenance_budget)


class MaintenanceGateTests(unittest.TestCase):
    def test_gate_hysteresis_enters_and_releases_at_exact_boundaries(self):
        kwargs = dict(managed_budget=6000, enter_ratio=0.75, release_ratio=0.55)
        self.assertFalse(agent.context_maintenance_transition(False, managed_tokens=4499, **kwargs))
        self.assertTrue(agent.context_maintenance_transition(False, managed_tokens=4500, **kwargs))
        self.assertTrue(agent.context_maintenance_transition(True, managed_tokens=3301, **kwargs))
        self.assertFalse(agent.context_maintenance_transition(True, managed_tokens=3300, **kwargs))

    def test_gate_physically_allows_only_context_replacement_actions(self):
        self.assertTrue(agent.context_maintenance_action_allowed(False, "shell"))
        self.assertTrue(agent.context_maintenance_action_allowed(False, "finish"))
        self.assertTrue(agent.context_maintenance_action_allowed(True, "drop_context"))
        self.assertTrue(agent.context_maintenance_action_allowed(True, "compact_context"))
        self.assertFalse(agent.context_maintenance_action_allowed(True, "shell"))
        self.assertFalse(agent.context_maintenance_action_allowed(True, "finish"))

    def test_gate_message_is_ephemeral_inventory_and_requires_drop(self):
        ctx = agent.WorkingContext()
        ctx.append(
            {"role": "user", "content": "RAW_PAYLOAD" * 700},
            removable=True,
            kind="shell_result",
            op_id="OP0009",
            step=9,
            label="sed -n '100,180p' pavlusha_agent/sandbox.py",
        )
        before = list(ctx.messages())
        msg = agent.context_maintenance_gate_message(
            ctx,
            managed_budget=2400,
            enter_ratio=0.75,
            release_ratio=0.55,
        )
        text = msg["content"]
        self.assertIn("CONTEXT MAINTENANCE GATE ACTIVE", text)
        self.assertIn("disabled normal shell and finish actions", text)
        self.assertIn("only accepted actions", text)
        self.assertIn("drop_context", text)
        self.assertIn("compact_context", text)
        self.assertIn("W0001", text)
        self.assertIn("<= 1320 tokens", text)
        self.assertNotIn("RAW_PAYLOAD", text)
        self.assertEqual(ctx.messages(), before)

    def test_gate_pressure_excludes_protected_and_non_working_prompt_material(self):
        ctx = agent.WorkingContext()
        ctx.append({"role": "assistant", "content": "PROTECTED" * 5000})
        ctx.append(
            {"role": "user", "content": "removable" * 100},
            removable=True, kind="shell_result", op_id="OP0010", step=10, label="small result",
        )
        stats = ctx.telemetry()
        self.assertGreater(stats["protected_approx_tokens"], stats["removable_approx_tokens"])
        self.assertEqual(
            agent.context_pressure_level(
                managed_tokens=stats["removable_approx_tokens"], managed_budget=6000
            ),
            "silent",
        )

    def test_parser_defaults_leave_hard_gate_off(self):
        args = agent.build_parser().parse_args(["task"])
        self.assertEqual(args.context_maintenance_gate, "off")
        self.assertEqual(args.context_maintenance_enter_ratio, 0.75)
        self.assertEqual(args.context_maintenance_release_ratio, 0.55)


if __name__ == "__main__":
    unittest.main()
