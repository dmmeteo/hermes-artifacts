"""Online backup: ``python -m app.backup DESTINATION`` copies a consistent snapshot via SQLite's backup API."""

import sys
import time
from pathlib import Path

from .store import Store


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.backup DESTINATION", file=sys.stderr)
        return 2
    import os

    source = Path(os.environ.get("ARTIFACTS_DB", "data/artifacts.db"))
    if not source.exists():
        print(f"database not found: {source}", file=sys.stderr)
        return 1
    destination = Path(argv[1])
    try:
        Store(source, lambda: int(time.time())).backup(destination)
    except FileExistsError:
        print(f"refusing to overwrite {destination}", file=sys.stderr)
        return 1
    print(f"backup written to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
