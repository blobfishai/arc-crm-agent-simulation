import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_is_self_contained_outside_the_checkout(tmp_path):
    # Ignore PYTHONPATH and the current working directory. Only this checkout,
    # the interpreter and its standard library are needed by the runtime.
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import benchmark.dataset_factory.adapters.arc_crm as adapter
import benchmark.hubbench.engine.world as engine
from benchmark.dataset_factory.adapters.arc_crm.qualification import run
root = Path(sys.argv[1]).resolve()
assert Path(adapter.__file__).resolve().is_relative_to(root)
assert Path(engine.__file__).resolve().is_relative_to(root)
task = adapter.build_tasks()[0]
episode = run(task, task['oracle_steps'], Path(sys.argv[2]) / 'world.sqlite')
assert all(entry['success'] for entry in episode['trace'])
print(json.dumps(episode['verdict']))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(ROOT), str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["strict_pass"]
