"""Put ``src`` on the path so the tests run without an editable install.

``pip install -e .`` also works and is what the README recommends; this keeps a
bare ``pytest`` working for a reviewer who skipped that step.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
