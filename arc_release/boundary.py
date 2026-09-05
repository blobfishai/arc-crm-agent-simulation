"""Agent-phase isolation proof. Host log mounts are explicitly untrusted."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

AGENT_UID = 10001
MAIN_LIMITS = {"cpu.max": "100000 100000", "memory.max": "1073741824", "pids.max": "256"}
WORLD_LIMITS = {"cpu.max": "50000 100000", "memory.max": "536870912", "pids.max": "128"}


def denied_open(path, flags):
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno not in {1, 2, 13, 30}:
            raise
        return
    os.close(descriptor)
    raise ValueError(f"agent could open protected path: {path}")


def agent_probes():
    if os.getuid() != AGENT_UID:
        raise ValueError("isolation probes require the agent UID")
    for path in ["/run/arc-control/verifier-token", "/tests", "/state/world.sqlite", "/run/arc-guard"]:
        denied_open(path, os.O_RDONLY)
    for path in ["/opt/arc-client/client.py", "/usr/local/bin/tool"]:
        denied_open(path, os.O_WRONLY)
    for directory in ["/tests", "/solution", "/opt/arc-client", "/public", "/run/arc-guard"]:
        probe = Path(directory) / ".arc-write-probe"
        try:
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as error:
            if error.errno not in {1, 2, 13, 30}:
                raise
        else:
            os.close(descriptor)
            probe.unlink()
            raise ValueError(f"agent could create a file in {directory}")
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for method, header, expected in [("GET", {}, 403), ("GET", {"X-Arc-Verifier-Token": "wrong"}, 403), ("POST", {}, 405)]:
        request = urllib.request.Request("http://world:8766/verifier/snapshot", method=method, headers=header)
        try:
            with direct.open(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
        if status != expected:
            raise ValueError("private world endpoint isolation failed")
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1):
            pass
    except OSError:
        pass
    else:
        raise ValueError("closed-world agent unexpectedly reached external TCP")
    return {"agent_uid": AGENT_UID, "protected_paths_denied": True, "private_api_denied": True, "external_tcp_denied": True}


def health():
    if os.getuid() != 0:
        raise ValueError("phase guard must run as root")
    for path, mode in [(Path("/tests"), 0o700), (Path("/solution"), 0o755)]:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError(f"unexpected protected directory ownership/mode: {path}")
    if Path("/run/arc-control").exists():
        raise ValueError("world verifier credentials must not be mounted in the agent container")
    startup = Path("/run/arc-guard")
    startup.mkdir(mode=0o700, exist_ok=False)
    startup.chmod(0o700)

    def unprivileged():
        os.setgroups([])
        os.setgid(AGENT_UID)
        os.setuid(AGENT_UID)

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "probe"],
        check=False, capture_output=True, text=True, timeout=35, preexec_fn=unprivileged,
    )
    if result.returncode:
        raise ValueError("dropped-UID isolation probe failed: " + result.stderr)
    proof = json.loads(result.stdout)
    main_limits = {name: (Path("/sys/fs/cgroup") / name).read_text().strip() for name in MAIN_LIMITS}
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with direct.open("http://world:8766/health", timeout=10) as response:
        world_limits = json.load(response)["limits"]
    if main_limits != MAIN_LIMITS or world_limits != WORLD_LIMITS:
        raise ValueError("cgroup CPU/memory/PID limits differ from the task budget")
    proof |= {"main_limits": main_limits, "world_limits": world_limits, "verifier_mode": "separate",
              "host_log_mounts_trusted": False, "world_credentials_mounted_in_agent": False}
    with (startup / "startup.json").open("x") as handle:
        json.dump(proof, handle, sort_keys=True)
    (startup / "startup.json").chmod(0o600)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["health", "probe"])
    args = parser.parse_args()
    print(json.dumps(agent_probes() if args.phase == "probe" else health(), sort_keys=True))


if __name__ == "__main__":
    main()
