from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any, Iterable
import csv
import json
import re
import subprocess


SPECIAL_UNIQUE_SYMBOLS = {"GME", "MACRO"}
SOURCE_EXTENSIONS = {".cc", ".cpp", ".cxx"}
REQUIRED_CATALOG_FIELDS = {"ACIS头文件名", "模块名", "类型", "元素唯一标识", "父元素"}


@dataclass(frozen=True, slots=True)
class TestBlock:
    suite: str
    name: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class PropertyOccurrence:
    file: str
    suite: str
    test: str
    line: int
    offset: int
    value: str | None
    expression: str

    def evidence(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "suite": self.suite,
            "test": self.test,
            "line": self.line,
        }


def generate_catalogs(
    gme_root: str | Path,
    acis_symbol_dir: str | Path,
    modules: Iterable[str],
    output_root: str | Path,
) -> list[tuple[Path, dict[str, Any]]]:
    root = Path(gme_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"GME repository does not exist: {root}")

    catalog_rows, catalog_digest = load_acis_symbol_catalog(acis_symbol_dir)
    normalized_modules = sorted({_module_name(module) for module in modules})
    if not normalized_modules:
        normalized_modules = _discover_modules(root)
    if not normalized_modules:
        raise ValueError(f"No test modules found under {root / 'tests/gme/src'}")

    destination = Path(output_root).resolve()
    generated: list[tuple[Path, dict[str, Any]]] = []
    for module in normalized_modules:
        catalog = build_module_catalog(root, module, catalog_rows, catalog_digest)
        path = write_catalog(catalog, destination / f"{module}.json")
        generated.append((path, catalog))
    return generated


def load_acis_symbol_catalog(
    acis_symbol_dir: str | Path,
) -> tuple[dict[str, list[dict[str, str]]], str]:
    root = _resolve_catalog_directory(Path(acis_symbol_dir).resolve())
    csv_paths = sorted(path for path in root.glob("*.csv") if path.is_file())
    if not csv_paths:
        raise FileNotFoundError(f"No acis_symbol CSV files found in: {root}")

    digest = sha256()
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in csv_paths:
        content = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or [])
            missing = REQUIRED_CATALOG_FIELDS - fields
            if missing:
                names = ", ".join(sorted(missing))
                raise ValueError(f"acis_symbol CSV is missing fields ({names}): {path}")
            for row in reader:
                symbol = str(row.get("元素唯一标识") or "").strip()
                if not symbol:
                    continue
                by_symbol[symbol].append(
                    {
                        "source_catalog": path.name,
                        "acis_header": str(row.get("ACIS头文件名") or "").strip(),
                        "catalog_module": str(row.get("模块名") or "").strip(),
                        "kind": str(row.get("类型") or "").strip(),
                        "parent": str(row.get("父元素") or "").strip(),
                    }
                )
    return dict(by_symbol), digest.hexdigest()


def build_module_catalog(
    gme_root: str | Path,
    module: str,
    registered_symbols: dict[str, list[dict[str, str]]],
    acis_catalog_sha256: str,
) -> dict[str, Any]:
    root = Path(gme_root).resolve()
    normalized_module = _module_name(module)
    test_root = root / "tests" / "gme" / "src" / normalized_module
    if not test_root.is_dir():
        raise FileNotFoundError(f"Test module does not exist: {test_root}")

    occurrences: list[PropertyOccurrence] = []
    tests_without_property: list[dict[str, str]] = []
    duplicate_property_tests: list[dict[str, Any]] = []
    test_count = 0
    source_files = sorted(
        path for path in test_root.rglob("*") if path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS
    )
    for source_path in source_files:
        text = source_path.read_text(encoding="utf-8", errors="replace")
        relative = source_path.relative_to(root).as_posix()
        blocks = list(_iter_test_blocks(text))
        test_count += len(blocks)
        properties = list(_iter_unique_symbol_properties(text, relative, blocks))
        occurrences.extend(properties)

        for block in blocks:
            count = sum(1 for item in properties if block.start <= item.offset < block.end)
            if count == 0:
                tests_without_property.append(
                    {"file": relative, "suite": block.suite, "test": block.name}
                )
            elif count > 1:
                duplicate_property_tests.append(
                    {
                        "file": relative,
                        "suite": block.suite,
                        "test": block.name,
                        "property_count": count,
                    }
                )

    registered: dict[str, list[PropertyOccurrence]] = defaultdict(list)
    special: dict[str, list[PropertyOccurrence]] = defaultdict(list)
    unregistered: dict[str, list[PropertyOccurrence]] = defaultdict(list)
    dynamic: list[PropertyOccurrence] = []
    for occurrence in occurrences:
        if occurrence.value is None:
            dynamic.append(occurrence)
            continue
        value = occurrence.value.strip()
        if value.upper() in SPECIAL_UNIQUE_SYMBOLS:
            special[value.upper()].append(occurrence)
        elif value in registered_symbols:
            registered[value].append(occurrence)
        else:
            unregistered[value].append(occurrence)

    interfaces = [
        _build_interface(normalized_module, symbol, evidence, registered_symbols[symbol])
        for symbol, evidence in sorted(registered.items())
    ]
    source_counts = Counter(item["source_catalog"] for item in interfaces)
    registered_occurrences = sum(len(items) for items in registered.values())
    special_occurrences = sum(len(items) for items in special.values())
    unregistered_occurrences = sum(len(items) for items in unregistered.values())
    return {
        "schema_version": 1,
        "module": normalized_module,
        "source": {
            "gme_root_commit": _git_revision(root),
            "test_repository_commit": _git_revision(root / "tests" / "gme"),
            "test_root": test_root.relative_to(root).as_posix(),
            "acis_catalog_sha256": acis_catalog_sha256,
            "generator": "scripts/generate_interface_catalog.py",
        },
        "summary": {
            "test_files": len(source_files),
            "test_cases": test_count,
            "record_property_occurrences": len(occurrences),
            "registered_symbol_occurrences": registered_occurrences,
            "interface_count": len(interfaces),
            "source_catalog_counts": dict(sorted(source_counts.items())),
            "special_symbol_occurrences": special_occurrences,
            "unregistered_symbol_occurrences": unregistered_occurrences,
            "dynamic_symbol_occurrences": len(dynamic),
            "tests_without_unique_symbol": len(tests_without_property),
            "tests_with_multiple_unique_symbols": len(duplicate_property_tests),
        },
        "interfaces": interfaces,
        "excluded": {
            "special_symbols": [
                {
                    "value": marker,
                    "occurrences": len(items),
                    "evidence": [item.evidence() for item in items],
                }
                for marker, items in sorted(special.items())
            ],
            "unregistered_symbols": [
                {
                    "value": symbol,
                    "occurrences": len(items),
                    "evidence": [item.evidence() for item in items],
                }
                for symbol, items in sorted(unregistered.items())
            ],
            "dynamic_symbols": [
                {**item.evidence(), "expression": item.expression} for item in dynamic
            ],
            "tests_without_unique_symbol": tests_without_property,
            "tests_with_multiple_unique_symbols": duplicate_property_tests,
        },
    }


def write_catalog(catalog: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _build_interface(
    module: str,
    symbol: str,
    evidence: list[PropertyOccurrence],
    metadata_rows: list[dict[str, str]],
) -> dict[str, Any]:
    ordered_metadata = sorted(
        metadata_rows,
        key=lambda item: (
            item["source_catalog"],
            item["acis_header"],
            item["kind"],
            item["parent"],
        ),
    )
    primary = ordered_metadata[0]
    target_counts = Counter((item.file, item.suite) for item in evidence)
    targets = [
        {"file": file, "suite": suite, "test_count": count}
        for (file, suite), count in sorted(target_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    target = targets[0] if targets else {"file": "", "suite": "", "test_count": 0}
    name = _symbol_name(symbol)
    digest = sha1(symbol.encode("utf-8")).hexdigest()[:10]
    return {
        "id": f"{module}.{_slug(name)}.{digest}",
        "name": name,
        "unique_symbol": symbol,
        "source_catalog": primary["source_catalog"],
        "source_catalogs": sorted({item["source_catalog"] for item in ordered_metadata}),
        "acis_header": primary["acis_header"],
        "catalog_module": primary["catalog_module"],
        "kind": primary["kind"],
        "parent": primary["parent"],
        "target_file": target["file"],
        "test_suite": target["suite"],
        "existing_test_count": len(evidence),
        "target_candidates": targets,
        "evidence": [item.evidence() for item in evidence],
    }


def _iter_test_blocks(text: str) -> Iterable[TestBlock]:
    masked = _mask_cpp_comments(text)
    pattern = re.compile(r"(?m)^[ \t]*(?:TEST|TEST_F|TEST_P)\s*\(")
    starts = list(pattern.finditer(masked))
    for index, match in enumerate(starts):
        open_paren = masked.find("(", match.start())
        close_paren = _find_matching_delimiter(masked, open_paren, "(", ")")
        if close_paren < 0:
            continue
        args = _split_top_level(text[open_paren + 1 : close_paren])
        if len(args) < 2:
            continue
        next_start = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        brace = masked.find("{", close_paren, next_start)
        if brace < 0:
            continue
        close_brace = _find_matching_delimiter(masked, brace, "{", "}")
        end = close_brace + 1 if close_brace >= 0 else next_start
        yield TestBlock(args[0].strip(), args[1].strip(), match.start(), end)


def _iter_unique_symbol_properties(
    text: str,
    relative_file: str,
    blocks: list[TestBlock],
) -> Iterable[PropertyOccurrence]:
    masked = _mask_cpp_comments(text)
    pattern = re.compile(r"\bRecordProperty\s*\(")
    for match in pattern.finditer(masked):
        open_paren = masked.find("(", match.start())
        close_paren = _find_matching_delimiter(masked, open_paren, "(", ")")
        if close_paren < 0:
            continue
        args = _split_top_level(text[open_paren + 1 : close_paren])
        if len(args) < 2 or _cpp_string_value(args[0]) != "UniqueSymbol":
            continue
        block = next((item for item in blocks if item.start <= match.start() < item.end), None)
        expression = args[1].strip()
        yield PropertyOccurrence(
            file=relative_file,
            suite=block.suite if block else "",
            test=block.name if block else "",
            line=text.count("\n", 0, match.start()) + 1,
            offset=match.start(),
            value=_cpp_string_value(expression),
            expression=expression,
        )


def _mask_cpp_comments(text: str) -> str:
    chars = list(text)
    index = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            else:
                chars[index] = " "
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                chars[index] = " "
                chars[index + 1] = " "
                block_comment = False
                index += 2
            else:
                if char != "\n":
                    chars[index] = " "
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "/" and next_char == "/":
            chars[index] = " "
            chars[index + 1] = " "
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            chars[index] = " "
            chars[index + 1] = " "
            block_comment = True
            index += 2
            continue
        index += 1
    return "".join(chars)


def _cpp_string_value(expression: str) -> str | None:
    value = expression.strip()
    pattern = re.compile(r'(?:u8|u|U|L)?"((?:\\.|[^"\\])*)"', re.DOTALL)
    position = 0
    parts: list[str] = []
    for match in pattern.finditer(value):
        if value[position : match.start()].strip():
            return None
        parts.append(_decode_cpp_string(match.group(1)))
        position = match.end()
    if not parts or value[position:].strip():
        return None
    return "".join(parts)


def _decode_cpp_string(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def _split_top_level(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    closing = {")": "(", "]": "[", "}": "{", ">": "<"}
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in depths:
            depths[char] += 1
        elif char in closing and depths[closing[char]]:
            depths[closing[char]] -= 1
        elif char == "," and not any(depths.values()):
            items.append(value[start:index].strip())
            start = index + 1
    items.append(value[start:].strip())
    return items


def _find_matching_delimiter(text: str, start: int, opening: str, closing: str) -> int:
    if start < 0 or start >= len(text) or text[start] != opening:
        return -1
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _symbol_name(symbol: str) -> str:
    open_paren = symbol.find("(")
    if open_paren < 0:
        return symbol.strip()
    prefix = symbol[:open_paren].rstrip()
    match = re.search(
        r"(?P<name>(?:(?:[A-Za-z_~]\w*)::)*(?:operator\s*(?:\[\]|\(\)|[^\s]+)|[A-Za-z_~]\w*))\s*$",
        prefix,
    )
    return re.sub(r"\s+", "", match.group("name")) if match else prefix


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "symbol"


def _module_name(value: str) -> str:
    module = value.strip()
    if not module or not re.fullmatch(r"[A-Za-z0-9_-]+", module):
        raise ValueError(f"Invalid module name: {value!r}")
    return module


def _discover_modules(root: Path) -> list[str]:
    source_root = root / "tests" / "gme" / "src"
    if not source_root.is_dir():
        return []
    return sorted(
        path.name
        for path in source_root.iterdir()
        if path.is_dir() and any(
            candidate.is_file() and candidate.suffix.lower() in SOURCE_EXTENSIONS
            for candidate in path.rglob("*")
        )
    )


def _resolve_catalog_directory(path: Path) -> Path:
    if not path.is_dir():
        raise FileNotFoundError(f"acis_symbol directory does not exist: {path}")
    if any(path.glob("*.csv")):
        return path
    nested = path / "acis_symbol"
    if nested.is_dir() and any(nested.glob("*.csv")):
        return nested
    return path


def _git_revision(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()
