"""Safety checks for manual read-only production crosscheck workflow."""
from pathlib import Path
import yaml


def test_workflow_is_manual_pinned_and_read_only():
    path = Path(".github/workflows/production-nbbet-stats-crosscheck-dry-run.yml")
    content = path.read_text()
    data = yaml.safe_load(content)
    assert (data.get("on", data.get(True))) == {"workflow_dispatch": None}
    assert "schedule:" not in content
    assert " --apply" not in content
    assert "production-nbbet-stats-crosscheck-dry-run" in content
    assert "StrictHostKeyChecking=yes" in content
    assert "SSH_FINGERPRINT" in content
    assert "stats_crosscheck.py" in content
    assert "--lookback-days 1" in content
    assert "--max-minutes 38" in content
    assert "github.ref == 'refs/heads/main'" in content
    assert "upload-artifact@" in content
