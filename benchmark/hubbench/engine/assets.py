"""Dependency-free evidence-file writers (XLSX, PDF, text) and asset records.

Adapted from the FactoryBench-100 release writer (Apache-2.0, BlobfishAI).
Every writer is deterministic so the release tree is diff-stable.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import zipfile
from pathlib import Path
from typing import Any

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"
MARKDOWN = "text/markdown"
CSV = "text/csv"
JSON = "application/json"
EML = "message/rfc822"
YAML = "application/yaml"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rows_to_csv(rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()


def asset(
    path: str,
    *,
    kind: str,
    title: str,
    source: str,
    media_type: str,
    content: str | None = None,
    rows: list[list[Any]] | None = None,
    preview: str = "",
) -> dict[str, Any]:
    """Build one evidence-file record.  Spreadsheets carry ``rows`` and a CSV rendering."""

    if media_type == XLSX:
        if rows is None:
            raise ValueError(f"{path}: spreadsheet assets need rows")
        content = rows_to_csv(rows)
    if content is None:
        raise ValueError(f"{path}: content is required")
    record: dict[str, Any] = {
        "path": path,
        "kind": kind,
        "title": title,
        "source": source,
        "media_type": media_type,
        "content": content,
        "preview": preview or title,
        "sha256": sha256_text(content),
    }
    if rows is not None:
        record["rows"] = rows
    return record


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _stable_zip_info(filename: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=filename, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def xlsx_bytes(rows: list[list[Any]], sheet_name: str = "Sheet1") -> bytes:
    """Standards-shaped one-sheet XLSX workbook as bytes."""

    sheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column_number, value in enumerate(row, start=1):
            reference = f"{_column_name(column_number)}{row_number}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{html.escape("" if value is None else str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            _stable_zip_info("[Content_Types].xml"),
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            _stable_zip_info("_rels/.rels"),
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            _stable_zip_info("xl/workbook.xml"),
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{html.escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            _stable_zip_info("xl/_rels/workbook.xml.rels"),
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr(_stable_zip_info("xl/worksheets/sheet1.xml"), sheet)
    return buffer.getvalue()


def pdf_bytes(text: str) -> bytes:
    """Small valid single-page PDF carrying the extracted text."""

    lines = [line[:92] for line in text.splitlines() if line.strip()][:48]
    commands = ["BT", "/F1 10 Tf", "54 750 Td", "12 TL"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            commands.append("T*")
        commands.append(f"({escaped}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def asset_bytes(record: dict[str, Any]) -> bytes:
    if record["media_type"] == XLSX:
        return xlsx_bytes(record["rows"])
    if record["media_type"] == PDF:
        return pdf_bytes(record["content"])
    return record["content"].encode("utf-8")


def write_asset(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(asset_bytes(record))


def eml(*, from_addr: str, to_addr: str, subject: str, date: str, message_id: str, body: str, attachments: list[str] | None = None) -> str:
    """Minimal RFC 822 message text; attachments are referenced by name."""

    headers = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: <{message_id}>",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if attachments:
        headers.append("X-Attachments: " + ", ".join(attachments))
    return "\n".join(headers) + "\n\n" + body.strip() + "\n"


def yaml_lines(value: Any, indent: int = 0) -> str:
    """Tiny YAML emitter for evidence indexes (scalars, lists, dicts only)."""

    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(yaml_lines(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {json.dumps(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                first = True
                for key, inner in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(inner, (dict, list)):
                        lines.append(f"{prefix}{key}:")
                        lines.append(yaml_lines(inner, indent + 2))
                    else:
                        lines.append(f"{prefix}{key}: {json.dumps(inner)}")
            else:
                lines.append(f"{pad}- {json.dumps(item)}")
        return "\n".join(lines)
    return f"{pad}{json.dumps(value)}"


__all__ = [
    "CSV",
    "EML",
    "JSON",
    "MARKDOWN",
    "PDF",
    "XLSX",
    "YAML",
    "asset",
    "asset_bytes",
    "eml",
    "pdf_bytes",
    "rows_to_csv",
    "sha256_text",
    "write_asset",
    "xlsx_bytes",
    "yaml_lines",
]
