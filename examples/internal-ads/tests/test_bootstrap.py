import shutil
import stat
import subprocess
import sys
from pathlib import Path


def test_bootstrap_config_permissions_and_no_overwrite(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for filename in ("bootstrap.py", "accounts.example.json"):
        shutil.copyfile(root / filename, tmp_path / filename)
    result = subprocess.run(
        [sys.executable, str(tmp_path / "bootstrap.py"), "--demo"], capture_output=True, text=True
    )
    assert result.returncode == 0
    private = tmp_path / ".env"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "accounts.json").stat().st_mode) == 0o644
    original = private.read_bytes()
    assert b"ADS_ENABLE_WRITES=false" in original and b"ADS_DEMO=true" in original
    second = subprocess.run(
        [sys.executable, str(tmp_path / "bootstrap.py")], capture_output=True, text=True
    )
    assert second.returncode != 0 and private.read_bytes() == original
    for line in original.decode().splitlines():
        if line.startswith(("ADS_API_KEY=", "ADS_ADMIN_KEY=", "ADS_ENCRYPTION_KEY=")):
            assert line.split("=", 1)[1] not in result.stdout
