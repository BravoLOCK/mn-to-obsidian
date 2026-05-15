#!/usr/bin/env python3
"""
MarginNote study set backup (.marginpkg) to Obsidian markdown exporter.

This script is intentionally defensive:
- it first unpacks and inspects the backup structure
- it looks for likely note metadata sources (SQLite / JSON / plist / XML)
- it collects image assets and prepares an Obsidian-friendly output layout

The exact data mapping varies across MarginNote backup variants, so the script
includes a discovery mode that becomes the basis for format-specific export.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import sqlite3
import sys
import textwrap
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
import xml.etree.ElementTree as ET


IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

DATA_EXTENSIONS = {
    ".sqlite",
    ".db",
    ".json",
    ".plist",
    ".xml",
}


@dataclass
class PackageSummary:
    package_path: Path
    unpack_dir: Path
    all_files: list[Path] = field(default_factory=list)
    image_files: list[Path] = field(default_factory=list)
    data_files: list[Path] = field(default_factory=list)
    sqlite_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    json_samples: dict[str, Any] = field(default_factory=dict)
    plist_samples: dict[str, Any] = field(default_factory=dict)
    xml_roots: dict[str, str] = field(default_factory=dict)


@dataclass
class NoteRecord:
    note_id: str
    title: str | None
    highlight_text: str | None
    notes_text: str | None
    media_list: str | None
    start_page: int | None
    end_page: int | None
    zindex: int | None
    mindlinks: list[str]
    note_date: float | None
    note_type: int | None


def sanitize_filename(value: str, fallback: str = "note") -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid else ch for ch in value).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or fallback


def derive_effective_name(package_path: Path) -> str:
    stem = package_path.stem
    cleaned = re.sub(r"\(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\)\s*$", "", stem).strip()
    return sanitize_filename(cleaned or stem, "MarginNote Export")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def unpack_marginpkg(package_path: Path, unpack_dir: Path) -> None:
    if unpack_dir.exists():
        shutil.rmtree(unpack_dir)
    ensure_dir(unpack_dir)

    if zipfile.is_zipfile(package_path):
        with zipfile.ZipFile(package_path, "r") as archive:
            archive.extractall(unpack_dir)
        return

    raise ValueError(
        f"{package_path.name} is not a ZIP-compatible marginpkg backup. "
        "If this file comes from MarginNote, please send a sample so we can "
        "add the right unpacking logic."
    )


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def sniff_sqlite(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"tables": []}
    try:
        with sqlite3.connect(path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row[0] for row in cursor.fetchall()]
            result["tables"] = tables
            previews: dict[str, Any] = {}
            for table in tables[:10]:
                try:
                    col_cursor = conn.execute(f'PRAGMA table_info("{table}")')
                    columns = [row[1] for row in col_cursor.fetchall()]
                    row_cursor = conn.execute(f'SELECT * FROM "{table}" LIMIT 1')
                    sample_row = row_cursor.fetchone()
                    previews[table] = {
                        "columns": columns,
                        "sample_row": sample_row,
                    }
                except sqlite3.DatabaseError as exc:
                    previews[table] = {"error": str(exc)}
            result["preview"] = previews
    except sqlite3.DatabaseError as exc:
        result["error"] = str(exc)
    return result


def sniff_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return {"type": "dict", "keys": list(data.keys())[:20]}
    if isinstance(data, list):
        return {"type": "list", "length": len(data), "first_item": data[0] if data else None}
    return {"type": type(data).__name__, "value": data}


def sniff_plist(path: Path) -> Any:
    with path.open("rb") as handle:
        data = plistlib.load(handle)
    if isinstance(data, dict):
        return {"type": "dict", "keys": list(data.keys())[:20]}
    if isinstance(data, list):
        return {"type": "list", "length": len(data)}
    return {"type": type(data).__name__}


def sniff_xml(path: Path) -> str:
    tree = ET.parse(path)
    return tree.getroot().tag


def markdown_to_plain_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\r\n", "\n").strip()
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`>#-]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_media_ids(media_list: str | None) -> list[str]:
    if not media_list:
        return []
    return [part for part in media_list.split("-") if part]


def decode_media_blob(blob: bytes) -> bytes:
    payload = plistlib.loads(blob)
    if isinstance(payload, dict):
        objects = payload.get("$objects", [])
        if len(objects) > 1:
            candidate = objects[1]
            if isinstance(candidate, bytes):
                return candidate
            if isinstance(candidate, dict):
                ns_data = candidate.get("NS.data")
                if isinstance(ns_data, bytes):
                    return ns_data
    if isinstance(payload, bytes):
        return payload
    raise ValueError("unsupported media blob format")


def guess_image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".bin"


def is_probably_image(data: bytes) -> bool:
    return guess_image_extension(data) != ".bin"


def get_png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    return None


def fetch_note_records(db_path: Path) -> dict[str, NoteRecord]:
    notes: dict[str, NoteRecord] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                ZNOTEID,
                ZNOTETITLE,
                ZHIGHLIGHT_TEXT,
                ZNOTES_TEXT,
                ZMEDIA_LIST,
                ZSTARTPAGE,
                ZENDPAGE,
                ZZINDEX,
                ZMINDLINKS,
                ZNOTE_DATE,
                ZTYPE
            FROM ZBOOKNOTE
            """
        ).fetchall()

    for row in rows:
        note_id = row["ZNOTEID"]
        links = []
        if row["ZMINDLINKS"]:
            links = [part for part in str(row["ZMINDLINKS"]).split("|") if part]
        notes[note_id] = NoteRecord(
            note_id=note_id,
            title=row["ZNOTETITLE"],
            highlight_text=row["ZHIGHLIGHT_TEXT"],
            notes_text=row["ZNOTES_TEXT"],
            media_list=row["ZMEDIA_LIST"],
            start_page=row["ZSTARTPAGE"],
            end_page=row["ZENDPAGE"],
            zindex=row["ZZINDEX"],
            mindlinks=links,
            note_date=row["ZNOTE_DATE"],
            note_type=row["ZTYPE"],
        )
    return notes


def fetch_media_map(db_path: Path) -> dict[str, bytes]:
    media_map: dict[str, bytes] = {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT ZMD5, ZDATA FROM ZMEDIA").fetchall()
    for md5, blob in rows:
        media_map[md5] = decode_media_blob(blob)
    return media_map


def find_marginnotes_db(summary: PackageSummary) -> Path | None:
    for file_path in summary.all_files:
        if file_path.suffix.lower() == ".marginnotes":
            return file_path
    return None


def find_root_note(notes: dict[str, NoteRecord]) -> NoteRecord | None:
    type256 = [note for note in notes.values() if note.note_type == 256]
    if not type256:
        return None
    child_ids = {child for note in type256 for child in note.mindlinks}
    roots = [note for note in type256 if note.note_id not in child_ids]
    if roots:
        return sorted(
            roots,
            key=lambda note: (
                note.zindex if note.zindex is not None else 0,
                note.start_page if note.start_page is not None else -1,
                note.note_id,
            ),
        )[0]
    return None


def collect_exported_note_ids(root: NoteRecord, notes: dict[str, NoteRecord]) -> set[str]:
    visited: set[str] = set()
    stack = [root.note_id]

    while stack:
        note_id = stack.pop()
        if note_id in visited:
            continue
        visited.add(note_id)
        note = notes.get(note_id)
        if note:
            stack.extend(note.mindlinks)

    return visited


def select_note_media_ids(note: NoteRecord, media_map: dict[str, bytes]) -> list[str]:
    media_ids = parse_media_ids(note.media_list)
    if not media_ids:
        return []

    selected: list[str] = []
    grouped_pngs: dict[tuple[int, int], list[tuple[str, int]]] = {}

    for media_id in media_ids:
        data = media_map.get(media_id)
        if data is None or not is_probably_image(data):
            continue
        dims = get_png_dimensions(data)
        if dims is None:
            selected.append(media_id)
            continue
        grouped_pngs.setdefault(dims, []).append((media_id, len(data)))

    for _, candidates in grouped_pngs.items():
        candidates.sort(key=lambda item: item[1], reverse=True)
        selected.append(candidates[0][0])

    return selected


def export_note_tree_markdown(
    root: NoteRecord,
    notes: dict[str, NoteRecord],
    media_paths: dict[str, str],
    note_media_ids: dict[str, list[str]],
) -> tuple[str, int]:
    lines: list[str] = []
    visited: set[str] = set()

    def render(note: NoteRecord, depth: int) -> None:
        if note.note_id in visited:
            return
        visited.add(note.note_id)

        heading_level = min(depth + 1, 6)
        title = markdown_to_plain_text(note.title) or "Untitled"
        lines.append(f"{'#' * heading_level} {title}")
        lines.append("")

        body_parts: list[str] = []
        title_plain = markdown_to_plain_text(note.title)
        highlight_plain = markdown_to_plain_text(note.highlight_text)
        notes_plain = markdown_to_plain_text(note.notes_text)
        chosen_media_ids = note_media_ids.get(note.note_id, [])
        has_image_media = any(media_id in media_paths for media_id in chosen_media_ids)

        # MarginNote often stores OCR'ed image text in highlight_text for image notes.
        # When a note has exported images, keep the image and suppress the OCR body.
        if not has_image_media and highlight_plain and highlight_plain != title_plain:
            body_parts.append(highlight_plain)
        if notes_plain and notes_plain not in body_parts:
            body_parts.append(notes_plain)

        for paragraph in body_parts:
            lines.append(paragraph)
            lines.append("")

        for media_id in chosen_media_ids:
            rel_path = media_paths.get(media_id)
            if rel_path:
                lines.append(f"![[{rel_path}]]")
                lines.append("")

        for child_id in note.mindlinks:
            child = notes.get(child_id)
            if child:
                render(child, depth + 1)

    render(root, 0)
    return "\n".join(lines).strip() + "\n", len(visited)


def inspect_package(package_path: Path, unpack_dir: Path) -> PackageSummary:
    unpack_marginpkg(package_path, unpack_dir)

    summary = PackageSummary(package_path=package_path, unpack_dir=unpack_dir)

    for file_path in iter_files(unpack_dir):
        summary.all_files.append(file_path)
        suffix = file_path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            summary.image_files.append(file_path)
        if suffix in DATA_EXTENSIONS:
            summary.data_files.append(file_path)

    for data_file in summary.data_files:
        rel = data_file.relative_to(unpack_dir).as_posix()
        suffix = data_file.suffix.lower()
        try:
            if suffix in {".sqlite", ".db"}:
                summary.sqlite_summaries[rel] = sniff_sqlite(data_file)
            elif suffix == ".json":
                summary.json_samples[rel] = sniff_json(data_file)
            elif suffix == ".plist":
                summary.plist_samples[rel] = sniff_plist(data_file)
            elif suffix == ".xml":
                summary.xml_roots[rel] = sniff_xml(data_file)
        except Exception as exc:  # noqa: BLE001
            marker = {"error": str(exc)}
            if suffix in {".sqlite", ".db"}:
                summary.sqlite_summaries[rel] = marker
            elif suffix == ".json":
                summary.json_samples[rel] = marker
            elif suffix == ".plist":
                summary.plist_samples[rel] = marker
            elif suffix == ".xml":
                summary.xml_roots[rel] = str(marker)

    return summary


def write_inspection_report(summary: PackageSummary, report_path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# MarginNote Package Inspection: {summary.package_path.name}")
    lines.append("")
    lines.append(f"- Unpacked to: `{summary.unpack_dir}`")
    lines.append(f"- Files found: `{len(summary.all_files)}`")
    lines.append(f"- Images found: `{len(summary.image_files)}`")
    lines.append(f"- Data files found: `{len(summary.data_files)}`")
    lines.append("")

    if summary.image_files:
        lines.append("## Images")
        for path in summary.image_files[:100]:
            lines.append(f"- `{path.relative_to(summary.unpack_dir).as_posix()}`")
        if len(summary.image_files) > 100:
            lines.append(f"- ... and {len(summary.image_files) - 100} more")
        lines.append("")

    if summary.sqlite_summaries:
        lines.append("## SQLite")
        for rel, data in summary.sqlite_summaries.items():
            lines.append(f"### `{rel}`")
            if "error" in data:
                lines.append(f"- Error: `{data['error']}`")
                continue
            tables = data.get("tables", [])
            lines.append(f"- Tables: `{', '.join(tables) if tables else '(none)'}`")
            preview = data.get("preview", {})
            for table, table_info in list(preview.items())[:5]:
                lines.append(f"- `{table}` columns: `{', '.join(table_info.get('columns', []))}`")
            lines.append("")

    if summary.json_samples:
        lines.append("## JSON")
        for rel, data in summary.json_samples.items():
            lines.append(f"- `{rel}`: `{json.dumps(data, ensure_ascii=False)[:300]}`")
        lines.append("")

    if summary.plist_samples:
        lines.append("## Plist")
        for rel, data in summary.plist_samples.items():
            lines.append(f"- `{rel}`: `{json.dumps(data, ensure_ascii=False)[:300]}`")
        lines.append("")

    if summary.xml_roots:
        lines.append("## XML")
        for rel, data in summary.xml_roots.items():
            lines.append(f"- `{rel}` root: `{data}`")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def export_stub(summary: PackageSummary, output_dir: Path) -> None:
    """
    Create an Obsidian-friendly placeholder export.

    Before the real note mapping is implemented for a concrete package sample,
    this still provides:
    - copied image attachments
    - a manifest note with structure details
    """
    ensure_dir(output_dir)
    attachments_dir = output_dir / "attachments"
    ensure_dir(attachments_dir)

    for image_path in summary.image_files:
        target = attachments_dir / sanitize_filename(image_path.name, "image")
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            index = 2
            while True:
                candidate = attachments_dir / f"{stem}-{index}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                index += 1
        shutil.copy2(image_path, target)

    manifest = output_dir / "MarginNote Import Preview.md"
    body = textwrap.dedent(
        f"""\
        # MarginNote Import Preview

        这个导出器已经完成了以下工作：

        - 解包 `marginpkg`
        - 收集图片资源到 `attachments/`
        - 生成结构探测报告，帮助我们适配真实笔记内容

        下一步：

        1. 查看同目录下的 `inspection_report.md`
        2. 根据你提供的真实 `marginpkg` 内部结构，补上正文、标题层级和图片嵌入逻辑

        当前统计：

        - 原包文件数：{len(summary.all_files)}
        - 图片数：{len(summary.image_files)}
        - 数据文件数：{len(summary.data_files)}
        """
    )
    manifest.write_text(body, encoding="utf-8")


def export_obsidian(summary: PackageSummary, output_dir: Path) -> None:
    db_path = find_marginnotes_db(summary)
    if db_path is None:
        export_stub(summary, output_dir)
        return

    notes = fetch_note_records(db_path)
    media_map = fetch_media_map(db_path)
    root = find_root_note(notes)
    if root is None:
        export_stub(summary, output_dir)
        return

    if output_dir.exists():
        shutil.rmtree(output_dir)
    ensure_dir(output_dir)

    effective_name = derive_effective_name(summary.package_path)
    attachment_dir_name = effective_name
    attachments_dir = output_dir / attachment_dir_name
    ensure_dir(attachments_dir)

    exported_note_ids = collect_exported_note_ids(root, notes)
    exported_media_ids: set[str] = set()
    note_media_ids: dict[str, list[str]] = {}
    for note_id in exported_note_ids:
        note = notes.get(note_id)
        if note:
            chosen_media_ids = select_note_media_ids(note, media_map)
            note_media_ids[note_id] = chosen_media_ids
            exported_media_ids.update(chosen_media_ids)

    media_paths: dict[str, str] = {}
    for media_id in sorted(exported_media_ids):
        data = media_map.get(media_id)
        if data is None:
            continue
        if not is_probably_image(data):
            continue
        ext = guess_image_extension(data)
        filename = sanitize_filename(media_id, "asset") + ext
        target = attachments_dir / filename
        target.write_bytes(data)
        media_paths[media_id] = f"{attachment_dir_name}/{filename}"

    markdown, exported_note_count = export_note_tree_markdown(root, notes, media_paths, note_media_ids)
    note_filename = effective_name + ".md"
    (output_dir / note_filename).write_text(markdown, encoding="utf-8")

    report_lines = [
        "# Export Summary",
        "",
        f"- Root note: `{root.title}`",
        f"- Exported notes: `{exported_note_count}`",
        f"- Total notes in database: `{len(notes)}`",
        f"- Media exported: `{len(media_paths)}`",
        f"- Export folder: `{output_dir.name}`",
        f"- Markdown file: `{note_filename}`",
        f"- Attachment folder: `{attachment_dir_name}`",
    ]
    (output_dir / "export_summary.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert MarginNote .marginpkg backups into Obsidian markdown."
    )
    parser.add_argument("package", type=Path, help="Path to the .marginpkg file")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("build"),
        help="Working directory for unpacked files and reports",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Directory for generated export folder; default is a sibling folder named after the package's effective title",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Only unpack and inspect the package without creating the export stub",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    package_path = args.package.resolve()
    if not package_path.exists():
        parser.error(f"package not found: {package_path}")

    workspace = args.workspace.resolve()
    effective_name = derive_effective_name(package_path)
    output = args.output.resolve() if args.output else (package_path.parent / effective_name)
    unpack_dir = workspace / "unpacked"
    ensure_dir(workspace)

    summary = inspect_package(package_path, unpack_dir)
    report_path = workspace / "inspection_report.md"
    write_inspection_report(summary, report_path)

    if not args.inspect_only:
        export_obsidian(summary, output)

    print(f"Inspection report: {report_path}")
    if not args.inspect_only:
        print(f"Obsidian export stub: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
