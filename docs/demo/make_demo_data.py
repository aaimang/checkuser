"""Write fake snapshots for the usergraph.py demo GIF.

The data is synthetic: the room is "Demo Room" and there are no player names, so no real
log goes into the repo. Counts follow a daily curve plus seeded noise.

Usage:
    python3 docs/demo/make_demo_data.py OUT.jsonl            # 8 days up to now
    python3 docs/demo/make_demo_data.py OUT.jsonl --append   # add one snapshot at the next mark
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOM_ID = 1
ROOM_NAME = "Demo Room"
STEP = timedelta(minutes=5)


def count_at(when: datetime, rng: random.Random) -> int:
    """Return a fake user count: busy in the evening (UTC+8), quiet at night."""
    hour = (when.hour + 8 + when.minute / 60) % 24
    daily = 14 * math.sin((hour - 14) / 24 * 2 * math.pi)
    return max(3, round(28 + daily + rng.gauss(0, 2.5)))


def row(when: datetime, count: int) -> str:
    """Format one snapshot line in the snapshots.jsonl layout."""
    return json.dumps(
        {
            "taken_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "room_id": ROOM_ID,
            "room_name": ROOM_NAME,
            "count": count,
            "names": [],
        }
    )


def main() -> None:
    """Write 8 days of snapshots, or append one snapshot with a jump in users."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()
    rng = random.Random(42)  # noqa: S311 - fake demo data, not security
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    mark = now - timedelta(minutes=now.minute % 5)

    if args.append:
        with args.out.open("a", encoding="utf-8") as f:
            f.write(row(mark + STEP, count_at(mark, rng) + 15) + "\n")
        return

    lines = []
    when = mark - timedelta(days=8)
    while when <= mark:
        lines.append(row(when, count_at(when, rng)))
        when += STEP
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
