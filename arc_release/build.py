"""Freeze six self-contained Harbor tasks with separate agent/world/verifier inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

from benchmark.dataset_factory.adapters.arc_crm import FAMILY, SOURCE_LOCK, build_tasks
from benchmark.dataset_factory.adapters.arc_crm.qualification import digest, qualify
from benchmark.dataset_factory.adapters.arc_crm.release import public_task
from benchmark.hubbench.engine.assets import asset_bytes
from benchmark.hubbench.engine.families import public_tool_definitions
from benchmark.hubbench.engine.validation import canonical_json

ROOT = Path(__file__).resolve().parents[1]
DATASET = "blobfishai/arc-crm-6"
VERSION = "0.1.0"
BASE_IMAGE = "python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17"
ENGINE = "benchmark/hubbench/engine"
MARKERS = [f"{ENGINE}/__init__.py"]
GENERATED_MARKERS = ["benchmark/__init__.py", "benchmark/hubbench/__init__.py"]
WORLD_ENGINE = ["families.py", "http.py", "server.py", "tasks.py", "validation.py", "world.py", "core.sql"]
VERIFIER_ENGINE = ["families.py", "validation.py", "world.py", "verifier.py", "core.sql"]
SEALED_KEYS = (
    "task_id", "family", "version", "role", "as_of", "answer_schema", "expected",
    "required_investigations", "post_write_verifications", "allowed_write_tables", "write_count", "rubric_milestones",
)


def json_bytes(value):
    return (canonical_json(value) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_relative(relative: str):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != relative or "\\" in relative:
        raise ValueError(f"unsafe release member: {relative}")
    return path


def content_hash(task: Path):
    """Harbor 0.21.0 file selection, independently computed and cross-tested.

    Custom ignore files and symlinks are prohibited in this distribution.
    .pyo is deliberately NOT ignored: Harbor's defaults include only .pyc.
    """
    if (task / ".gitignore").exists():
        raise ValueError("custom task ignores are not permitted")
    members = []
    for path in task.rglob("*"):
        relative = path.relative_to(task)
        if path.is_symlink():
            raise ValueError("symlinked Harbor member")
        if not path.is_file():
            continue
        if relative.as_posix() not in {"task.toml", "instruction.md", "README.md"} and relative.parts[0] not in {
            "environment", "tests", "solution", "steps"
        }:
            continue
        if "__pycache__" in relative.parts or path.name == ".DS_Store" or path.name.endswith((".pyc", ".swp", ".swo", "~")):
            continue
        members.append(relative.as_posix())
    members.sort()
    raw = "".join(f"{relative}\0{sha((task / relative).read_bytes())}\n" for relative in members).encode()
    return "sha256:" + sha(raw), members


def family_metadata():
    return {
        "family": {
            key: getattr(FAMILY, key)
            for key in ("slug", "name", "version", "cluster", "description", "servers", "organization", "as_of")
        },
        "tools": [
            {key: getattr(tool, key) for key in ("name", "description", "input_schema", "hint", "shape", "idempotent")}
            for tool in FAMILY.tools
        ],
    }


def source_identity(*, require_clean=False):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None or (require_clean and status):
        raise ValueError("publication freeze requires an exact clean source commit")
    # Including actual code bytes keeps dirty local experiments distinguishable.
    paths = sorted(
        [*MARKERS, *[f"{ENGINE}/{name}" for name in set(WORLD_ENGINE + VERIFIER_ENGINE)],
         *[path.relative_to(ROOT).as_posix() for path in (ROOT / "arc_release").glob("*.py")],
         *[path.relative_to(ROOT).as_posix() for path in (ROOT / "benchmark/dataset_factory/adapters/arc_crm").iterdir()
           if path.is_file() and path.suffix in {".py", ".json", ".sql"}]]
    )
    files = {relative: sha((ROOT / relative).read_bytes()) for relative in paths}
    return {
        "repository": "https://github.com/blobfishai/arc-crm-agent-simulation",
        "commit": commit, "dirty": bool(status), "implementation_sha256": digest(files), "implementation_files": files,
    }


def task_toml(task):
    text = f'''schema_version = "1.4"

[[artifacts]]
source = "/export"
destination = "arc-world-snapshot"
service = "world"

[[artifacts]]
source = "/run/arc-guard/startup.json"
destination = "arc-agent-isolation.json"
service = "main"

[task]
name = "blobfishai/{task['task_id']}"
version = "{VERSION}"
description = {json.dumps(task['title'])}
authors = [{{name = "Blobfish AI"}}]
keywords = ["crm", "synthetic", "mcp", "rest", "cli", "web", "closed-world"]

[agent]
user = "10001"
timeout_sec = 1200.0

[verifier]
user = "0"
timeout_sec = 120.0
environment_mode = "separate"

[[verifier.collect]]
command = "/usr/local/bin/python -I -S -B /opt/arc-world/collect.py"
service = "world"
user = "0"
timeout_sec = 40.0

[verifier.environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 1024
storage_mb = 2048
gpus = 0

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 1024
storage_mb = 2048
gpus = 0

[environment.healthcheck]
command = "/usr/local/bin/python -I -S -B /opt/arc-client/boundary.py health > /logs/artifacts/arc-isolation-health.txt 2>&1"
interval_sec = 1.0
timeout_sec = 40.0
start_period_sec = 0.0
start_interval_sec = 1.0
retries = 1
'''
    for server in [*FAMILY.servers, "hubbench"]:
        text += f'''
[[environment.mcp_servers]]
name = "{server}"
transport = "streamable-http"
url = "http://world:8765/mcp/{server}"
'''
    return text + f'''
[metadata]
benchmark = "Blobfish Arc CRM"
dataset = "{DATASET}"
task_id = "{task['task_id']}"
version = "{VERSION}"
metric = "HubScore"
synthetic = true
llm_judge = false
source_rows_reproduced = 0
'''


def agent_dockerfile():
    return f'''FROM {BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ARC_URL=http://world:8765
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \\
    && mkdir -p /workspace /public /opt/arc-client /solution /tests /logs/agent /logs/verifier /logs/artifacts \\
    && chown agent:agent /workspace /logs/agent /logs/artifacts \\
    && chmod 0700 /tests && chmod 0755 /solution
COPY client/ /opt/arc-client/
COPY public/ /public/
COPY tool /usr/local/bin/tool
RUN chmod -R a-w /opt/arc-client /public && chmod 0555 /usr/local/bin/tool
WORKDIR /workspace
CMD ["sleep", "infinity"]
'''


def world_dockerfile():
    return f'''FROM {BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY world/ /opt/arc-world/
RUN mkdir -p /state /run/arc-control && chmod 0700 /state /run/arc-control && chmod -R a-w /opt/arc-world
CMD ["/usr/local/bin/python", "-I", "-S", "-B", "/opt/arc-world/world.py"]
'''


COMPOSE = '''services:
  main:
    build:
      context: .
      dockerfile: Dockerfile
    depends_on:
      world:
        condition: service_healthy
    networks: [arc-internal]
    pids_limit: 256
    security_opt: ["no-new-privileges:true"]
    cap_drop: [NET_RAW]
  world:
    build:
      context: .
      dockerfile: Dockerfile.world
    cpus: 0.5
    mem_limit: 512m
    pids_limit: 128
    security_opt: ["no-new-privileges:true"]
    cap_drop: [ALL]
    volumes:
      - world-state:/state
      - arc-control:/run/arc-control
    networks: [arc-internal]
    healthcheck:
      test: ["CMD", "/usr/local/bin/python", "-I", "-S", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2).read()"]
      interval: 1s
      timeout: 3s
      retries: 30
networks:
  arc-internal:
    internal: true
volumes:
  world-state: {}
  arc-control: {}
'''

SURFACE_GUIDE = '''

## Executable interfaces

The CLI, HTML forms, REST and MCP all share the same live mock world.
There is no external CRM, document download, email delivery or signature service.

- CLI: `tool list`, `tool schema NAME`, `tool NAME '{"argument":"value"}'`.
- Web: `http://world:8765/`; inspect and submit `/app/<server>/<resource>/<operation>` forms.
- REST: `POST http://world:8765/api/v1/tools/<name>` with a JSON argument object.
- MCP: JSON-RPC `tools/call` at `http://world:8765/mcp/<server>`; server names are `arccrm`, `desk`, `vault`, `hubbench`.
- Public brief, tool schemas and binary evidence: `/public/task.json`, `/public/tools.json`, `/public/assets/`.

Use `hubbench.context.get` to discover the scoped records. Submit every field of
the public answer schema through `hubbench.submit_answer` after executing and
checking the business changes. The private verifier, database and reference
solution are not agent resources. Reading a local evidence file does not count
as the required provider-tool investigation recorded by the world.
'''


def freeze(output: Path, *, require_clean=False):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("release destination must be new; never overwrite a frozen candidate")
    source = source_identity(require_clean=require_clean)
    tasks = build_tasks()
    report = qualify(tasks)
    if not report["qualified"] or len(tasks) != 6 or [t["task_id"] for t in tasks] != [f"arc-crm-{n:03}" for n in range(1, 7)]:
        raise ValueError("the exact six-task authoring set must qualify before export")
    output.mkdir(parents=True, exist_ok=False)
    files = {}

    def write(relative, raw):
        path = output / safe_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = raw.encode() if isinstance(raw, str) else raw
        with path.open("xb") as handle:
            handle.write(raw)
        path.chmod(0o644)
        files[relative] = {"path": relative, "bytes": len(raw), "sha256": sha(raw), "mode": "0644"}

    def write_json(relative, value):
        write(relative, json_bytes(value))

    def copy(relative, original):
        path = ROOT / safe_relative(original)
        if path.is_symlink():
            raise ValueError("symlinked source input")
        write(relative, path.read_bytes())

    metadata = family_metadata()
    references = []
    records = []
    for task, qualification in zip(tasks, report["tasks"], strict=True):
        task_id = task["task_id"]
        prefix = f"harbor/tasks/{task_id}"
        public = public_task(task)
        world = public | {"seed_tables": task["seed_tables"]}
        sealed = {key: task[key] for key in SEALED_KEYS}
        identity = {
            "dataset": DATASET, "version": VERSION, "task_id": task_id,
            "source_commit": source["commit"], "source_dirty": source["dirty"],
            "implementation_sha256": source["implementation_sha256"], "source_lock_sha256": digest(SOURCE_LOCK),
            "authoring_task_sha256": digest(task), "world_task_sha256": sha(json_bytes(world)), "sealed_task_sha256": sha(json_bytes(sealed)),
        }
        instruction = task["instruction"] + SURFACE_GUIDE
        write(f"{prefix}/instruction.md", instruction)
        write(f"{prefix}/README.md", f"# {task['title']}\n\nIndependent synthetic Arc CRM workflow; not a reproduction of source rows.\n")
        write(f"{prefix}/task.toml", task_toml(task))
        write(f"{prefix}/environment/Dockerfile", agent_dockerfile())
        write(f"{prefix}/environment/Dockerfile.world", world_dockerfile())
        write(f"{prefix}/environment/docker-compose.yaml", COMPOSE)
        write(f"{prefix}/environment/tool", '#!/bin/sh\nexec /usr/local/bin/python -I -S -B /opt/arc-client/client.py "$@"\n')
        for name in ["client.py", "boundary.py"]:
            copy(f"{prefix}/environment/client/{name}", f"arc_release/{name}")
        write_json(f"{prefix}/environment/public/task.json", public)
        write_json(f"{prefix}/environment/public/tools.json", public_tool_definitions(FAMILY, task["answer_schema"]))
        write(f"{prefix}/environment/public/instruction.md", instruction)
        for asset in task["assets"]:
            safe_relative(asset["path"])
            if not asset["path"].startswith("assets/"):
                raise ValueError("evidence must stay inside the public assets directory")
            raw = asset_bytes(asset)
            write(f"{prefix}/environment/public/{asset['path']}", raw)
            write(f"tasks/{task_id}/{asset['path']}", raw)
        copy(f"{prefix}/environment/world/world.py", "arc_release/world.py")
        copy(f"{prefix}/environment/world/collect.py", "arc_release/collect.py")
        write_json(f"{prefix}/environment/world/task.json", world)
        write_json(f"{prefix}/environment/world/identity.json", identity)
        # A builder-free shim; original authoring __init__.py is NEVER copied.
        shim = '''import json
from pathlib import Path
from benchmark.hubbench.engine.families import Family
from .tools import TOOLS

def unavailable():
    raise RuntimeError("task builders are not part of the world runtime")

ROOT = Path(__file__).parent
FAMILY = Family(**json.loads((ROOT / "family.json").read_text()), schema_sql=(ROOT / "schema.sql").read_text(), tools=TOOLS, build_tasks=unavailable)
'''
        write(f"{prefix}/environment/world/runtime/arc_world/__init__.py", shim)
        write_json(f"{prefix}/environment/world/runtime/arc_world/family.json", metadata["family"])
        for name in ["tools.py", "schema.sql"]:
            copy(f"{prefix}/environment/world/runtime/arc_world/{name}", f"benchmark/dataset_factory/adapters/arc_crm/{name}")
        for runtime, engine_files in [(f"{prefix}/environment/world/runtime", WORLD_ENGINE), (f"{prefix}/tests/runtime", VERIFIER_ENGINE)]:
            for marker in GENERATED_MARKERS:
                write(f"{runtime}/{marker}", '"""Sealed distribution import namespace; no authoring exports."""\n')
            for original in [*MARKERS, *[f"{ENGINE}/{name}" for name in engine_files]]:
                copy(f"{runtime}/{original}", original)
        copy(f"{prefix}/tests/verify.py", "arc_release/verify.py")
        write(f"{prefix}/tests/Dockerfile", f'''FROM {BASE_IMAGE}
COPY . /tests/
RUN chmod -R a-w /tests && chmod 0700 /tests && chmod 0500 /tests/test.sh
WORKDIR /tests
CMD ["sleep", "infinity"]
''')
        write(f"{prefix}/tests/docker-compose.yaml", '''services:
  main:
    network_mode: none
    pids_limit: 256
    security_opt: ["no-new-privileges:true"]
    cap_drop: [NET_RAW]
''')
        write_json(f"{prefix}/tests/task.json", sealed)
        write_json(f"{prefix}/tests/identity.json", identity)
        write_json(f"{prefix}/tests/family.json", metadata)
        write(f"{prefix}/tests/test.sh", '''#!/bin/sh
set -eu
exec /usr/local/bin/python -I -S -B /tests/verify.py --bundle /export --isolation /run/arc-guard/startup.json
''')
        copy(f"{prefix}/solution/oracle.py", "arc_release/oracle.py")
        write_json(f"{prefix}/solution/steps.json", task["oracle_steps"])
        write(f"{prefix}/solution/solve.sh", '#!/bin/sh\nset -eu\nexec /usr/local/bin/python -I -S -B /solution/oracle.py\n')
        for directory in ["environment/public", "environment/world", "tests"]:
            copy(f"{prefix}/{directory}/ENGINE-NOTICE", "benchmark/hubbench/NOTICE")
            copy(f"{prefix}/{directory}/LICENSE", "LICENSE")
        task_digest, members = content_hash(output / prefix)
        references.append({"name": f"blobfishai/{task_id}", "digest": task_digest, "files": len(members)})
        write_json(f"tasks/{task_id}/task.json", public)
        write_json(f"tasks/{task_id}/tools.json", public_tool_definitions(FAMILY, task["answer_schema"]))
        write_json(f"tasks/{task_id}/oracle-trajectory.json", qualification["oracle"]["trace"])
        write_json(f"tasks/{task_id}/oracle-verdict.json", qualification["oracle"]["verdict"])
        records.append(public | {"harbor_task": f"harbor/tasks/{task_id}", "harbor_task_digest": task_digest,
                                 "oracle_trajectory": f"tasks/{task_id}/oracle-trajectory.json", "model_evaluated": False})
    dataset_manifest = f'''schema_version = "1.0"
[dataset]
name = "{DATASET}"
version = "{VERSION}"
description = "Six independently authored synthetic CRM workflows on one mocked CLI/web/REST/MCP world per episode. Partial Arc-inspired coverage, not source-row reproduction."
keywords = ["crm", "mcp", "synthetic", "closed-world"]
[[dataset.authors]]
name = "Blobfish AI"
'''
    for reference in references:
        dataset_manifest += f'\n[[tasks]]\nname = "{reference["name"]}"\ndigest = "{reference["digest"]}"\n'
    write("harbor/dataset.toml", dataset_manifest)
    write_json("source-lock.json", SOURCE_LOCK)
    write_json("source-identity.json", source)
    write_json("qualification.json", report)
    write("data/tasks.jsonl", "".join(canonical_json(record) + "\n" for record in records))
    copy("LICENSE", "LICENSE")
    copy("ENGINE-NOTICE", "benchmark/hubbench/NOTICE")
    copy("vendor-manifest.json", "vendor-manifest.json")
    write("README.md", f'''---
license: apache-2.0
language: [en]
tags: [benchmark, synthetic, crm, mcp, tool-use]
pretty_name: Blobfish Arc CRM 6
size_categories: [n<1K]
configs:
- config_name: default
  data_files:
  - split: test
    path: data/tasks.jsonl
---

# Blobfish Arc CRM 6

Six independently authored synthetic CRM workflows, 31 domain/evidence tools,
three mock servers plus two HubBench controls, and 33 binary evidence assets.
Every episode shares state across real CLI, HTML-form, REST and MCP execution.
The source-locked inspiration has 1,200 conversations and 27 tool names;
**zero source rows are reproduced** and this is not upstream API parity.

This freeze is a package candidate. Check separate publication receipts
for Docker, registry and exact remote object qualification; their existence is
not implied by this card. No model leaderboard score is claimed.
The six authoring oracles pass and replay identically; 167 negative controls reject.
These are solvability checks, not model performance or full source adaptation.

- Source repository: {source['repository']}/tree/{source['commit']}
- Source dataset pin and scope: `source-lock.json`.
- Six inspectable briefs and reference (oracle, not model) trajectories: `tasks/`.
- Full Harbor task packages and dataset manifest: `harbor/`.
- Actual file bytes/hashes and candidate status: `manifest.json`.

Run locally with Harbor 0.21.0 and Docker:

```sh
harbor run -p harbor/tasks -a oracle -e docker -n 1 -k 1 -r 0
```

Each trial has an internal-only network, 1 CPU/1 GiB main and 0.5 CPU/512 MiB
world limits, zero GPUs, a UID-10001 agent, and a fresh 1 CPU/1 GiB verifier
container with networking disabled. Agent and verifier never run together.
Harbor's declared 2 GiB storage budget is metadata, not a Docker-enforced disk
quota. No runtime installation or paid model call is required for these oracles.
Default Harbor cleanup removes only its trial containers, networks and volumes.

The `environment/` build context includes world seed state, but the agent image
copies only `client/` and `public/`. Do not mount the entire checkout or package
into a model container. `solution/` is oracle-only; `tests/` builds the separate
grading container. After stopping main, Harbor collects a serialized snapshot
from the world sidecar and destroys the agent environment before grading. Host
log mounts are not trusted; separate verification clears prior reward files.
All evidence and organizations are synthetic. The private collection credential
is random per episode, exists only in the world container, and is never published.

The shared verifier reports HubScore. HubBench names in HTTP/MCP are retained
engine attribution, not membership in the separately published HubBench v1.4.
New code and fixtures are Apache-2.0; the upstream card's MIT label is metadata,
not a license claim about linked upstream repositories. See `ENGINE-NOTICE`.
''')
    manifest = {
        "schema_version": 1, "dataset": DATASET, "version": VERSION, "status": "package-candidate",
        "publication_ready": False, "source": source, "base_image": BASE_IMAGE,
        "task_count": 6, "tool_count": 31, "engine_controls": 2, "assets": 33,
        "source_rows_reproduced": 0, "model_evaluated": False, "tasks": references,
        "remaining_gates": ["all-six frozen Docker oracles and isolation receipts", "clean merged source freeze",
                            "exact Hugging Face object identity", "all-six Harbor registry round trip", "website disclosure"],
        "files": [files[key] for key in sorted(files)],
    }
    write("manifest.json", json_bytes(manifest))
    return manifest


def verify_freeze(output: Path):
    output = Path(output).absolute()
    manifest = json.loads((output / "manifest.json").read_text())
    entries = manifest["files"]
    expected = {entry["path"] for entry in entries} | {"manifest.json"}
    if len(expected) != len(entries) + 1:
        raise ValueError("duplicate manifest members")
    actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != expected:
        raise ValueError("frozen membership changed")
    for entry in entries:
        path = output / safe_relative(entry["path"])
        if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
            raise ValueError("unsafe frozen member")
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or sha(raw) != entry["sha256"] or f"{path.stat().st_mode & 0o777:04o}" != entry["mode"]:
            raise ValueError(f"frozen member identity changed: {entry['path']}")
    references = manifest["tasks"]
    expected_names = [f"blobfishai/arc-crm-{n:03}" for n in range(1, 7)]
    if [reference["name"] for reference in references] != expected_names:
        raise ValueError("incorrect six-task membership")
    dataset = tomllib.loads((output / "harbor/dataset.toml").read_text())
    if dataset["dataset"]["name"] != DATASET or dataset["dataset"]["version"] != VERSION:
        raise ValueError("wrong dataset identity")
    if dataset["tasks"] != [{"name": reference["name"], "digest": reference["digest"]} for reference in references]:
        raise ValueError("dataset task membership/digests differ")
    for reference in references:
        task = output / "harbor/tasks" / reference["name"].split("/")[1]
        actual_digest, members = content_hash(task)
        config = tomllib.loads((task / "task.toml").read_text())
        if actual_digest != reference["digest"] or len(members) != reference["files"]:
            raise ValueError("Harbor task content changed")
        if config["task"]["name"] != reference["name"] or config["task"]["version"] != VERSION:
            raise ValueError("Harbor task identity changed")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "verify"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    manifest = freeze(args.path, require_clean=args.require_clean) if args.command == "freeze" else verify_freeze(args.path)
    print(json.dumps({key: manifest[key] for key in ["dataset", "version", "status", "task_count", "publication_ready"]}, sort_keys=True))


if __name__ == "__main__":
    main()
