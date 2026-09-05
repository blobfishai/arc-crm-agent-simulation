# Blobfish Arc CRM

An independently authored, source-locked CRM benchmark world inspired by
[Arc CRM](https://huggingface.co/datasets/Arc-Intelligence/arc-crm-benchmark/tree/8efb61f5920812d03d6267ed25f4439acb82cfbf).
This is not the upstream Arc codebase, upstream API parity, or a model leaderboard.

Six synthetic workflows cover contact onboarding, stage correction, bounded
quote drafting, quote replacement with preserved history, unsigned contract
drafting, and corrected document association. They share persistent SQLite
state through 31 CRM/evidence contracts on three mock servers, plus two engine
controls. All CLI, HTML-form, REST and MCP calls execute the same tools.

This repository contains the tested **local authoring runtime** and an isolated
package exporter. Hugging Face/Harbor publication is not yet claimed. No source
conversation or code was copied from Arc; all task fixtures and final-state
checks are independently authored. The reviewed source has 1,200 conversations
and 27 tool names; this version reproduces zero source rows and covers only the
six listed workflows.

## Isolated packages

`arc_release` exports six self-contained Harbor tasks without changing the
vendored authoring implementation. Use a new destination for every freeze:

```sh
python3.12 -m arc_release.build freeze /tmp/arc-crm-packages
python3.12 -m arc_release.build verify /tmp/arc-crm-packages
harbor run -p /tmp/arc-crm-packages/harbor/tasks -a oracle -e docker -n 1 -k 1 -r 0
```

Add `--require-clean` to `freeze` for a release candidate tied to a clean source
commit. Dirty development exports are labeled and cannot establish publication.
The immutable file manifest, six task digests and installed-Harbor hash
cross-check bind the exported inputs. A completed Harbor command or reward of
1 alone is not a qualification receipt: require every oracle's successful exit,
strict verifier verdict, canonical trace/state and frozen task identity.

The agent image contains only a stdlib remote client, public schemas and evidence.
An isolated world container has the seed state and handlers, but no task builder
or verifier. The agent UID is 10001; startup checks denied runtime paths,
private API access and external networking under that UID. A random per-episode
collection credential stays exclusively in the world container.

Harbor stops the agent before the world-side collection hook serializes a
consistent SQLite snapshot. It then destroys the agent environment and builds
a separate verifier from `tests/`, with networking disabled and no agent running
beside it. Host log mounts are not a security boundary: prior reward files are
cleared before fresh grading. Admission requires successful stop/collection
markers, service provenance, exact bundle hashes and independently regrades the
saved snapshot. The reference solution is uploaded only for oracle runs.

Each local Docker trial uses an internal-only network, a 1 CPU/1 GiB main
container and a 0.5 CPU/512 MiB world container with PID limits, followed by a
separate 1 CPU/1 GiB verifier. The guard checks actual cgroup-v2 limits. Docker does not
enforce Harbor's declared storage budget. No paid model, cloud deployment or
runtime dependency installation is needed. Model runners requiring installation
or provider egress need a separately reviewed runner configuration; this release
does not claim to have run them. Harbor's default cleanup removes its own trial
containers, networks and volumes, not source releases or other world data.

## Run

Python 3.12+ is required. Runtime and qualification need only the standard library.
From this checkout, choose a new session directory:

```sh
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.runtime serve \
  --task arc-crm-003 --session /tmp/arc-crm-demo --port 8766
```

Open `http://127.0.0.1:8766/` to inspect records and submit executable forms.
Call REST at `/api/v1/tools/<name>` or JSON-RPC MCP at `/mcp` and `/mcp/<server>`.
The CLI uses that same served world:

```sh
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.runtime tool \
  --url http://127.0.0.1:8766 hubbench.context.get '{}'
python3.12 -m benchmark.dataset_factory.adapters.arc_crm.release qualify
uv run --no-project --python 3.12 --with pytest python -m pytest -q
```

See the [adapter guide](benchmark/dataset_factory/adapters/arc_crm/README.md)
for schemas, safe session reuse, deterministic candidate exports, exact scope
and grading limitations. This checkout includes task builders and sealed
authoring material: it must not itself become an agent container image.

## Provenance and result labels

`vendor-manifest.json` records every byte-identical first-party source file
imported from merged Blobfish monorepo commit
`a40865c97a5f0e0ba39c8e84a98cf1d448546b1f`. The fixture source lock separately
records the Arc dataset revision and card/data hashes. Neither one substitutes
for a future frozen package digest or registry publication receipt.

The six in-process oracles score 100 and replay identically; alternate wording
and evidence order pass, and all 167 negative controls reject. These are
solvability and verifier checks, **not model performance**. The shared verifier
reports its original HubScore metric. HTTP/MCP presentation retains the
HubBench engine name, but this is not part of the published HubBench v1.4.0
distribution. See [engine attribution](benchmark/hubbench/NOTICE).

New source files and synthetic fixtures in this repository are released under
[Apache-2.0](LICENSE). The upstream dataset card's MIT label is retained only
as source metadata and is not a license claim about linked upstream repositories.
