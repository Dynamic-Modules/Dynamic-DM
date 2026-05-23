from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable


MIN_REPLACE_LINE_CAP = 24
MAX_DELETE_LINES = 100
MAX_INSERT_LINES = 300
MAX_REPLACE_ADDED_LINES = 180
MAX_REPLACE_REMOVED_LINES = 100
MAX_OPERATIONS = 80
REPLACE_LINE_CAP_FILE_FRACTION = 0.15
CONTEXT_SEARCH_LIMIT = 10


@dataclass(frozen=True)
class PatchOperation:
    mode: str
    anchor: str
    content: str
    end_anchor: str | None = None
    occurrence: int = 1
    strategy: str = "line-hunk"
    sort_index: int = 0


@dataclass(frozen=True)
class PatchSet:
    target_file: str
    operations: list[PatchOperation] = field(default_factory=list)
    strategy: str = "unchanged"
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True)
class WriteResult:
    manifest_path: Path
    patch_files: list[Path]


def create_patch_set(
    upstream_source: str,
    local_source: str,
    target_file: str,
    *,
    allow_full_file_fallback: bool = True,
) -> PatchSet:
    if upstream_source == local_source:
        return PatchSet(target_file=target_file)

    warnings: list[str] = []
    candidates = [
        ("line replacements", infer_independent_line_replacements),
        ("single hunk", infer_single_hunk),
        ("multiple hunks", infer_line_hunks),
    ]
    for strategy, infer in candidates:
        operations = infer(upstream_source, local_source)
        if operations and operations_reproduce(upstream_source, local_source, operations):
            return PatchSet(target_file=target_file, operations=operations, strategy=strategy)

    if allow_full_file_fallback:
        operation = PatchOperation(
            mode="replace",
            anchor=upstream_source,
            content=local_source,
            occurrence=1,
            strategy="full-file-replace",
        )
        if operations_reproduce(upstream_source, local_source, [operation]):
            warnings.append(
                "Could not infer narrow operations; generated a full-file replace patch."
            )
            return PatchSet(
                target_file=target_file,
                operations=[operation],
                strategy="full-file-replace",
                warnings=warnings,
            )

    warnings.append("Could not infer patch operations that reproduce the local source.")
    return PatchSet(target_file=target_file, warnings=warnings)


def apply_patch_operations(source: str, operations: Iterable[PatchOperation]) -> str:
    patched = source
    for operation in operations:
        patched = apply_patch_operation(patched, operation)
    return patched


def apply_patch_operation(source: str, operation: PatchOperation) -> str:
    spans = find_anchor_spans(source, operation.anchor)
    if len(spans) < operation.occurrence:
        raise ValueError(
            f"anchor occurrence {operation.occurrence} not found for {operation.anchor!r}"
        )
    start, end, _line = spans[operation.occurrence - 1]
    content = line_mode_content(operation.content, operation.anchor)

    if operation.mode == "insert_before":
        return f"{source[:start]}{content}{source[start:]}"
    if operation.mode == "insert_after":
        return f"{source[:end]}{content}{source[end:]}"
    if operation.mode == "replace":
        return f"{source[:start]}{content}{source[end:]}"
    if operation.mode == "replace_between":
        if operation.end_anchor is None:
            raise ValueError("replace_between requires end_anchor")
        end_span = find_first_anchor_span_after(source, operation.end_anchor, end)
        if end_span is None:
            raise ValueError(f"end anchor not found after start anchor: {operation.end_anchor!r}")
        end_start, _end_end, _end_line = end_span
        return f"{source[:end]}{operation.content}{source[end_start:]}"
    raise ValueError(f"unsupported patch mode {operation.mode!r}")


def operations_reproduce(
    upstream_source: str,
    local_source: str,
    operations: list[PatchOperation],
) -> bool:
    try:
        return apply_patch_operations(upstream_source, operations) == local_source
    except ValueError:
        return False


def infer_independent_line_replacements(
    upstream_source: str,
    local_source: str,
) -> list[PatchOperation] | None:
    upstream_lines = upstream_source.splitlines(keepends=True)
    local_lines = local_source.splitlines(keepends=True)
    if len(upstream_lines) != len(local_lines):
        return None

    operations: list[PatchOperation] = []
    for index, (upstream_line, local_line) in enumerate(zip(upstream_lines, local_lines)):
        if upstream_line == local_line:
            continue
        anchor = line_anchor(upstream_line)
        if not anchor:
            return None
        operations.append(
            PatchOperation(
                mode="replace",
                anchor=anchor,
                content=local_line,
                occurrence=line_anchor_occurrence(upstream_lines, anchor, index),
                strategy="line-replace",
                sort_index=index,
            )
        )

    if not operations or len(operations) > MAX_OPERATIONS:
        return None
    return sorted_for_application(operations)


def infer_single_hunk(upstream_source: str, local_source: str) -> list[PatchOperation] | None:
    upstream_lines = upstream_source.splitlines(keepends=True)
    local_lines = local_source.splitlines(keepends=True)
    start = 0

    while (
        start < len(upstream_lines)
        and start < len(local_lines)
        and upstream_lines[start] == local_lines[start]
    ):
        start += 1

    upstream_end = len(upstream_lines)
    local_end = len(local_lines)
    while (
        upstream_end > start
        and local_end > start
        and upstream_lines[upstream_end - 1] == local_lines[local_end - 1]
    ):
        upstream_end -= 1
        local_end -= 1

    operation = infer_hunk_operation(
        upstream_lines,
        start,
        upstream_end,
        upstream_lines[start:upstream_end],
        local_lines[start:local_end],
        upstream_source,
    )
    return [operation] if operation else None


def infer_line_hunks(upstream_source: str, local_source: str) -> list[PatchOperation] | None:
    upstream_lines = upstream_source.splitlines(keepends=True)
    local_lines = local_source.splitlines(keepends=True)
    matcher = SequenceMatcher(None, upstream_lines, local_lines, autojunk=False)
    operations: list[PatchOperation] = []

    for tag, upstream_start, upstream_end, local_start, local_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        operation = infer_hunk_operation(
            upstream_lines,
            upstream_start,
            upstream_end,
            upstream_lines[upstream_start:upstream_end],
            local_lines[local_start:local_end],
            upstream_source,
        )
        if not operation:
            return None
        operations.append(operation)

    if len(operations) < 2 or len(operations) > MAX_OPERATIONS:
        return None
    return sorted_for_application(operations)


def infer_hunk_operation(
    upstream_lines: list[str],
    start: int,
    upstream_end: int,
    removed_lines: list[str],
    added_lines: list[str],
    upstream_source: str,
) -> PatchOperation | None:
    if not removed_lines and not added_lines:
        return None
    if not hunk_within_dynamic_cap(upstream_lines, removed_lines, added_lines):
        return None

    if not removed_lines:
        return infer_insert(upstream_lines, start, added_lines)

    added_text = "".join(added_lines)
    removed_text = "".join(removed_lines)
    bounded = infer_bounded_replace(upstream_lines, start, upstream_end, added_text)
    if bounded and operations_reproduce(upstream_source, patch_hunk(upstream_source, start, upstream_end, added_text), [bounded]):
        return bounded

    if removed_text and count_occurrences(upstream_source, removed_text) == 1:
        return PatchOperation(
            mode="replace",
            anchor=removed_text,
            content=added_text,
            occurrence=1,
            strategy="unique-block-replace",
            sort_index=start,
        )

    contextual = infer_contextual_replace(
        upstream_lines,
        start,
        upstream_end,
        added_lines,
        upstream_source,
    )
    if contextual:
        return contextual

    if len(removed_lines) == 1:
        anchor = line_anchor(removed_lines[0])
        if anchor:
            return PatchOperation(
                mode="replace",
                anchor=anchor,
                content=added_text,
                occurrence=line_anchor_occurrence(upstream_lines, anchor, start),
                strategy="line-replace",
                sort_index=start,
            )

    if removed_text:
        return PatchOperation(
            mode="replace",
            anchor=removed_text,
            content=added_text,
            occurrence=block_occurrence(upstream_source, removed_text, start),
            strategy="block-replace",
            sort_index=start,
        )
    return None


def patch_hunk(source: str, start: int, end: int, added_text: str) -> str:
    lines = source.splitlines(keepends=True)
    return "".join([*lines[:start], added_text, *lines[end:]])


def infer_bounded_replace(
    upstream_lines: list[str],
    start: int,
    upstream_end: int,
    added_text: str,
) -> PatchOperation | None:
    if start <= 0 or upstream_end >= len(upstream_lines):
        return None
    start_anchor = line_anchor(upstream_lines[start - 1])
    end_anchor = line_anchor(upstream_lines[upstream_end])
    if not start_anchor or not end_anchor:
        return None
    return PatchOperation(
        mode="replace_between",
        anchor=start_anchor,
        end_anchor=end_anchor,
        content=added_text,
        occurrence=line_anchor_occurrence(upstream_lines, start_anchor, start - 1),
        strategy="bounded-replace",
        sort_index=start,
    )


def infer_insert(
    upstream_lines: list[str],
    start: int,
    added_lines: list[str],
) -> PatchOperation | None:
    content = "".join(added_lines)
    if start > 0:
        anchor = line_anchor(upstream_lines[start - 1])
        if anchor:
            return PatchOperation(
                mode="insert_after",
                anchor=anchor,
                content=content,
                occurrence=line_anchor_occurrence(upstream_lines, anchor, start - 1),
                strategy="line-insert-after",
                sort_index=start,
            )

    if start < len(upstream_lines):
        anchor = line_anchor(upstream_lines[start])
        if anchor:
            return PatchOperation(
                mode="insert_before",
                anchor=anchor,
                content=content,
                occurrence=line_anchor_occurrence(upstream_lines, anchor, start),
                strategy="line-insert-before",
                sort_index=start,
            )

    return None


def infer_contextual_replace(
    upstream_lines: list[str],
    start: int,
    upstream_end: int,
    added_lines: list[str],
    upstream_source: str,
) -> PatchOperation | None:
    for before_count in range(1, CONTEXT_SEARCH_LIMIT + 1):
        before_start = start - before_count
        if before_start < 0:
            break
        for after_count in range(1, CONTEXT_SEARCH_LIMIT + 1):
            after_end = upstream_end + after_count
            if after_end > len(upstream_lines):
                break
            before_lines = upstream_lines[before_start:start]
            removed_lines = upstream_lines[start:upstream_end]
            after_lines = upstream_lines[upstream_end:after_end]
            anchor = "".join([*before_lines, *removed_lines, *after_lines])
            content = "".join([*before_lines, *added_lines, *after_lines])
            if anchor and count_occurrences(upstream_source, anchor) == 1:
                return PatchOperation(
                    mode="replace",
                    anchor=anchor,
                    content=content,
                    occurrence=1,
                    strategy="contextual-replace",
                    sort_index=start,
                )
    return None


def hunk_within_dynamic_cap(
    upstream_lines: list[str],
    removed_lines: list[str],
    added_lines: list[str],
) -> bool:
    if not removed_lines:
        return len(added_lines) <= MAX_INSERT_LINES
    if not added_lines:
        return len(removed_lines) <= MAX_DELETE_LINES

    file_scaled_cap = int(len(upstream_lines) * REPLACE_LINE_CAP_FILE_FRACTION) + 1
    removed_cap = max(MIN_REPLACE_LINE_CAP, min(MAX_REPLACE_REMOVED_LINES, file_scaled_cap))
    return len(removed_lines) <= removed_cap and len(added_lines) <= MAX_REPLACE_ADDED_LINES


def sorted_for_application(operations: list[PatchOperation]) -> list[PatchOperation]:
    # Occurrence values are calculated against the original file. Applying repeated
    # line replacements from the bottom keeps later occurrences addressable.
    return sorted(
        operations,
        key=lambda operation: (operation.sort_index, operation.anchor, operation.occurrence),
        reverse=True,
    )


def find_anchor_spans(source: str, anchor: str) -> list[tuple[int, int, int]]:
    if "\n" in anchor:
        return find_block_anchor_spans(source, anchor)
    return find_line_anchor_spans(source, anchor)


def find_first_anchor_span_after(
    source: str,
    anchor: str,
    offset: int,
) -> tuple[int, int, int] | None:
    for span in find_anchor_spans(source, anchor):
        if span[0] >= offset:
            return span
    return None


def find_line_anchor_spans(source: str, anchor: str) -> list[tuple[int, int, int]]:
    matches: list[tuple[int, int, int]] = []
    offset = 0
    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        line_end = offset + len(line)
        if anchor in line:
            matches.append((offset, line_end, line_number))
        offset = line_end
    return matches


def find_block_anchor_spans(source: str, anchor: str) -> list[tuple[int, int, int]]:
    matches: list[tuple[int, int, int]] = []
    index = source.find(anchor)
    while index != -1:
        matches.append((index, index + len(anchor), source.count("\n", 0, index) + 1))
        index = source.find(anchor, index + max(1, len(anchor)))
    return matches


def line_mode_content(content: str, anchor: str) -> str:
    if content == "" or "\n" in anchor or content.endswith("\n"):
        return content
    return content + "\n"


def line_anchor(line: str) -> str:
    return line.rstrip("\r\n")


def line_anchor_occurrence(lines: list[str], anchor: str, line_index: int) -> int:
    count = 0
    for index, line in enumerate(lines[: line_index + 1]):
        if anchor in line:
            count += 1
        if index == line_index:
            return max(1, count)
    return max(1, count)


def block_occurrence(source: str, anchor: str, start_line_index: int) -> int:
    if not anchor:
        return 1
    prefix = "".join(source.splitlines(keepends=True)[:start_line_index])
    return count_occurrences(prefix, anchor) + 1


def count_occurrences(source: str, anchor: str) -> int:
    if not anchor:
        return 0
    count = 0
    index = source.find(anchor)
    while index != -1:
        count += 1
        index = source.find(anchor, index + max(1, len(anchor)))
    return count


def list_modified_dm_files(repo_root: Path, upstream_ref: str) -> list[str]:
    output = run_text(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=M",
            upstream_ref,
            "--",
            "*.dm",
        ],
        repo_root,
    )
    return [
        item
        for item in output.splitlines()
        if item
        and not item.startswith(".dynamic_modules_build/")
        and not item.startswith("dynamic_modules/")
    ]


def read_git_file(repo_root: Path, ref: str, repo_path: str) -> str:
    return run_text(["git", "show", f"{ref}:{repo_path}"], repo_root)


def run_text(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{' '.join(cmd)} failed")
    return result.stdout


def write_patch_module(
    output_dir: Path,
    module_id: str,
    module_name: str,
    patch_sets: list[PatchSet],
    *,
    base_ref: str,
) -> WriteResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    patch_files: list[Path] = []
    manifest_path = output_dir / f"{module_id}.module.toml"
    patch_entries: list[tuple[PatchSet, PatchOperation, str, str]] = []

    for patch_set in patch_sets:
        for index, operation in enumerate(patch_set.operations, start=1):
            patch_id = f"{slug_target(patch_set.target_file)}-{index}"
            relative_patch_path = f"patches/{patch_id}.dm"
            patch_path = output_dir / relative_patch_path
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(operation.content, encoding="utf-8", newline="\n")
            patch_files.append(patch_path)
            patch_entries.append((patch_set, operation, relative_patch_path, patch_id))

    manifest_path.write_text(
        render_module_manifest(
            module_id,
            module_name,
            patch_entries,
            base_ref=base_ref,
        ),
        encoding="utf-8",
        newline="\n",
    )
    readme_path = output_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(
            f"# {module_name}\n\nConverted Dynamic DM patch module.\n",
            encoding="utf-8",
            newline="\n",
        )
    return WriteResult(manifest_path=manifest_path, patch_files=patch_files)


def render_module_manifest(
    module_id: str,
    module_name: str,
    patch_entries: list[tuple[PatchSet, PatchOperation, str, str]],
    *,
    base_ref: str,
) -> str:
    lines = [
        f"id = {toml_string(module_id)}",
        f"name = {toml_string(module_name)}",
        'version = "0.1.0"',
        'module_api = "1"',
        f"description = {toml_string(f'Converted Dynamic DM patches from {base_ref}.')}",
        "",
        "[load]",
        'requires = ["dynamic-dm"]',
        "",
        "[compat]",
        'target = "tgstation"',
        'minimum_dynamic_modules = "1.0.0"',
        "",
        "[build]",
        "dm_files = []",
        "test_files = []",
        "assets = []",
        "tgui = []",
        "",
    ]

    for patch_set, operation, patch_file, patch_id in patch_entries:
        lines.extend(
            [
                "[[patches]]",
                f"id = {toml_string(patch_id)}",
                f"target_file = {toml_string(patch_set.target_file)}",
                f"mode = {toml_string(operation.mode)}",
                f"anchor = {toml_string(operation.anchor)}",
                *(
                    [f"end_anchor = {toml_string(operation.end_anchor)}"]
                    if operation.end_anchor is not None
                    else []
                ),
                f"file = {toml_string(patch_file)}",
                f"occurrence = {operation.occurrence}",
                'risk = "converted_dynamic_dm"',
                f"description = {toml_string(f'Generated by Dynamic DM using {operation.strategy}.')}",
                "",
            ]
        )
    return "\n".join(lines)


def toml_string(value: str) -> str:
    return json.dumps(value)


def slug_target(target: str) -> str:
    slug = target.replace("\\", "/").rsplit(".", 1)[0]
    slug = "".join(char if char.isalnum() else "-" for char in slug)
    slug = "-".join(part for part in slug.lower().split("-") if part)
    return slug[:90] or "patch"
