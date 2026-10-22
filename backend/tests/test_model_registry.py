import subprocess
import sys


def test_importing_models_alone_configures_all_mappers():
    """`python -m app.reset_password` / `app.reset_2fa` import only part of the app: the mapper registry must still be complete."""
    code = "import app.models; from sqlalchemy.orm import configure_mappers; configure_mappers()"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-800:]


def test_reset_scripts_import_cleanly_and_configure_mappers():
    for module in ("app.reset_password", "app.reset_2fa"):
        code = f"import {module}; from sqlalchemy.orm import configure_mappers; configure_mappers()"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (module, result.stderr[-800:])
