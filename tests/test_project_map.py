from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import ProviderTurn, ShellResult
from pavlusha_agent.project_map import ProjectMap, ProjectSymbol, PythonTreeSitterIndexer
from pavlusha_agent.runtime import run_agent


class CountingIndexer:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def parse(self, source: bytes):
        self.calls.append(source)
        text = source.decode("utf-8", errors="replace")
        line_count = max(1, len(text.splitlines()))
        symbol = ProjectSymbol(
            kind="function",
            name="synthetic",
            signature=f"def synthetic(size={len(source)})",
            start_line=1,
            end_line=line_count,
        )
        return [symbol], False


class ProjectMapCacheTests(unittest.TestCase):
    def test_add_reuse_change_delete_and_persistent_hash_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            state = Path(tmp) / "state"
            root.mkdir()
            state.mkdir()
            source = root / "alpha.py"
            source.write_text("def alpha():\n    return 1\n", encoding="utf-8")

            indexer = CountingIndexer()
            project_map = ProjectMap(root, state / "project_map.json", indexer=indexer)
            first = project_map.refresh()
            self.assertEqual((first.files, first.parsed, first.reused, first.removed), (1, 1, 0, 0))
            self.assertEqual(len(indexer.calls), 1)
            self.assertIn("alpha.py", project_map.text())
            self.assertIn("L1-2 function synthetic", project_map.text())

            second = project_map.refresh()
            self.assertEqual((second.parsed, second.reused, second.removed), (0, 1, 0))
            self.assertEqual(len(indexer.calls), 1)

            source.write_text("def alpha():\n    return 2\n# changed\n", encoding="utf-8")
            third = project_map.refresh()
            self.assertEqual((third.parsed, third.reused, third.removed), (1, 0, 0))
            self.assertEqual(len(indexer.calls), 2)

            # A new controller process reuses the persisted SHA/symbol cache without reparsing.
            fresh_indexer = CountingIndexer()
            reloaded = ProjectMap(root, state / "project_map.json", indexer=fresh_indexer)
            fourth = reloaded.refresh()
            self.assertEqual((fourth.parsed, fourth.reused), (0, 1))
            self.assertEqual(fresh_indexer.calls, [])

            source.unlink()
            fifth = reloaded.refresh()
            self.assertEqual((fifth.files, fifth.parsed, fifth.reused, fifth.removed), (0, 0, 0, 1))
            self.assertNotIn("alpha.py", reloaded.text())

    def test_new_file_is_added_and_ignored_generated_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            (root / "pkg").mkdir()
            (root / "pkg" / "real.py").write_text("x = 1\n", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "ghost.py").write_text("x = 2\n", encoding="utf-8")
            (root / "bench-runs").mkdir()
            (root / "bench-runs" / "old.py").write_text("x = 3\n", encoding="utf-8")
            indexer = CountingIndexer()
            project_map = ProjectMap(root, Path(tmp) / "state/map.json", indexer=indexer)
            result = project_map.refresh()
            self.assertEqual(result.files, 1)
            self.assertIn("pkg/real.py", project_map.text())
            self.assertNotIn("ghost.py", project_map.text())
            self.assertNotIn("old.py", project_map.text())

            (root / "pkg" / "new.py").write_text("def new():\n    pass\n", encoding="utf-8")
            result = project_map.refresh()
            self.assertEqual((result.files, result.parsed, result.reused), (2, 1, 1))
            self.assertIn("pkg/new.py", project_map.text())

    def test_symlinked_python_file_is_never_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            outside = Path(tmp) / "outside.py"
            outside.write_text("TOP_SECRET = True\n", encoding="utf-8")
            (root / "leak.py").symlink_to(outside)
            indexer = CountingIndexer()
            project_map = ProjectMap(root, Path(tmp) / "state/map.json", indexer=indexer)
            result = project_map.refresh()
            self.assertEqual(result.files, 0)
            self.assertEqual(indexer.calls, [])
            self.assertNotIn("leak.py", project_map.text())

    def test_reset_discards_old_map_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            state = Path(tmp) / "state"
            root.mkdir()
            state.mkdir()
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            first = CountingIndexer()
            ProjectMap(root, state / "map.json", indexer=first).refresh()
            second = CountingIndexer()
            ProjectMap(root, state / "map.json", indexer=second, reset=True).refresh()
            self.assertEqual(len(second.calls), 1)


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter") is not None and importlib.util.find_spec("tree_sitter_python") is not None,
    "Tree-sitter controller dependencies are not installed in this test environment",
)
class TreeSitterIntegrationTests(unittest.TestCase):
    def test_python_symbols_signatures_qualification_and_ranges(self):
        source = b'''class Box(Base):\n    def open(self, x: int) -> str:\n        def nested():\n            return "x"\n        return str(x)\n\nasync def fetch(url):\n    return url\n'''
        symbols, has_error = PythonTreeSitterIndexer().parse(source)
        self.assertFalse(has_error)
        by_name = {item.name: item for item in symbols}
        self.assertEqual(set(by_name), {"Box", "Box.open", "Box.open.nested", "fetch"})
        self.assertEqual(by_name["Box"].signature, "class Box(Base)")
        self.assertEqual(by_name["Box.open"].signature, "def open(self, x: int) -> str")
        self.assertTrue(by_name["fetch"].signature.startswith("async def fetch"))
        self.assertEqual((by_name["Box.open"].start_line, by_name["Box.open"].end_line), (2, 5))


class RuntimeProjectMapTests(unittest.TestCase):
    def _run(self, tmp, FakeMap, actions):
        seen_messages = []
        pending = iter(actions)

        def completion(self, messages, on_delta=None, **kwargs):
            seen_messages.append(messages)
            return ProviderTurn(content=json.dumps(next(pending)), reasoning_content="",
                                finish_reason="stop", prompt_tokens=100, completion_tokens=10)

        def shell(workdir, command, **kwargs):
            return ShellResult(command=command, network=False, exit_code=0, timed_out=False,
                               duration=0.01, stdout="ok", stderr="")

        with patch("pavlusha_agent.runtime.ProjectMap", FakeMap), \
             patch("pavlusha_agent.runtime.shutil.which", return_value="/test/bwrap"), \
             patch("pavlusha_agent.runtime.ChatProvider.worker_completion", completion), \
             patch("pavlusha_agent.runtime.run_shell", side_effect=shell):
            args = build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off',
                "--workdir", str(Path(tmp) / "work"), "--state-dir", str(Path(tmp) / "state"),
                "--worker-context-budget", "40000", "--project-map", "on",
                "--project-review-every", "0",
                "--max-steps", str(len(actions)), "task",
            ])
            self.assertEqual(run_agent(args), 0)
        return seen_messages

    def test_project_map_is_checkpoint_snapshot_outside_working_context(self):
        class FakeMap:
            def __init__(self, root, cache_path, *, reset=False, indexer=None): self.refreshes = 0
            def refresh(self):
                self.refreshes += 1
                return SimpleNamespace(files=1, parsed=1, reused=0, removed=0, map_changed=True)
            def message(self):
                return {"role": "user", "content": "PROJECT MAP\npkg/a.py\n  L1-2 function a: def a()"}

        actions = [
            {"action": "project_init", "design": [], "work": [{"objective": "verify", "status": "ACTIVE", "deliverables": []}, {"objective": "finish verification", "status": "PLANNED", "deliverables": []}]},
            {"action": "shell", "command": "echo ok"},
            {"action": "project_update", "changes": [{"op": "update_work", "id": "W001", "status": "DONE",
                "evidence": ["SHELL RESULT: echo ok -> exit 0"]},
                {"op": "update_work", "id": "W002", "status": "DONE",
                "evidence": ["SHELL RESULT: echo ok -> exit 0"]}]},
            {"action": "finish", "summary": "done"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            seen = self._run(tmp, FakeMap, actions)
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(sum(m["content"].startswith("PROJECT MAP") for m in request) == 1 for request in seen))
        self.assertIn("checkpoint navigation snapshot", seen[0][0]["content"])

    def test_map_stays_frozen_after_shell_and_local_state_update(self):
        class FakeMap:
            def __init__(self, root, cache_path, *, reset=False, indexer=None): self.version = 0
            def refresh(self):
                self.version += 1
                return SimpleNamespace(files=1, parsed=1, reused=0, removed=0, map_changed=True)
            def message(self): return {"role": "user", "content": f"PROJECT MAP VERSION {self.version}"}

        actions = [
            {"action": "project_init", "design": [], "work": [{"objective": "change", "status": "ACTIVE", "deliverables": []}, {"objective": "verify change", "status": "PLANNED", "deliverables": []}]},
            {"action": "shell", "command": "printf changed > a.py"},
            {"action": "project_update", "changes": [{"op": "update_work", "id": "W001", "status": "DONE",
                "evidence": ["SHELL RESULT: printf changed > a.py -> exit 0"]},
                {"op": "update_work", "id": "W002", "status": "DONE",
                "evidence": ["SHELL RESULT: printf changed > a.py -> exit 0"]}]},
            {"action": "finish", "summary": "done"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            seen = self._run(tmp, FakeMap, actions)
        for request in seen:
            self.assertEqual(request[2]["content"], "PROJECT MAP VERSION 1")


if __name__ == "__main__":
    unittest.main()
