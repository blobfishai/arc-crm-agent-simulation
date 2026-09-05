"""Local-only world startup inspection; never a qualification or model result."""

import argparse
import asyncio
import json
import tempfile
import uuid
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import TaskConfig
from harbor.models.trial.paths import TrialPaths


async def inspect(args):
    references = json.loads((args.frozen / "manifest.json").read_text())["tasks"]
    for reference in references:
        task_id = reference["name"].split("/")[1]
        if args.task and task_id not in args.task:
            continue
        task = args.cache / reference["name"] / reference["digest"].split(":")[1]
        config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
        with tempfile.TemporaryDirectory(prefix="arc-startup-diagnostic-") as output:
            environment = DockerEnvironment(
                environment_dir=task / "environment", environment_name=task_id,
                session_id=f"arc-inspect-{task_id}-{uuid.uuid4().hex[:8]}",
                trial_paths=TrialPaths(Path(output)), task_env_config=config.environment,
            )
            try:
                try:
                    await asyncio.wait_for(environment.start(False), timeout=120)
                    print(json.dumps({"task": task_id, "started": True}), flush=True)
                except Exception as error:
                    print(json.dumps({"task": task_id, "started": False, "exception": type(error).__name__}), flush=True)
                log = await environment._run_docker_compose_command(["logs", "--no-color", "world"], check=False, timeout_sec=10)
                print(log.stdout, flush=True)
                metadata = "from pathlib import Path; import hashlib,json; p=Path('/opt/arc-world'); print(json.dumps({'identity':json.loads((p/'identity.json').read_text()),'task_sha256':hashlib.sha256((p/'task.json').read_bytes()).hexdigest()}))"
                result = await environment._run_docker_compose_command(
                    ["run", "--rm", "--no-deps", "--entrypoint", "/usr/local/bin/python", "world", "-I", "-S", "-B", "-c", metadata],
                    check=False, timeout_sec=30,
                )
                print(result.stdout, flush=True)
            finally:
                await environment.stop(delete=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frozen", type=Path)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/harbor/tasks/packages")
    parser.add_argument("--task", action="append", choices=[f"arc-crm-{number:03}" for number in range(1, 7)])
    asyncio.run(inspect(parser.parse_args()))
