"""Code must stay 3.10-compatible: fail on APIs that arrive in 3.11+."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED_IMPORTS = {"tomllib"}
BANNED_NAMES = {"TaskGroup", "StrEnum", "Self"}


def test_no_post_310_apis():
    bad = []
    for d in ("signalcourt", "scripts", "tests"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name == "test_py310_compat.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    bad += [f"{p}:{node.lineno} import {a.name}" for a in node.names if a.name in BANNED_IMPORTS]
                elif isinstance(node, ast.ImportFrom):
                    if node.module in BANNED_IMPORTS:
                        bad.append(f"{p}:{node.lineno} from {node.module}")
                    bad += [f"{p}:{node.lineno} {a.name}" for a in node.names if a.name in BANNED_NAMES]
                elif isinstance(node, ast.Attribute):
                    is_utc = node.attr == "UTC" and getattr(node.value, "id", "") == "datetime"
                    if node.attr in BANNED_NAMES or is_utc:
                        bad.append(f"{p}:{node.lineno} .{node.attr}")
    assert not bad, "\n".join(bad)
