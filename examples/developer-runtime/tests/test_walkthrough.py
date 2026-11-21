"""Fresh-process actual local contracts; no shared API state or personal profile."""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_existing_local_extension_contract_in_a_fresh_process():
    result = subprocess.run([sys.executable, str(ROOT / "walkthrough.py")],
                            capture_output=True, text=True, timeout=35, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt == {
        "contract": "local-developer-extension-v1", "validated": True,
        "registered_via_existing_route": True, "denied_execution_count": 0,
        "reviewed_sum": 42, "executions": 1, "single_use_review": True,
        "foreign_session_review_refused": True, "invalid_arguments_refused": True,
        "bounds_refused": True, "uninstalled": True, "stale_review_no_execution": True,
    }
