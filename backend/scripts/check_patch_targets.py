"""Report @patch("app...") targets whose module or named object no longer exists.

Module moves rename these silently: the target is a string, so ruff and ty never look
inside it, and the failure only surfaces when that one test runs. Deep attributes are
not checked - plenty are created lazily or patched with create=True.
"""

import importlib
import re
import sys
from pathlib import Path

MISSING = object()
targets = {
    m.group(1)
    for f in Path("tests").rglob("*.py")
    for m in re.finditer(r'patch(?:\.object)?\(\s*["\']([\w.]+)["\']', f.read_text())
    if m.group(1).startswith("app.")
}
broken = []
for t in sorted(targets):
    parts = t.split(".")
    for i in range(len(parts), 0, -1):
        try:
            mod = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        if parts[i:] and getattr(mod, parts[i], MISSING) is MISSING:
            broken.append(t)
        break
    else:
        broken.append(t)
for t in broken:
    print(f"STALE PATCH TARGET: {t}")
sys.exit(1 if broken else 0)
