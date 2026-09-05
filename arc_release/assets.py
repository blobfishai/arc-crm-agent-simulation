"""Lossless native release assets; logical text and binary hashes are distinct."""

from __future__ import annotations

import hashlib
import textwrap

from benchmark.hubbench.engine.assets import PDF
from benchmark.hubbench.engine.assets import asset_bytes as authoring_asset_bytes


def pdf_bytes(text: str) -> bytes:
    """Wrap and paginate ASCII evidence without silently clipping any characters.

    The synthetic fixtures are ASCII. Reject unsupported characters explicitly
    instead of replacing them in a font that cannot represent them.
    """
    text.encode("ascii")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise ValueError("unsupported control character in PDF evidence")
    lines = []
    for line in text.expandtabs(4).splitlines():
        lines.extend(textwrap.wrap(line, width=92, replace_whitespace=False,
                                   drop_whitespace=False, break_on_hyphens=False) or [""])
    pages = [lines[offset:offset + 48] for offset in range(0, len(lines), 48)] or [[""]]
    page_ids = [4 + 2 * index for index in range(len(pages))]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in page_ids)}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page_id, page in zip(page_ids, pages, strict=True):
        commands = ["BT", "/F1 10 Tf", "54 750 Td", "12 TL"]
        for index, line in enumerate(page):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if index:
                commands.append("T*")
            commands.append(f"({escaped}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        ])
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def asset_bytes(record: dict) -> bytes:
    content_digest = hashlib.sha256(record["content"].encode()).hexdigest()
    if record["sha256"] != content_digest:
        raise ValueError("logical asset text hash differs")
    return pdf_bytes(record["content"]) if record["media_type"] == PDF else authoring_asset_bytes(record)


def public_assets(records: list[dict], rendered: dict[str, bytes]) -> list[dict]:
    return [{key: value for key, value in record.items() if key not in {"content", "rows"}} | {
        "sha256_scope": "utf8-text-content",
        "content_sha256": record["sha256"],
        "file_sha256": hashlib.sha256(rendered[record["path"]]).hexdigest(),
        "file_bytes": len(rendered[record["path"]]),
    } for record in records]


def verify_asset(metadata: dict, raw: bytes, content: str) -> None:
    logical = hashlib.sha256(content.encode()).hexdigest()
    if (metadata.get("sha256_scope") != "utf8-text-content" or metadata.get("content_sha256") != logical
            or metadata.get("sha256") != logical or metadata.get("file_sha256") != hashlib.sha256(raw).hexdigest()
            or metadata.get("file_bytes") != len(raw)):
        raise ValueError("asset logical/binary identity differs")
    if metadata["media_type"] == PDF and raw != pdf_bytes(content):
        raise ValueError("native PDF does not contain the complete declared text")
