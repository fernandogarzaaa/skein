"""Encoding guard: shipped surfaces must be pure ASCII.

History: cp1252 consoles crashed twice on two different non-ASCII
instances (CLI status icons in Stage 7, profile source notes in
Stage 8). Both instances lived in src/skein and reached real console
output, so the boundary enforced here is: everything under src/skein
must be ASCII. Docs prose (README/CHANGELOG/decisions) may use UTF-8
freely - those files are never printed to a console by the tool, with
one exception covered transitively: the README support-matrix block is
byte-compared against generated registry output (test_backends.py), so
it can only contain what the ASCII-clean registry strings produce.
"""

from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "skein"


def test_shipped_sources_are_ascii():
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        try:
            text = path.read_text(encoding="ascii")
        except UnicodeDecodeError as e:
            offenders.append(f"{path.name}: {e}")
        else:
            bad = sorted({c for c in text if ord(c) > 127})
            if bad:
                offenders.append(f"{path.name}: {bad}")
    assert not offenders, "non-ASCII in shipped sources:\n" + "\n".join(offenders)
