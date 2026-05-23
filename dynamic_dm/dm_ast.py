from __future__ import annotations

import re
from dataclasses import dataclass


PATH_RE = re.compile(
    r"^(?P<path>/[A-Za-z0-9_][A-Za-z0-9_/]*)(?P<suffix>\s*\(|\s*=|\s*(?://.*)?$)"
)
RELATIVE_MEMBER_RE = re.compile(
    r"^(?P<kind>proc|verb|var|global|static|tmp)/(?P<name>[A-Za-z0-9_][A-Za-z0-9_/]*)(?:\s*\(|\s*=|\s*(?://.*)?$)"
)
BARE_PROC_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
BARE_VAR_ASSIGNMENT_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=")
MEMBER_MARKERS = ("proc", "verb", "var", "global")
NON_DEFINITION_BARE_WORDS = {
    "if",
    "for",
    "while",
    "switch",
    "spawn",
    "return",
    "set",
    "sleep",
}


@dataclass(frozen=True)
class DmDefinition:
    path: str
    kind: str
    parent_path: str | None
    indent: int
    start_line: int
    end_line: int
    start_offset: int
    header_end_offset: int
    end_offset: int
    header: str
    text: str
    next_boundary: str | None


@dataclass(frozen=True)
class ParsedDmSource:
    definitions: list[DmDefinition]

    @property
    def unique_definitions(self) -> dict[str, DmDefinition]:
        grouped: dict[str, list[DmDefinition]] = {}
        for definition in self.definitions:
            grouped.setdefault(definition.path, []).append(definition)
        return {
            path: definitions[0]
            for path, definitions in grouped.items()
            if len(definitions) == 1
        }


@dataclass
class _OpenDefinition:
    path: str
    kind: str
    parent_path: str | None
    indent: int
    start_line: int
    start_offset: int
    header_end_offset: int
    header: str


def parse_dm_source(source: str) -> ParsedDmSource:
    lines = source.splitlines(keepends=True)
    starts = line_offsets(lines)
    definitions: list[DmDefinition] = []
    stack: list[_OpenDefinition] = []
    in_block_comment = False

    for index, line in enumerate(lines):
        line_number = index + 1
        offset = starts[index]
        parse_line, in_block_comment = strip_comments_for_definition_parse(line, in_block_comment)
        stripped = parse_line.lstrip(" \t")
        if not stripped.strip() or stripped.lstrip().startswith("//"):
            continue
        if stripped.strip().startswith("#"):
            continue

        indent = dm_indent(line)
        while stack and indent <= stack[-1].indent:
            definitions.append(close_definition(stack.pop(), source, line_number - 1, offset, line))

        parsed = parse_definition_line(stripped, indent, stack)
        if not parsed:
            continue

        path, kind, parent_path = parsed
        stack.append(
            _OpenDefinition(
                path=path,
                kind=kind,
                parent_path=parent_path,
                indent=indent,
                start_line=line_number,
                start_offset=offset,
                header_end_offset=offset + len(line),
                header=line,
            )
        )

    eof = len(source)
    end_line = len(lines)
    while stack:
        definitions.append(close_definition(stack.pop(), source, end_line, eof, None))

    definitions.sort(key=lambda definition: (definition.start_offset, definition.end_offset))
    return ParsedDmSource(definitions=definitions)


def parse_definition_line(
    stripped_line: str,
    indent: int,
    stack: list[_OpenDefinition],
) -> tuple[str, str, str | None] | None:
    stripped = stripped_line.strip()
    absolute = PATH_RE.match(stripped)
    if absolute:
        path = absolute_definition_path(absolute.group("path"), absolute.group("suffix"))
        return path, definition_kind(path), parent_path_for(path)

    parent = stack[-1] if stack else None
    relative = RELATIVE_MEMBER_RE.match(stripped)
    if relative and parent is None:
        path = relative_definition_path(None, relative.group("kind"), relative.group("name"))
        return path, definition_kind(path), parent_path_for(path)

    if parent is None or indent <= parent.indent or parent.kind != "type":
        return None

    if relative:
        path = relative_definition_path(parent.path, relative.group("kind"), relative.group("name"))
        return path, definition_kind(path), parent.path

    bare = BARE_PROC_RE.match(stripped)
    if bare and bare.group("name") not in NON_DEFINITION_BARE_WORDS:
        path = normalize_dm_path(f"{parent.path}/proc/{bare.group('name')}")
        return path, "proc", parent.path

    bare_var = BARE_VAR_ASSIGNMENT_RE.match(stripped)
    if bare_var:
        path = normalize_dm_path(f"{parent.path}/var/{bare_var.group('name')}")
        return path, "var", parent.path

    return None


def close_definition(
    open_definition: _OpenDefinition,
    source: str,
    end_line: int,
    end_offset: int,
    boundary_line: str | None,
) -> DmDefinition:
    return DmDefinition(
        path=open_definition.path,
        kind=open_definition.kind,
        parent_path=open_definition.parent_path,
        indent=open_definition.indent,
        start_line=open_definition.start_line,
        end_line=end_line,
        start_offset=open_definition.start_offset,
        header_end_offset=open_definition.header_end_offset,
        end_offset=end_offset,
        header=open_definition.header,
        text=source[open_definition.start_offset:end_offset],
        next_boundary=boundary_line,
    )


def line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    current = 0
    for line in lines:
        offsets.append(current)
        current += len(line)
    return offsets


def dm_indent(line: str) -> int:
    expanded = 0
    for char in line:
        if char == "\t":
            expanded += 4
        elif char == " ":
            expanded += 1
        else:
            break
    return expanded


def strip_comments_for_definition_parse(line: str, in_block_comment: bool) -> tuple[str, bool]:
    parsed: list[str] = []
    index = 0
    while index < len(line):
        if in_block_comment:
            end = line.find("*/", index)
            if end == -1:
                return "".join(parsed), True
            index = end + 2
            in_block_comment = False
            continue
        if line.startswith("//", index):
            break
        if line.startswith("/*", index):
            in_block_comment = True
            index += 2
            continue
        parsed.append(line[index])
        index += 1
    return "".join(parsed), in_block_comment


def normalize_dm_path(path: str) -> str:
    normalized = re.sub(r"/+", "/", path.strip())
    return normalized if normalized.startswith("/") else f"/{normalized}"


def absolute_definition_path(path: str, suffix: str) -> str:
    normalized = normalize_dm_path(path)
    suffix = suffix.lstrip()
    if suffix.startswith("(") and definition_kind(normalized) == "type":
        return member_path(normalized, "proc")
    if suffix.startswith("=") and definition_kind(normalized) == "type":
        return member_path(normalized, "var")
    return normalized


def relative_definition_path(parent_path: str | None, kind: str, name: str) -> str:
    if kind in {"static", "tmp"}:
        path = f"var/{kind}/{name}"
    else:
        path = f"{kind}/{name}"
    if parent_path:
        return normalize_dm_path(f"{parent_path}/{path}")
    return normalize_dm_path(path)


def member_path(path: str, marker: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        return path
    return "/" + "/".join([*parts[:-1], marker, parts[-1]])


def definition_kind(path: str) -> str:
    parts = path.strip("/").split("/")
    marker = first_member_marker(parts)
    if marker == "proc":
        return "proc"
    if marker == "verb":
        return "verb"
    if marker in {"var", "global"}:
        return "var"
    return "type"


def parent_path_for(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) <= 1:
        return None
    marker = first_member_marker(parts)
    if marker is not None:
        index = parts.index(marker)
        return "/" + "/".join(parts[:index]) if index else None
    return "/" + "/".join(parts[:-1])


def first_member_marker(parts: list[str]) -> str | None:
    marker_positions = [
        (parts.index(marker), marker)
        for marker in MEMBER_MARKERS
        if marker in parts
    ]
    if not marker_positions:
        return None
    return min(marker_positions)[1]
