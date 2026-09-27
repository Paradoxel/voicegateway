"""The agent-side code never touches the dependency-injection container.

``attach()``, ``guard()``, the sinks and the fleet heartbeat run inside the
user's agent process. ``dependency-injector`` ships only with the
``[dashboard]`` extra, and wiring patches the modules it is wired into, so
none of that has any business in someone else's process. The container is the
server's composition root and stays there.

Two checks. The static one walks every import, including the lazy ones inside
functions that a runtime check only sees when the function runs. The runtime
one proves the agent entry points load without the library at all.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]

#: Everything that runs in the user's agent process.
_AGENT_SIDE = [
    _SRC / "inference",
    _SRC / "fleet",
    _SRC / "accounting",
    _SRC / "guard.py",
    _SRC / "openrtc.py",
    _SRC / "__init__.py",
    _SRC / "services" / "sinks.py",
]

_FORBIDDEN = (
    "dependency_injector",
    "voicegateway.core.container",
    "voicegateway.core.app_wiring",
)


def _modules(path: Path) -> list[Path]:
    return sorted(path.rglob("*.py")) if path.is_dir() else [path]


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def test_agent_side_modules_never_import_the_container() -> None:
    offenders = []
    for root in _AGENT_SIDE:
        for path in _modules(root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, name in _imports(tree):
                if name.startswith(_FORBIDDEN):
                    offenders.append(f"{path.relative_to(_SRC)}:{lineno}: {name}")
    assert not offenders, "\n".join(offenders)


def test_attach_loads_without_dependency_injector() -> None:
    code = (
        "import sys, voicegateway; "
        "from voicegateway import attach, guard, register_worker; "
        "import voicegateway.services.sinks; "
        "assert 'dependency_injector' not in sys.modules, 'container library loaded'; "
        "print('CLEAN')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout
