# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "plotext>=5.3,<6",  # 6.x is a rewrite with a different API
#     "tzdata; sys_platform == 'win32'",  # Windows has no time zone database for zoneinfo
# ]
# ///
"""Live terminal graph of the room user count from ``data/snapshots.jsonl``.

The graph redraws when checkuser.py adds a snapshot or when the terminal changes size.
Press Ctrl+C to stop.

At start a menu asks for the display time zone. Each time label is converted with the
daylight saving rules in force at that snapshot, so a window across a DST change is correct.
Use ``--tz NAME`` (an IANA name such as Europe/London) to skip the menu.

A second menu asks for the time range: 1 hour, 1 day, 1 week, 1 month, 3 months,
6 months, 1 year or all data. Use ``--range KEY`` (for example ``--range week``) to skip it.
A month is 30 days and a year is 365 days. The x axis covers the whole range.

Colours:
    cyan line   = user count
    yellow line = average in the window
    green dot   = highest count, red dot = lowest count, magenta dot = newest count

Run it:
    uv run --script usergraph.py
    uv run --script usergraph.py --tz America/New_York --range week
    uv run --script usergraph.py --hours 6
    uv run --script usergraph.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# GMT+8 with no DST, the same offset as DISPLAY_TZ in checkuser.py. Used when there is no menu.
DEFAULT_TZ = "Asia/Kuala_Lumpur"
# Menu choices: (IANA name, short note). Edit this list to change the menu.
MENU_ZONES: list[tuple[str, str]] = [
    (DEFAULT_TZ, "GMT+8, same as checkuser"),
    ("UTC", "how the log stores times"),
    ("Europe/London", "UK"),
    ("Europe/Amsterdam", "Netherlands, Central Europe"),
    ("Europe/Helsinki", "Finland"),
    ("Europe/Istanbul", "Turkey"),
    ("America/New_York", "US Eastern"),
    ("America/Chicago", "US Central"),
    ("America/Los_Angeles", "US Pacific"),
    ("America/Sao_Paulo", "Brazil"),
    ("Australia/Sydney", "Australia Eastern"),
]
# Time range choices: (key for --range, menu text, hours). 0 hours means all data.
# Months and years are fixed lengths (30 and 365 days), not calendar months.
RANGES: list[tuple[str, str, float]] = [
    ("1h", "1 hour", 1),
    ("day", "1 day (24 hours)", 24),
    ("week", "1 week (7 days)", 7 * 24),
    ("month", "1 month (30 days)", 30 * 24),
    ("3months", "3 months (91 days)", 91 * 24),
    ("6months", "6 months (182 days)", 182 * 24),
    ("year", "1 year (365 days)", 365 * 24),
    ("all", "all data", 0),
]
DEFAULT_RANGE = "day"
DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "snapshots.jsonl"


@dataclass(frozen=True)
class Point:
    """One snapshot: when it was taken, which room, how many users."""

    taken_at: datetime
    room_id: int
    room_name: str
    count: int


def parse_lines(lines: list[str]) -> list[Point]:
    """Read snapshot JSON lines. Skip blank, broken or half-written lines.

    Args:
        lines: Lines from ``snapshots.jsonl``.

    Returns:
        The valid points, in file order.
    """
    points: list[Point] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            taken = datetime.fromisoformat(row["taken_at"].replace("Z", "+00:00"))
            if taken.tzinfo is None:
                taken = taken.replace(tzinfo=UTC)
            points.append(
                Point(taken, int(row["room_id"]), str(row.get("room_name", "")), int(row["count"]))
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return points


def select(points: list[Point], hours: float, room_id: int | None, now: datetime) -> list[Point]:
    """Keep the points of one room inside the time window.

    Args:
        points: All points.
        hours: Window size in hours. 0 or less keeps all of them.
        room_id: Room to show. ``None`` picks the room of the newest point.
        now: The current time (UTC aware).

    Returns:
        The selected points, sorted by time.
    """
    if not points:
        return []
    if room_id is None:
        room_id = max(points, key=lambda p: p.taken_at).room_id
    chosen = [p for p in points if p.room_id == room_id]
    if hours > 0:
        start = now - timedelta(hours=hours)
        chosen = [p for p in chosen if p.taken_at >= start]
    return sorted(chosen, key=lambda p: p.taken_at)


def utc_offset(dt: datetime) -> str:
    """Format the UTC offset of an aware datetime, for example ``UTC+05:30`` or ``UTC-04:00``."""
    minutes = int((dt.utcoffset() or timedelta()).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def dst_note(tz: tzinfo, now: datetime) -> str:
    """Describe the zone now and say if it uses daylight saving time this year.

    Args:
        tz: The zone.
        now: The current time (aware).

    Returns:
        Text such as ``GMT UTC+00:00, DST: BST UTC+01:00`` or ``+08 UTC+08:00, no DST``.
    """
    here = now.astimezone(tz)
    text = f"{here.tzname()} {utc_offset(here)}"
    # Compare mid-January and mid-July so both hemispheres are covered.
    jan = datetime(now.year, 1, 15, 12, tzinfo=UTC).astimezone(tz)
    jul = datetime(now.year, 7, 15, 12, tzinfo=UTC).astimezone(tz)
    if jan.utcoffset() == jul.utcoffset():
        return f"{text}, no DST"
    summer = jul if (jul.dst() or timedelta()) > (jan.dst() or timedelta()) else jan
    if here.utcoffset() == summer.utcoffset():
        winter = jan if summer is jul else jul
        return f"{text} (DST now), winter: {winter.tzname()} {utc_offset(winter)}"
    return f"{text}, DST: {summer.tzname()} {utc_offset(summer)}"


def system_zone_name() -> str | None:
    """Return the IANA name of this computer's time zone, or ``None`` if it is not known.

    It reads the ``TZ`` variable, then the ``/etc/localtime`` link (macOS and Linux).
    Windows has no such link, so the menu leaves out the "this computer" choice there.
    """
    env = os.environ.get("TZ", "").lstrip(":")
    if env and is_zone(env):
        return env
    real = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in real:
        name = real.split("zoneinfo/", 1)[1]
        if is_zone(name):
            return name
    return None


def is_zone(name: str) -> bool:
    """Return True if ``name`` is a known IANA time zone."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def menu_choices() -> list[tuple[str, str]]:
    """Return the menu zones, with this computer's zone first if it is known and not listed."""
    choices = list(MENU_ZONES)
    local = system_zone_name()
    if local and local not in {name for name, _ in choices}:
        choices.insert(0, (local, "this computer"))
    return choices


def pick_zone(answer: str, choices: list[tuple[str, str]]) -> str | None:
    """Turn a menu answer into a zone name.

    Args:
        answer: What the user typed: empty (default), a menu number, or an IANA name.
        choices: The menu zones.

    Returns:
        The zone name, or ``None`` if the answer is not valid.
    """
    answer = answer.strip()
    if not answer:
        return DEFAULT_TZ
    if answer.isdigit():
        index = int(answer) - 1
        return choices[index][0] if 0 <= index < len(choices) else None
    # Accept any case, for example "europe/london".
    matches = {name.lower(): name for name in available_timezones()}
    return matches.get(answer.lower())


def ask_zone(now: datetime) -> str:
    """Show the time zone menu and return the chosen IANA name.

    Raises:
        EOFError: If the input closes before a valid answer.
    """
    choices = menu_choices()
    print("Select the time zone for the graph:\n")
    for number, (name, note) in enumerate(choices, start=1):
        default = "  [default]" if name == DEFAULT_TZ else ""
        print(f"  {number:>2}. {name:<22} {note:<28} {dst_note(ZoneInfo(name), now)}{default}")
    print(
        "\nType a number, or any IANA name (for example Asia/Tokyo). Press Enter for the default."
    )
    while True:
        zone = pick_zone(input("> "), choices)
        if zone:
            return zone
        print("Not a menu number or a known IANA name. Try again.")


def pick_range(answer: str) -> str | None:
    """Turn a menu answer into a range key.

    Args:
        answer: What the user typed: empty (default), a menu number, or a key such as ``week``.

    Returns:
        The range key, or ``None`` if the answer is not valid.
    """
    answer = answer.strip().lower()
    if not answer:
        return DEFAULT_RANGE
    if answer.isdigit():
        index = int(answer) - 1
        return RANGES[index][0] if 0 <= index < len(RANGES) else None
    return answer if answer in {key for key, _, _ in RANGES} else None


def range_hours(key: str) -> tuple[str, float]:
    """Return (menu text, hours) for a range key."""
    for name, text, hours in RANGES:
        if name == key:
            return text, hours
    raise KeyError(key)


def ask_range() -> str:
    """Show the time range menu and return the chosen key.

    Raises:
        EOFError: If the input closes before a valid answer.
    """
    print("\nSelect the time range:\n")
    for number, (key, text, _) in enumerate(RANGES, start=1):
        default = "  [default]" if key == DEFAULT_RANGE else ""
        print(f"  {number:>2}. {text:<22} --range {key}{default}")
    print("\nType a number or a key. Press Enter for the default.")
    while True:
        key = pick_range(input("> "))
        if key:
            return key
        print("Not a menu number or a range key. Try again.")


def time_ticks(
    start: datetime, end: datetime, tz: tzinfo, count: int = 8
) -> list[tuple[float, str]]:
    """Make evenly spaced x axis labels from ``start`` to ``end``.

    Args:
        start: Left edge of the axis (aware).
        end: Right edge of the axis (aware).
        tz: Display zone. Each label uses the DST rules at its own time.
        count: Number of gaps between labels.

    Returns:
        (minutes after ``start``, label) pairs. Short spans show the time, long spans the date.
    """
    span = max((end - start).total_seconds() / 60, 1)
    if span <= 6 * 60:
        fmt = "%H:%M"
    elif span <= 8 * 24 * 60:
        fmt = "%d/%m %H:%M"
    else:
        fmt = "%d/%m/%y"
    return [
        (
            span * i / count,
            (start + timedelta(minutes=span * i / count)).astimezone(tz).strftime(fmt),
        )
        for i in range(count + 1)
    ]


def fit(text: str, width: int) -> str:
    """Cut ``text`` to ``width`` characters, ending with ``…`` if it is cut."""
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def draw(
    points: list[Point], hours: float, label: str, source: Path, tz: ZoneInfo, now: datetime
) -> None:
    """Clear the terminal and draw the graph with plotext.

    Args:
        points: The points to draw, sorted by time.
        hours: Window size in hours. 0 starts the axis at the first point.
        label: Range text for the title, for example ``1 week (7 days)``.
        source: The snapshots file, for the "no snapshots" message.
        tz: The display zone. Each label uses the DST rules at its own time.
        now: The current time (aware). It is the right edge of the axis.
    """
    import plotext as plt  # Imported here so the tests do not need plotext.

    plt.clear_figure()
    plt.clear_terminal()
    plt.theme("pro")
    width, height = shutil.get_terminal_size()
    plt.plotsize(width, max(height - 2, 10))

    if not points:
        plt.title(f"No snapshots in the last {label} in {source}")
        plt.show()
        return

    start = now - timedelta(hours=hours) if hours > 0 else points[0].taken_at
    end = max(now, points[-1].taken_at)
    xs = [(p.taken_at - start).total_seconds() / 60 for p in points]
    ys = [p.count for p in points]
    avg = sum(ys) / len(ys)
    hi = max(range(len(ys)), key=ys.__getitem__)
    lo = min(range(len(ys)), key=ys.__getitem__)
    newest = points[-1]

    plt.plot(xs, ys, color="cyan", marker="braille", label="users")
    plt.hline(avg, color="yellow")
    plt.scatter([xs[hi]], [ys[hi]], color="green", marker="●", label=f"max {ys[hi]}")
    plt.scatter([xs[lo]], [ys[lo]], color="red", marker="●", label=f"min {ys[lo]}")
    plt.scatter([xs[-1]], [ys[-1]], color="magenta", marker="◆", label=f"now {ys[-1]}")

    ticks = time_ticks(start, end, tz)
    plt.xticks([x for x, _ in ticks], [text for _, text in ticks])
    plt.xlim(0, ticks[-1][0])
    pad = max(2, (max(ys) - min(ys)) // 5)
    plt.ylim(max(min(ys) - pad, 0), max(ys) + pad)

    window = label if hours <= 0 else f"last {label}"
    first = points[0].taken_at.astimezone(tz)
    if hours > 0 and points[0].taken_at - start > timedelta(hours=1):
        window += f", data from {first:%d/%m/%y %H:%M}"
    last = newest.taken_at.astimezone(tz)
    zone = f"{last.tzname()} {utc_offset(last)}"
    title = (
        f"{window} | now {newest.count} | avg {avg:.1f} | last {last:%H:%M} {zone} | "
        f"{len(points)} snapshots | {newest.room_name}"
    )
    # plotext draws no title at all if it is wider than the plot, so cut it.
    plt.title(fit(title, width - 6))
    plt.xlabel(f"Time ({tz.key}, {zone})")
    plt.ylabel("Users")
    plt.show()


def file_state(path: Path) -> tuple[float, int] | None:
    """Return (mtime, size) of the file, or ``None`` if it does not exist."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime, stat.st_size


def load(path: Path) -> list[Point]:
    """Read every point from the file. A missing file gives no points."""
    try:
        return parse_lines(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return []


def main(argv: list[str] | None = None) -> int:
    """Parse the options and redraw the graph until Ctrl+C.

    Args:
        argv: Command-line options. ``None`` reads ``sys.argv``.

    Returns:
        The exit code (0).
    """
    parser = argparse.ArgumentParser(description="Live terminal graph of the room user count.")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="snapshots.jsonl path")
    parser.add_argument(
        "--range",
        choices=[key for key, _, _ in RANGES],
        default=None,
        help=f"time range, skips the range menu (no menu default: {DEFAULT_RANGE})",
    )
    parser.add_argument(
        "--hours", type=float, default=None, help="custom range in hours (0 = all), skips the menu"
    )
    parser.add_argument("--room", type=int, default=None, help="room id (default: newest room)")
    parser.add_argument(
        "--poll", type=float, default=2, help="seconds between file checks (default 2)"
    )
    parser.add_argument("--once", action="store_true", help="draw one time, then exit")
    parser.add_argument(
        "--tz", default=None, help=f"IANA time zone, skips the menu (no menu default: {DEFAULT_TZ})"
    )
    args = parser.parse_args(argv)

    if args.tz and pick_zone(args.tz, []) is None:
        parser.error(f"unknown time zone: {args.tz}")
    if args.range and args.hours is not None:
        parser.error("use --range or --hours, not both")
    interactive = sys.stdin.isatty()
    try:
        if args.tz:
            zone = pick_zone(args.tz, []) or DEFAULT_TZ
        else:
            zone = ask_zone(datetime.now(UTC)) if interactive else DEFAULT_TZ
        if args.hours is not None:
            label, hours = (
                (f"{args.hours:g} hours", args.hours) if args.hours > 0 else ("all data", 0)
            )
        else:
            key = args.range or (ask_range() if interactive else DEFAULT_RANGE)
            label, hours = range_hours(key)
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    tz = ZoneInfo(zone)

    last_key = None
    try:
        while True:
            key = (file_state(args.file), shutil.get_terminal_size())
            if key != last_key:
                last_key = key
                now = datetime.now(UTC)
                points = select(load(args.file), hours, args.room, now)
                draw(points, hours, label, args.file, tz, now)
                sys.stdout.flush()
            if args.once:
                return 0
            # Also redraw each minute so the time window moves forward.
            for _ in range(max(int(60 / args.poll), 1)):
                time.sleep(args.poll)
                if (file_state(args.file), shutil.get_terminal_size()) != last_key:
                    break
            else:
                last_key = None
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
