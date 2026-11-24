"""Makes the OAuth fixture (`tests/fixtures/oauth/`) importable from tests/auth, the way
tests/mine and tests/cli already reach their sibling helpers: by path, since tests/ is
not a package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "fixtures" / "oauth"))
