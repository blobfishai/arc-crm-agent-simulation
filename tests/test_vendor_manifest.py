import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_vendored_file_matches_the_merged_first_party_pin():
    manifest = json.loads((ROOT / "vendor-manifest.json").read_text())
    assert manifest["source_commit"] == "a40865c97a5f0e0ba39c8e84a98cf1d448546b1f"
    assert len(manifest["files"]) == 24
    for file in manifest["files"]:
        raw = (ROOT / file["path"]).read_bytes()
        assert len(raw) == file["bytes"]
        assert hashlib.sha256(raw).hexdigest() == file["sha256"]
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == file["git_blob"]
