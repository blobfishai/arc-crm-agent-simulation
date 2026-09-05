"""Trusted world entry point. Does not import task builders or grading code."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def resource_limits():
    root = Path("/sys/fs/cgroup")
    try:
        return {name: (root / name).read_text().strip() for name in ["cpu.max", "memory.max", "pids.max"]}
    except FileNotFoundError:
        return None  # local non-container tests; never accepted by the Docker guard


def owned_directory(path: Path):
    if path.is_symlink() or path == Path(path.anchor):
        raise ValueError("state/control directory must not be a symlink or filesystem root")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("state/control directory must be owned by the world process")
    if any(path.iterdir()) and stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("existing nonempty state/control directory is not private")
    path.chmod(0o700)


def owned_files(path: Path, allowed: set[str]):
    for member in path.iterdir():
        info = member.lstat()
        if member.name not in allowed or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unexpected, symlinked or unowned session member")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("session files must not be readable by other users")


def create_file(path: Path, raw: bytes):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)


def prepare_episode(state: Path, control: Path, identity: dict, family, task):
    from benchmark.hubbench.engine.world import seed_database

    owned_directory(state)
    owned_directory(control)
    binding = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    state_names = {"identity.json", "world.sqlite", "world.sqlite-journal", "world.sqlite-wal", "world.sqlite-shm"}
    owned_files(state, state_names)
    owned_files(control, {"identity.json", "verifier-token"})
    database = state / "world.sqlite"
    if not any(state.iterdir()) and not any(control.iterdir()):
        # seed_database overwrites paths; call it only after establishing a new
        # private empty state directory and before exposing either HTTP port.
        create_file(state / "identity.json", binding)
        seed_database(family, task, database)
        database.chmod(0o600)
        create_file(control / "identity.json", binding)
        create_file(control / "verifier-token", (secrets.token_hex(32) + "\n").encode())
    else:
        if (
            not database.is_file()
            or not (state / "identity.json").is_file()
            or not (control / "identity.json").is_file()
            or not (control / "verifier-token").is_file()
            or (state / "identity.json").read_bytes() != binding
            or (control / "identity.json").read_bytes() != binding
        ):
            raise ValueError("partial or different episode binding; use new job-scoped volumes")
    token = (control / "verifier-token").read_text().strip()
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("malformed episode credential")
    return database, sha(token.encode())


def private_server(public, identity: dict, token_sha256: str, *, host="0.0.0.0", port=8766):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, raw, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                self.send(200, json.dumps({"status": "ok", "task_id": identity["task_id"], "channel": "verifier", "limits": resource_limits()}).encode())
                return
            if self.path != "/verifier/snapshot":
                self.send(404, b'{"error":"not found"}')
                return
            token = self.headers.get("X-Arc-Verifier-Token", "")
            if not token or not hmac.compare_digest(sha(token.encode()), token_sha256):
                self.send(403, b'{"error":"forbidden"}')
                return
            # Serialize on the one world executor: state and durable trace are
            # one consistent snapshot, never a concurrent SQLite file copy.
            raw = public.session.run(lambda world: world.connection.serialize())
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.sqlite3")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Arc-Identity", sha(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()))
            self.send_header("X-Arc-Snapshot-SHA256", sha(raw))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.send(405, b'{"error":"read only"}')

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/arc-world"))
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument("--control", type=Path, default=Path("/run/arc-control"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--private-port", type=int, default=8766)
    args = parser.parse_args(argv)
    os.umask(0o077)
    # Executed with -I -S. Only this trusted, root-owned runtime is importable.
    sys.path.insert(0, str(args.root / "runtime"))
    from arc_world import FAMILY

    from benchmark.hubbench.engine.http import build_server

    raw = (args.root / "task.json").read_bytes()
    identity = json.loads((args.root / "identity.json").read_text())
    if sha(raw) != identity["world_task_sha256"]:
        raise ValueError("world projection does not match frozen identity")
    task = json.loads(raw)
    database, token_digest = prepare_episode(args.state, args.control, identity, FAMILY, task)
    public = build_server(FAMILY, task, database, host=args.host, port=args.port)
    private = private_server(public, identity, token_digest, host=args.host, port=args.private_port)
    thread = threading.Thread(target=private.serve_forever, daemon=True)
    thread.start()

    def stop(*_):
        threading.Thread(target=public.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"task_id": task["task_id"], "public_url": public.url, "private_port": private.server_port}), flush=True)
    try:
        public.serve_forever()
    finally:
        private.shutdown()
        thread.join(timeout=5)
        private.server_close()
        public.server_close()
        public.session.close()


if __name__ == "__main__":
    main()
