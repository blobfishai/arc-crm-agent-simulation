import hashlib
import io
import json
import re
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from pypdf import PdfReader
from test_packages import frozen  # noqa: F401

from arc_release.assets import asset_bytes, pdf_bytes, public_assets, verify_asset
from benchmark.dataset_factory.adapters.arc_crm import build_tasks
from benchmark.hubbench.engine.assets import PDF, XLSX
from benchmark.hubbench.engine.assets import asset_bytes as legacy_asset_bytes


def words(text):
    return re.sub(r"\s+", "", text)


def test_every_native_pdf_preserves_all_authority_and_document_facts(request):
    frozen_dir = request.getfixturevalue("frozen")
    checked = 0
    for task in build_tasks():
        for asset in task["assets"]:
            if asset["media_type"] != PDF:
                continue
            raw = (frozen_dir / "tasks" / task["task_id"] / asset["path"]).read_bytes()
            extracted = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(raw)).pages)
            assert words(extracted) == words(asset["content"]), asset["path"]
            assert raw == asset_bytes(asset)
            checked += 1
    assert checked == 15


def test_pdf_wraps_and_paginates_without_silent_loss():
    text = "()\\ " + "word " * 200 + "\n" + "\n".join(f"line {i}: final authority {i}" for i in range(101))
    raw = pdf_bytes(text)
    document = PdfReader(io.BytesIO(raw))
    assert len(document.pages) >= 3
    assert words("\n".join(page.extract_text() for page in document.pages)) == words(text)
    assert pdf_bytes(text) == raw
    with pytest.raises(UnicodeEncodeError):
        pdf_bytes("unrepresentable emoji: 🐡")
    with pytest.raises(ValueError, match="control character"):
        pdf_bytes("silently hidden\x00content")


def test_every_asset_exposes_distinct_logical_and_binary_identities(request):
    frozen_dir = request.getfixturevalue("frozen")
    checked = 0
    for task in build_tasks():
        public = json.loads((frozen_dir / "tasks" / task["task_id"] / "task.json").read_text())
        for asset, metadata in zip(task["assets"], public["assets"], strict=True):
            raw = (frozen_dir / "tasks" / task["task_id"] / asset["path"]).read_bytes()
            assert metadata["sha256_scope"] == "utf8-text-content"
            assert metadata["content_sha256"] == asset["sha256"] == hashlib.sha256(asset["content"].encode()).hexdigest()
            assert metadata["file_sha256"] == hashlib.sha256(raw).hexdigest()
            assert metadata["file_bytes"] == len(raw)
            if asset["media_type"] == XLSX:
                with ZipFile(io.BytesIO(raw)) as archive:
                    sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                rows = [["".join(cell.itertext()) for cell in row] for row in sheet.findall("s:sheetData/s:row", ns)]
                assert rows == [[str(value) for value in row] for row in asset["rows"]]
            checked += 1
    assert checked == 33


def test_asset_writer_rejects_an_unbound_logical_digest():
    asset = build_tasks()[0]["assets"][0] | {"sha256": "0" * 64}
    with pytest.raises(ValueError, match="logical asset text hash"):
        asset_bytes(asset)


def test_even_rehashed_truncated_pdf_cannot_pass_native_asset_admission():
    asset = build_tasks()[0]["assets"][-1]
    assert asset["media_type"] == PDF and max(map(len, asset["content"].splitlines())) > 92
    raw = legacy_asset_bytes(asset)
    metadata = public_assets([asset], {asset["path"]: raw})[0]
    with pytest.raises(ValueError, match="complete declared text"):
        verify_asset(metadata, raw, asset["content"])
    complete = asset_bytes(asset)
    verify_asset(public_assets([asset], {asset["path"]: complete})[0], complete, asset["content"])
