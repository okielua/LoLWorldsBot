from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_FILES = sorted(
    path
    for path in PROJECT_ROOT.rglob("*.py")
    if not any(
        part.startswith(".") or part in {"venv", "__pycache__", "node_modules"}
        for part in path.relative_to(PROJECT_ROOT).parts[:-1]
    )
)


@pytest.mark.parametrize(
    "path", PYTHON_FILES, ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix()
)
def test_project_python_compiles(path):
    # Compile source without executing imports, starting Discord, or writing .pyc files.
    compile(path.read_bytes(), str(path), "exec")
