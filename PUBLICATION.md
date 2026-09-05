# Qualified publication workflow

`publication/` is separate from `arc_release/`: adding a release card or registry
receipt does not change the already qualified runtime import closure. Never edit
an existing freeze or wrapper. The commands below require new output paths.

1. Build a clean freeze and admit all six local Docker trials with `arc_release`.
2. `python3.12 -m publication.bundle harbor NEW_WRAPPER --frozen FROZEN --local-job LOCAL_JOB`.
   This independently regrades every snapshot and creates dataset-level files
   binding the exact six package digests. Workstation paths are omitted publicly.
3. Using Harbor 0.21.0's Python, run
   `python -m publication.registry publish FROZEN LOCAL_JOB NEW_WRAPPER --output NEW_REGISTRY_IDENTITY.json`.
   It preflights all seven names, publishes serially and verifies actual tags,
   metadata, file membership/sizes/digests and immutable resolutions. Existing
   names must already match exactly; unexpected tags/content are never moved.
   The dataset publisher API avoids the CLI's manifest-rewriting sync step.
4. Run **all six**, with no filters/retries, using the returned dataset digest:
   `harbor run -d blobfishai/arc-crm-6@sha256:DIGEST -a oracle -e docker -n 1 -k 1 -r 0 -o JOBS --job-name NEW_JOB`.
   Admit with `python3.12 -m arc_release.receipts FROZEN REGISTRY_JOB --registry --output NEW_RECEIPT.json`.
5. `python3.12 -m publication.bundle hf NEW_HF_WRAPPER --frozen FROZEN --local-job LOCAL_JOB --registry-job REGISTRY_JOB --harbor NEW_WRAPPER --registry-receipt NEW_REGISTRY_IDENTITY.json`.
   The HF wrapper includes all frozen bytes, six flat dataset-viewer rows,
   source/qualification files, registry metadata and an allowlisted set of all
   local/registry oracle traces, verdicts, isolation proofs and SQLite snapshots.
   It never exports private world credentials, host configurations or workspaces.
6. In an isolated environment with `huggingface-hub`, run
   `python -m publication.hf publish NEW_HF_WRAPPER --output NEW_HF_IDENTITY.json`.
   Only the fixed `SamuelChien821/arc-crm-6` dataset is in scope. The uploader
   creates a public repository, uses optimistic parent-commit concurrency, and
   verifies every Git/LFS object at the returned immutable commit. Existing
   nonempty repositories must already match exactly; no overwrite/delete occurs.
7. Use these exact pins/evidence for website samples and links. Keep model ranks
   empty until an actual, separately authorized model evaluation is complete.

`publication.registry verify` and `publication.hf verify --revision COMMIT`
perform read-only remote admission; output receipts must remain outside input
wrappers. `publication.bundle verify WRAPPER` checks the exact local inventory.

The original frozen candidate manifest is retained as historical input. Harbor's
dataset card describes qualification available at publication time; later
registry-run and HF-object receipts are separate documents, never retroactive
changes to that digest. Six workflows are partial Arc-inspired coverage, not
reproductions of 1,200 source rows or adaptations of the full dataset catalog.

## Registry identifier is separate from client content hashing

The first Arc publication returned registry identifier `sha256:4d3737ff3fb16fc8f9b4ec54f75c19021435973fd624ad27ee54e88e459173db`,
while Harbor 0.21.0's client manifest algorithm computed `sha256:9a003b64f3ed10e090f3ae77a7bd9fa6a1dcff44061cc468af81a108d8a1ae75`.
All six task names/digests and all five dataset file paths/sizes/SHA256s match.
The server SQL is not in the inspected public source; its cause is not established.
Do not describe the registry identifier as a locally recomputed content hash.

The receipt preserves both values and an explicit mismatch flag. Admission still
requires every task config, instruction, README, metadata and file; all dataset
members and metadata; public non-yanked tags; and byte-identical metadata from a
second resolution by the immutable registry identifier. The all-six registry
execution separately verifies downloaded package digests and regrades snapshots.
No remote content or tag is modified to conceal a client/registry discrepancy.

Primary client source: [manifest algorithm](https://github.com/harbor-framework/harbor/blob/5c364a538e0af19eb58a53fdb895d7c0f974cef5/src/harbor/models/dataset/manifest.py)
and [publisher RPC usage](https://github.com/harbor-framework/harbor/blob/5c364a538e0af19eb58a53fdb895d7c0f974cef5/src/harbor/publisher/publisher.py).
