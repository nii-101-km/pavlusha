"""Deterministic controller-owned Project Map for Worker navigation.

The map is intentionally small and mechanical: file paths, Python symbols,
signatures, and line ranges.  It is not semantic memory and never becomes a
WorkingContext item.  Raw file SHA-256 values live only in the controller cache
so unchanged files do not need to be parsed again.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core import AgentError

MAP_SCHEMA_VERSION = 1
DEFAULT_IGNORE_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "build",
    "dist",
    "bench-runs",
})


@dataclass(frozen=True)
class ProjectSymbol:
    kind: str
    name: str
    signature: str
    start_line: int
    end_line: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "signature": self.signature,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectSymbol":
        return cls(
            kind=str(value.get("kind", "symbol")),
            name=str(value.get("name", "")),
            signature=str(value.get("signature", "")),
            start_line=int(value.get("start_line", 0)),
            end_line=int(value.get("end_line", 0)),
        )


@dataclass(frozen=True)
class ProjectMapRefresh:
    files: int
    parsed: int
    reused: int
    removed: int
    map_changed: bool


class SourceIndexer(Protocol):
    def parse(self, source: bytes) -> tuple[list[ProjectSymbol], bool]: ...


class PythonTreeSitterIndexer:
    """Small adapter around the official py-tree-sitter Python grammar."""

    def __init__(self) -> None:
        try:
            import tree_sitter_python as tspython
            from tree_sitter import Language, Parser
        except ImportError as exc:
            raise AgentError(
                "Project Map requires Tree-sitter. Install controller dependencies with "
                "`python3 -m pip install -r requirements.txt` (tree-sitter and tree-sitter-python)."
            ) from exc
        try:
            language = Language(tspython.language())
            self._parser = Parser(language)
        except Exception as exc:
            raise AgentError(f"cannot initialize Tree-sitter Python parser: {exc}") from exc

    @staticmethod
    def _text(source: bytes, node: Any) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    @classmethod
    def _signature(cls, source: bytes, node: Any) -> str:
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        header = source[node.start_byte:end].decode("utf-8", errors="replace")
        # Keep exact lexical content modulo whitespace so the map stays compact and one-line.
        header = " ".join(header.split()).rstrip()
        if header.endswith(":"):
            header = header[:-1].rstrip()
        return header

    def parse(self, source: bytes) -> tuple[list[ProjectSymbol], bool]:
        tree = self._parser.parse(source)
        if tree is None:
            raise AgentError("Tree-sitter returned no syntax tree")
        symbols: list[ProjectSymbol] = []

        def visit(node: Any, parents: tuple[str, ...]) -> None:
            node_type = node.type
            if node_type in {"class_definition", "function_definition"}:
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    return
                local_name = self._text(source, name_node)
                qualified_name = ".".join((*parents, local_name)) if parents else local_name
                symbols.append(ProjectSymbol(
                    kind="class" if node_type == "class_definition" else "function",
                    name=qualified_name,
                    signature=self._signature(source, node),
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                ))
                body = node.child_by_field_name("body")
                if body is not None:
                    for child in body.named_children:
                        visit(child, (*parents, local_name))
                return
            for child in node.named_children:
                visit(child, parents)

        visit(tree.root_node, ())
        symbols.sort(key=lambda item: (item.start_line, item.end_line, item.name, item.kind))
        return symbols, bool(tree.root_node.has_error)


class ProjectMap:
    """Hash-aware current-code map stored outside the Worker sandbox."""

    def __init__(
        self,
        root: Path,
        cache_path: Path,
        *,
        indexer: SourceIndexer | None = None,
        ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS,
        reset: bool = False,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.cache_path = cache_path.expanduser().resolve()
        self.ignore_dirs = ignore_dirs
        self._indexer = indexer
        if reset:
            try:
                self.cache_path.unlink()
            except FileNotFoundError:
                pass
        self._files: dict[str, dict[str, Any]] = self._load_cache()
        self._rendered = self._render()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema_version") != MAP_SCHEMA_VERSION:
            return {}
        files = raw.get("files")
        if not isinstance(files, dict):
            return {}
        clean: dict[str, dict[str, Any]] = {}
        for path, record in files.items():
            if not isinstance(path, str) or not isinstance(record, dict):
                continue
            sha256 = record.get("sha256")
            symbols = record.get("symbols")
            if not isinstance(sha256, str) or not isinstance(symbols, list):
                continue
            try:
                normalized_symbols = [
                    ProjectSymbol.from_dict(item).as_dict()
                    for item in symbols if isinstance(item, dict)
                ]
            except (TypeError, ValueError):
                return {}
            clean[path] = {
                "sha256": sha256,
                "has_error": bool(record.get("has_error", False)),
                "symbols": normalized_symbols,
            }
        return clean

    @property
    def indexer(self) -> SourceIndexer:
        if self._indexer is None:
            self._indexer = PythonTreeSitterIndexer()
        return self._indexer

    def _iter_python_files(self) -> list[Path]:
        out: list[Path] = []
        for current, dirnames, filenames in os.walk(self.root, topdown=True, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for name in sorted(dirnames):
                candidate = current_path / name
                if name in self.ignore_dirs or candidate.is_symlink():
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                candidate = current_path / name
                # Never let a Worker-created symlink make the controller read outside /work.
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                out.append(candidate)
        out.sort(key=lambda path: path.relative_to(self.root).as_posix())
        return out

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": MAP_SCHEMA_VERSION,
            "files": {path: self._files[path] for path in sorted(self._files)},
        }
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.cache_path)

    def refresh(self) -> ProjectMapRefresh:
        previous_render = self._rendered
        old_paths = set(self._files)
        current: dict[str, dict[str, Any]] = {}
        parsed = 0
        reused = 0

        for path in self._iter_python_files():
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                # A host-side edit raced the scan. The next refresh will see the settled tree.
                continue
            relative = path.relative_to(self.root).as_posix()
            digest = self._sha256(data)
            cached = self._files.get(relative)
            if cached is not None and cached.get("sha256") == digest:
                current[relative] = cached
                reused += 1
                continue
            symbols, has_error = self.indexer.parse(data)
            current[relative] = {
                "sha256": digest,
                "has_error": bool(has_error),
                "symbols": [item.as_dict() for item in symbols],
            }
            parsed += 1

        removed = len(old_paths - set(current))
        cache_changed = current != self._files
        self._files = current
        self._rendered = self._render()
        if cache_changed:
            self._save_cache()
        return ProjectMapRefresh(
            files=len(current),
            parsed=parsed,
            reused=reused,
            removed=removed,
            map_changed=self._rendered != previous_render,
        )

    def _render(self) -> str:
        lines = [
            "PROJECT MAP (controller-generated current /work; navigation aid, not memory)",
            "The filesystem remains authoritative. Python files are indexed as symbols/signatures/ranges.",
        ]
        if not self._files:
            lines.append("(no indexed Python files)")
            return "\n".join(lines)
        for relative in sorted(self._files):
            record = self._files[relative]
            suffix = " [parse-errors]" if record.get("has_error") else ""
            lines.append(f"{relative}{suffix}")
            symbols = [ProjectSymbol.from_dict(item) for item in record.get("symbols", [])]
            if not symbols:
                lines.append("  (no class/function symbols)")
                continue
            for symbol in symbols:
                lines.append(
                    f"  L{symbol.start_line}-{symbol.end_line} {symbol.kind} "
                    f"{symbol.name}: {symbol.signature}"
                )
        return "\n".join(lines)

    def text(self) -> str:
        return self._rendered

    def message(self) -> dict[str, str]:
        return {"role": "user", "content": self.text()}
