import subprocess
import sys

from thoth import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "thoth", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
