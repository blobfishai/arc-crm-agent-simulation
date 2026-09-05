# Blobfish Arc CRM — independent six-workflow adaptation

Version 0.1.0 is a local candidate, not yet a published dataset or a model result.
It is separate from the already published HubBench v1.4.0 distribution.

The pinned inspiration is
[Arc-Intelligence/arc-crm-benchmark](https://huggingface.co/datasets/Arc-Intelligence/arc-crm-benchmark/tree/8efb61f5920812d03d6267ed25f4439acb82cfbf).
The source lock preserves the card/data hashes, access/license observations and
the review of all 1,200 conversations. All final-state fields in that snapshot
were empty. Our records, permission rules, tool schemas, executable outcomes,
and failure controls are independently authored. No upstream conversation,
initial entity, or code is copied or executed. The dataset card's MIT label is
not a license assertion about the redirected upstream code repository.

## Exact scope

Six synthetic tasks cover contact onboarding with duplicate resolution,
cross-turn stage correction, approved-discount quote drafting, cancellation and
replacement with retained history, unsigned contract drafting, and a corrected
document association. These are six distinct workflows, not 1,200 adapted rows.
They use 31 family tool contracts on three mock servers, plus two reused engine
controls. Tool mapping records distinguish 16 observed upstream names behind
15 independently authored contracts from Blobfish-only extensions. This is not
upstream API parity or coverage of all 27 observed names.

Every world has its own synthetic organization, conflicting requests, operative
and retired policy assets, explicit target/operation grants and protected decoy
records. SQLite foreign keys, enums, uniqueness and transactional handlers
enforce state. Quotes use current SKU prices and integer minor-unit arithmetic
with invoice-level half-up rounding. New contracts remain unsigned. Document
links resolve only to seeded `mock://` objects; no external request occurs.

## Run locally (Python 3.12+, standard library only)

From the monorepo root, choose a new session directory whose parent exists:

```sh
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.runtime serve \
  --task arc-crm-003 --session /tmp/arc-crm-example --port 8766
```

Open `http://127.0.0.1:8766/` for executable HTML forms and record views.
The catalog is `/api/v1/tools`; call `POST /api/v1/tools/<name>`, or JSON-RPC
`tools/call` at `/mcp` and `/mcp/<server>`. Use the remote CLI against that same
world:

```sh
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.runtime tool \
  --url http://127.0.0.1:8766 list
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.runtime tool \
  --url http://127.0.0.1:8766 hubbench.context.get '{}'
```

`tool --task ID --session DIR NAME '{...}'` and `stdio --task ID --session DIR`
also reopen the exact durable session. `verify` is a local privileged authoring
command, never an agent tool. Session bindings include the task, version and
implementation hashes; unowned, partial, symlinked or mismatched sessions are
rejected. There is no implicit reset or overwrite. This runtime is an authoring
environment, not a sandbox against a local user who can read the repository.

The reusable HTTP/CLI/MCP implementation and HubScore verifier are the merged
first-party HubBench engine. Its HTML/API/MCP presentation still says HubBench;
the task instruction and this adapter's manifests explicitly disclose the reuse.
No import registry is patched and no family is added to `hubbench/families`.

## Verification and limitations

```sh
python3.12 -m pytest benchmark/dataset_factory/tests/test_arc_crm.py \
  benchmark/dataset_factory/tests/test_arc_crm_surfaces.py -x -q
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.release qualify
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.release freeze \
  --path /tmp/arc-crm-candidate
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.release verify \
  --path /tmp/arc-crm-candidate
```

Qualification checks all six oracles at 100, byte-identical replay, independently
worded/reordered valid episodes, skipped investigations, omitted mutations,
missing readbacks, stale evidence, wrong answers, wrong targets and invalid
provider writes. The verifier checks every seeded row and every domain-table
count in addition to the engine's audited-table containment. Handoff grading is
structural plus disclosed literal-fact matching, **not** semantic consistency or
an LLM judgment. It permits alternate wording, but cannot detect a negation that
retains every required literal fact. Direct filesystem reads are not tool traces.

Frozen candidates contain public task briefs, assets, schemas and oracle
trajectories, plus separately labeled `sealed/` authoring/verifier contracts.
Never put `sealed/`, task builders, solutions or qualification reports in an
agent image. Asset metadata hashes the extracted text; the release manifest
separately hashes each actual PDF/XLSX/file byte sequence. Freeze never overwrites.

In-process oracle qualification does not establish model performance, Docker
isolation, registry availability or publication. Those require separate tests
and immutable receipts. No model is ranked and no paid model job is launched.
The wider source catalog remains metadata classification, not completed
source-by-source adaptation.
