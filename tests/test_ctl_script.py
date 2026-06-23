from pathlib import Path


def test_ctl_script_checks_command_availability():
    script = Path("ctl.sh").read_text(encoding="utf-8")
    assert "command -v pgrep" in script
    assert "command -v ss" in script
