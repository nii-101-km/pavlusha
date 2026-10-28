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
from pavlusha_modules.project_map.index import ProjectMap, ProjectSymbol, PythonTreeSitterIndexer


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
