"""``python3 -m seerauto`` entry point."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - normal process entry
    sys.exit(main())
