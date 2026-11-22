# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "g-python==0.1.6",
#     "pydantic>=2.0",
#     "rich>=13.0",
# ]
# ///
"""Room headcount logger for Habbo, as a G-Earth extension (G-Python).

What it does:

* Tracks the Habbo users (type 1 only: no bots, no pets) in the room you are in.
* Keeps ``current_users.txt``: a rich table of who is in the room now. It is rewritten
  on every join and leave.
* Every 5 minutes on the clock (:00, :05, :10 ... :55) it saves a snapshot (time, room,
  count, names) to ``snapshots.jsonl`` and rewrites ``history.txt``, a second rich table.
* The chat command ``:checkuser`` opens both files on this computer. The command is
  blocked, so nobody in the room sees it.

Usage::

    uv run --script checkuser.py --install --dry-run   # show what G-Earth would run
    uv run --script checkuser.py --install             # write G-Earth's command.txt
    uv run --script checkuser.py --show                # print the history table
    uv run --script checkuser.py --demo                # print both tables with sample data

Then restart G-Earth, then the Habbo client.

Unofficial fan project, not affiliated with or endorsed by Sulake Oy. G-Earth is a
man-in-the-middle on the game connection. Using it breaks Habbo's Terms of Service.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table

with warnings.catch_warnings():
    # g-python 0.1.6 uses `x is not 'NULL'`, which warns on Python 3.12+. It is harmless.
    warnings.simplefilter("ignore", SyntaxWarning)
    from g_python.gextension import Extension
    from g_python.hdirection import Direction
    from g_python.hmessage import HMessage

__version__ = "0.5.0"

log = logging.getLogger("checkuser")

COMMAND = ":checkuser"
# All times the user sees (tables, 5-minute marks) are in GMT+8. Stored times are UTC.
DISPLAY_TZ = timezone(timedelta(hours=8), "GMT+8")
# Fewest history rows the terminal view keeps, even when the user table fills the screen.
MIN_LIVE_HISTORY_ROWS = 3
# Longest title of the current-users table. The table is at least TITLE_MAX + 4 wide, so the
# title never wraps into a second line.
TITLE_MAX = 46
# Lines the terminal view leaves free under the tables: log lines and the shell prompt.
LIVE_MARGIN = 3

EXTENSION_INFO = {
    "title": "Checkuser",
    "description": "Logs who is in the room and counts every 5 minutes. Type :checkuser.",
    "version": __version__,
    "author": "aaimang",  # TODO: confirm the author name for G-Earth's list
}
# Packet names this extension uses. Checked against G-Earth's catalogue on connect.
NEEDS: dict[Direction, tuple[str, ...]] = {
    Direction.TO_SERVER: ("Chat", "Shout"),
    Direction.TO_CLIENT: (
        "Users",
        "UserRemove",
        "RoomReady",
        "CloseConnection",
        "GetGuestRoomResult",
    ),
}

# Entity type numbers in the `Users` packet (docs: references/packets.md section 7).
ENTITY_PLAYER, ENTITY_PET, ENTITY_OLD_BOT, ENTITY_BOT = 1, 2, 3, 4
# Sanity limits for parsing. These are guards against garbage, not game rules.
MAX_ENTITIES = 1000
MAX_BOT_SKILLS = 256
ROOM_NAME_CACHE = 32
PAYLOAD_START = 6  # an HPacket starts with a 4-byte length and a 2-byte header id


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


class Settings(BaseModel):
    """Run settings.

    Attributes:
        interval_minutes: Minutes between history snapshots. Must divide 60.
        data_dir: Folder for ``snapshots.jsonl``, both tables and the log.
        terminal_rows: Most history rows the terminal view shows (the newest ones; fewer when
            the window is small). ``history.txt`` and ``snapshots.jsonl`` hold every snapshot.
        table_width: Character width limit of both table files. The history table
            needs about 76 characters; the current-users table is narrower.
        burst_seconds: How long the live writer waits to merge a burst of changes.
    """

    model_config = ConfigDict(frozen=True)

    interval_minutes: int = Field(default=5, ge=1, le=60)
    data_dir: Path
    terminal_rows: int = Field(default=50, ge=1)
    table_width: int = Field(default=100, ge=60)
    burst_seconds: float = Field(default=0.3, ge=0)

    @field_validator("interval_minutes")
    @classmethod
    def _divides_hour(cls, value: int) -> int:
        """Check that the interval gives the same clock marks every hour.

        Args:
            value: The interval in minutes.

        Returns:
            The same value.

        Raises:
            ValueError: If 60 is not a multiple of the interval.
        """
        if 60 % value:
            raise ValueError("interval_minutes must divide 60 (1, 2, 3, 4, 5, 6, 10, 12, ...)")
        return value

    @property
    def snapshots_path(self) -> Path:
        """Path of the JSON Lines file with every snapshot."""
        return self.data_dir / "snapshots.jsonl"

    @property
    def history_path(self) -> Path:
        """Path of the history table file."""
        return self.data_dir / "history.txt"

    @property
    def current_path(self) -> Path:
        """Path of the current users table file."""
        return self.data_dir / "current_users.txt"


class Occupant(BaseModel):
    """A Habbo user in the room.

    Attributes:
        name: The user name.
        seen_since: When this extension first saw the user in the room.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    seen_since: AwareDatetime


class RoomView(BaseModel):
    """A copy of the room state at one moment.

    Attributes:
        room_id: Room id, or ``None`` when the user is not in a room.
        room_name: Room name, or an empty string if it is not known.
        occupants: Habbo users in the room (no bots, no pets).
    """

    model_config = ConfigDict(frozen=True)

    room_id: int | None
    room_name: str
    occupants: tuple[Occupant, ...]

    @property
    def label(self) -> str:
        """Room name for tables: the name, or ``room <id>``, or an empty string."""
        if self.room_name:
            return self.room_name
        return f"room {self.room_id}" if self.room_id is not None else ""

    @property
    def names(self) -> list[str]:
        """Unique user names, sorted without regard to case."""
        return sorted({o.name for o in self.occupants}, key=lambda n: (n.casefold(), n))


class Snapshot(BaseModel):
    """One history row: who was in the room at a clock mark.

    Attributes:
        taken_at: Time of the clock mark (or the real time if the mark was missed), kept in
            UTC. Tables show it in GMT+8.
        room_id: Room id.
        room_name: Room name for display.
        count: Number of users. Equals ``len(names)``.
        names: User names, sorted without regard to case, no duplicates.
    """

    model_config = ConfigDict(frozen=True)

    taken_at: AwareDatetime
    room_id: int
    room_name: str
    count: int = Field(ge=0)
    names: list[str]

    @field_validator("taken_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        """Store time in UTC. Tables convert to local time only for display.

        Args:
            value: Any aware time.

        Returns:
            The same instant in UTC.
        """
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_names(self) -> Snapshot:
        """Check that the count, the order and the uniqueness of the names are right.

        Returns:
            This snapshot.

        Raises:
            ValueError: If ``count`` is not ``len(names)``, a name repeats, or the order is wrong.
        """
        if self.count != len(self.names):
            raise ValueError(f"count {self.count} does not match {len(self.names)} names")
        if len(set(self.names)) != len(self.names):
            raise ValueError("names must be unique")
        if self.names != sorted(self.names, key=lambda n: (n.casefold(), n)):
            raise ValueError("names must be sorted without regard to case")
        return self

    @classmethod
    def from_view(cls, view: RoomView, taken_at: datetime) -> Snapshot:
        """Build a snapshot from a room view.

        Args:
            view: The room state. ``view.room_id`` must not be ``None``.
            taken_at: Time to record.

        Returns:
            The snapshot.

        Raises:
            ValueError: If the view has no room.
        """
        if view.room_id is None:
            raise ValueError("no room")
        names = view.names
        return cls(
            taken_at=taken_at,
            room_id=view.room_id,
            room_name=view.label,
            count=len(names),
            names=names,
        )


# --------------------------------------------------------------------------------------
# Packet parsing (bytes in, plain data out; no G-Python needed, so tests are simple)
# --------------------------------------------------------------------------------------


class ProtocolError(ValueError):
    """A packet did not match the expected layout."""


class Reader:
    """Bounds-checked reads from packet payload bytes (big-endian, UTF-8 strings)."""

    def __init__(self, data: bytes) -> None:
        """Wrap payload bytes (without the 6-byte packet header).

        Args:
            data: The payload.
        """
        self.data = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        """Number of unread bytes."""
        return len(self.data) - self.pos

    def _take(self, n: int, what: str) -> bytes:
        if n < 0 or self.remaining < n:
            raise ProtocolError(f"truncated {what} at byte {self.pos}")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def i32(self) -> int:
        """Read a signed 32-bit integer."""
        return int.from_bytes(self._take(4, "int"), "big", signed=True)

    def boolean(self) -> bool:
        """Read a 1-byte boolean."""
        return self._take(1, "bool") != b"\x00"

    def string(self) -> str:
        """Read a string with a 16-bit length, decoded as UTF-8."""
        length = int.from_bytes(self._take(2, "string length"), "big")
        return self._take(length, "string").decode("utf-8", errors="replace")

    def skip(self, n: int) -> None:
        """Skip ``n`` bytes."""
        self._take(n, "skipped bytes")


@dataclass(frozen=True)
class Player:
    """A Habbo user from the ``Users`` packet.

    Attributes:
        index: Room index (``UserRemove`` uses this).
        name: User name.
        user_id: Account id.
    """

    index: int
    name: str
    user_id: int


@dataclass(frozen=True)
class UsersParse:
    """Result of parsing a ``Users`` packet.

    Attributes:
        players: Players read from the packet (users only, no bots, no pets).
        error: ``None`` if the whole packet matched a layout. Otherwise the problem; then
            ``players`` holds only the entries read before the problem.
    """

    players: tuple[Player, ...]
    error: str | None = None


def _read_entity(r: Reader, live_tail: bool) -> Player | None:
    """Read one entity of the ``Users`` packet.

    Args:
        r: Reader positioned at the entity.
        live_tail: Whether a player has one extra int after ``isModerator`` (the live client
            does; the public documentation does not).

    Returns:
        A ``Player`` for a user, or ``None`` for a pet or a bot.

    Raises:
        ProtocolError: If the entity is truncated or has an unknown type.
    """
    user_id = r.i32()
    name = r.string()
    r.string()  # motto
    r.string()  # figure
    index = r.i32()
    r.i32()  # x
    r.i32()  # y
    r.string()  # z
    r.i32()  # direction
    kind = r.i32()
    if kind == ENTITY_PLAYER:
        r.string()  # sex
        r.i32()  # group id
        r.i32()  # group status
        r.string()  # group name
        r.string()  # swim figure
        r.i32()  # achievement score
        r.boolean()  # is moderator
        if live_tail:
            r.i32()
        return Player(index=index, name=name, user_id=user_id)
    if kind == ENTITY_PET:
        r.i32()  # sub type
        r.i32()  # owner id
        r.string()  # owner name
        r.i32()  # rarity
        for _ in range(6):  # saddle, riding, breed, harvest, revive, breeding permission
            r.boolean()
        r.i32()  # level
        r.string()  # posture
        return None
    if kind == ENTITY_OLD_BOT:
        return None
    if kind == ENTITY_BOT:
        r.string()  # sex
        r.i32()  # owner id
        r.string()  # owner name
        skills = r.i32()
        if not 0 <= skills <= MAX_BOT_SKILLS:
            raise ProtocolError(f"implausible bot skill count {skills}")
        r.skip(2 * skills)  # each skill is a 16-bit value
        return None
    raise ProtocolError(f"unknown entity type {kind}")


def _parse_users_with(payload: bytes, live_tail: bool) -> UsersParse:
    """Parse a ``Users`` payload with one fixed player tail layout.

    Args:
        payload: Packet payload (without header).
        live_tail: Which player tail layout to try.

    Returns:
        The parse. ``error`` is set if it did not match.
    """
    r = Reader(payload)
    players: list[Player] = []
    try:
        count = r.i32()
        if not 0 <= count <= MAX_ENTITIES:
            raise ProtocolError(f"implausible entity count {count}")
        for _ in range(count):
            player = _read_entity(r, live_tail)
            if player is not None:
                players.append(player)
        if r.remaining:
            raise ProtocolError(f"{r.remaining} unread bytes after {count} entities")
    except ProtocolError as exc:
        return UsersParse(tuple(players), str(exc))
    return UsersParse(tuple(players))


def parse_users(payload: bytes) -> UsersParse:
    """Parse a ``Users`` packet: try the live layout first, then the documented one.

    Args:
        payload: Packet payload (without header).

    Returns:
        The parse. If both layouts fail, the one that read more players is returned with
        its ``error`` set.
    """
    live = _parse_users_with(payload, live_tail=True)
    if live.error is None:
        return live
    documented = _parse_users_with(payload, live_tail=False)
    if documented.error is None:
        return documented
    return documented if len(documented.players) > len(live.players) else live


def parse_user_remove(payload: bytes) -> int:
    """Parse ``UserRemove``: one string holding a room index.

    Args:
        payload: Packet payload (without header).

    Returns:
        The room index.

    Raises:
        ProtocolError: If the packet is short or the string is not a number.
    """
    text = Reader(payload).string()
    try:
        return int(text)
    except ValueError:
        raise ProtocolError(f"user index {text!r} is not a number") from None


def parse_room_ready(payload: bytes) -> int:
    """Parse ``RoomReady``: ``s roomModel, i roomId``.

    Args:
        payload: Packet payload (without header).

    Returns:
        The room id.

    Raises:
        ProtocolError: If the packet is short.
    """
    r = Reader(payload)
    r.string()
    return r.i32()


def parse_guest_room(payload: bytes) -> tuple[int, str]:
    """Parse the start of ``GetGuestRoomResult``: ``b enterRoom, i flatId, s roomName``.

    The rest of the layout is an estimate in public sources, so it is ignored.

    Args:
        payload: Packet payload (without header).

    Returns:
        ``(room id, room name)``.

    Raises:
        ProtocolError: If the packet is short.
    """
    r = Reader(payload)
    r.boolean()
    room_id = r.i32()
    return room_id, r.string()


def parse_chat_text(payload: bytes) -> str:
    """Read the text of the user's own ``Chat`` or ``Shout`` (the first string).

    Args:
        payload: Packet payload (without header).

    Returns:
        The chat text.

    Raises:
        ProtocolError: If the packet is short.
    """
    return Reader(payload).string()


def is_command(text: str) -> bool:
    """Check whether chat text is the ``:checkuser`` command.

    Args:
        text: The chat text.

    Returns:
        ``True`` if the first word is ``:checkuser`` (any case). ``:checkusers`` is not.
    """
    words = text.split()
    return bool(words) and words[0].casefold() == COMMAND


def payload_of(packet: Any) -> bytes:
    """Get the payload bytes of a G-Python ``HPacket``.

    Args:
        packet: The packet.

    Returns:
        The bytes after the 6-byte header.
    """
    return bytes(packet.bytearray)[PAYLOAD_START:]


# --------------------------------------------------------------------------------------
# Guard: no exception may leave a G-Python callback; bad packets are saved once
# --------------------------------------------------------------------------------------

_reported: Counter[str] = Counter()
_dump_dir: Path | None = None
_dump_lock = threading.Lock()


def configure_dump_dir(path: Path | None) -> None:
    """Set the folder for saved bad packets.

    Args:
        path: The folder, or ``None`` to turn saving off.
    """
    global _dump_dir
    _dump_dir = path


def report_bad_packet(name: str, raw: bytes, problem: str) -> None:
    """Log the first bad packet per name and save it as hex. Later ones go to debug.

    Decode a saved file with the skill's ``habbo_packets.py decode --file ...``.

    Args:
        name: Packet name.
        raw: The whole packet bytes.
        problem: What did not match.
    """
    with _dump_lock:
        _reported[name] += 1
        first = _reported[name] == 1
    if not first:
        log.debug("%s packet did not match (%s)", name, problem)
        return
    saved = None
    if _dump_dir is not None and raw:
        try:
            _dump_dir.mkdir(parents=True, exist_ok=True)
            saved = _dump_dir / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.hex"
            lines = [f"# {name}: {problem}", f"# {len(raw)} bytes"]
            lines += [raw[i : i + 32].hex(" ") for i in range(0, len(raw), 32)]
            saved.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            log.debug("could not save bad packet: %s", exc)
            saved = None
    log.warning(
        "%s packet did not match the expected layout (%s); length=%d%s",
        name,
        problem,
        len(raw),
        f" saved={saved}" if saved else "",
    )


def reset_reports() -> None:
    """Forget which bad packets were reported (call on each new game connection)."""
    with _dump_lock:
        _reported.clear()


def guarded(name: str, fn: Callable[[HMessage], None]) -> Callable[[HMessage], None]:
    """Wrap an intercept callback so no exception reaches G-Python's packet thread.

    Args:
        name: Packet name, used when a bad packet is saved.
        fn: The callback.

    Returns:
        A wrapper that never raises.
    """

    def wrapper(message: HMessage) -> None:
        try:
            fn(message)
        except ProtocolError as exc:
            report_bad_packet(name, bytes(getattr(message.packet, "bytearray", b"")), str(exc))
        except Exception:
            log.exception("handler for %s failed", name)

    wrapper.__name__ = f"guarded_{name}"
    return wrapper


# --------------------------------------------------------------------------------------
# Room state
# --------------------------------------------------------------------------------------


def display_now() -> datetime:
    """Return the current time in the display zone (GMT+8)."""
    return datetime.now(DISPLAY_TZ)


class RoomState:
    """Thread-safe record of the room the user is in.

    Packet callbacks change it. Other threads read copies with :meth:`view`. Every real
    change bumps ``version`` and sets ``changed``.
    """

    def __init__(self, clock: Callable[[], datetime] = display_now) -> None:
        """Start with no room.

        Args:
            clock: Returns the current local time (tests pass a fake).
        """
        self._clock = clock
        self._lock = threading.Lock()
        self._users: dict[int, Occupant] = {}
        self._room_id: int | None = None
        self._room_name = ""
        self._names: dict[int, str] = {}
        self.version = 0
        self.changed = threading.Event()

    def _bump(self) -> None:
        self.version += 1
        self.changed.set()

    def enter_room(self, room_id: int) -> None:
        """Handle ``RoomReady``: start a new, empty room.

        Args:
            room_id: The room id.
        """
        with self._lock:
            name = self._names.get(room_id, "")
            if (room_id, name) == (self._room_id, self._room_name) and not self._users:
                return
            self._room_id, self._room_name = room_id, name
            self._users = {}
            self._bump()

    def leave_room(self) -> None:
        """Handle ``CloseConnection`` or a new game connection: no room, no users."""
        with self._lock:
            if self._room_id is None and not self._users:
                return
            self._room_id, self._room_name = None, ""
            self._users = {}
            self._bump()

    def remember_room_name(self, room_id: int, name: str) -> None:
        """Handle ``GetGuestRoomResult``: remember a room name, update the current room.

        Args:
            room_id: The room id.
            name: The room name.
        """
        with self._lock:
            self._names.pop(room_id, None)
            self._names[room_id] = name
            while len(self._names) > ROOM_NAME_CACHE:
                self._names.pop(next(iter(self._names)))
            if room_id == self._room_id and name != self._room_name:
                self._room_name = name
                self._bump()

    def add_players(self, players: Iterable[Player]) -> None:
        """Handle ``Users``: add or update users.

        Args:
            players: Users from the packet.
        """
        with self._lock:
            changed = False
            for player in players:
                known = self._users.get(player.index)
                if known is not None and known.name == player.name:
                    continue
                self._users[player.index] = Occupant(name=player.name, seen_since=self._clock())
                changed = True
            if changed:
                self._bump()

    def remove(self, index: int) -> None:
        """Handle ``UserRemove``: a user left.

        Args:
            index: Room index of the user.
        """
        with self._lock:
            if self._users.pop(index, None) is not None:
                self._bump()

    def view(self) -> RoomView:
        """Copy the current state.

        Returns:
            A frozen copy, sorted by name.
        """
        with self._lock:
            occupants = sorted(self._users.values(), key=lambda o: (o.name.casefold(), o.name))
            return RoomView(
                room_id=self._room_id,
                room_name=self._room_name,
                occupants=tuple(occupants),
            )


# --------------------------------------------------------------------------------------
# Tables and files
# --------------------------------------------------------------------------------------


def plural(count: int, word: str) -> str:
    """Format a count with a regular English noun (``1 user``, ``2 users``).

    Args:
        count: The number.
        word: The singular noun.

    Returns:
        The count and the noun.
    """
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def relative_time(then: datetime, now: datetime) -> str:
    """Describe how long ago ``then`` was, with the two largest units.

    Examples: ``34 seconds ago``, ``3 mins ago``, ``1 hour 3 mins ago``, ``2 hours ago``,
    ``1 day 2 hours ago``. Under 5 seconds (or if the clock went back) it says
    ``just now``: a count of 1 or 2 seconds would be old before the file is read.

    Args:
        then: The earlier time (any aware time).
        now: The current time (any aware time).

    Returns:
        The text.
    """
    seconds = int((now - then).total_seconds())
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{plural(seconds, 'second')} ago"
    days, rest = divmod(seconds // 60, 24 * 60)
    hours, mins = divmod(rest, 60)
    units = [(days, "day"), (hours, "hour"), (mins, "min")]
    start = next(k for k, (n, _) in enumerate(units) if n)  # 60 s or more: one unit is not 0
    parts = [plural(n, word) for n, word in units[start : start + 2] if n]
    return " ".join(parts) + " ago"


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, ending with ``…`` when it was cut.

    Args:
        text: The text.
        limit: Most characters to keep. At least 1.

    Returns:
        The text, or its start plus ``…``.
    """
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def build_current_table(view: RoomView, now: datetime, style: box.Box = box.ASCII) -> Table:
    """Build the table of users in the room now.

    Args:
        view: The room state.
        now: Time for the caption.
        style: Rich box style. ``box.ASCII`` stays clean in every editor and font.

    Returns:
        The table.
    """
    title = "Current users"
    if view.room_id is not None:
        # One line only: a title wider than the table wraps, and a taller frame breaks the
        # terminal view (see TITLE_MAX).
        title = shorten(f"Current users - {view.label}", TITLE_MAX)
    stamp = now.astimezone(DISPLAY_TZ).strftime("%H:%M:%S")
    table = Table(
        title=title,
        caption=f"{plural(len(view.names), 'user')} | updated {stamp} GMT+8",
        box=style,
        header_style="bold",
        min_width=TITLE_MAX + 4,
    )
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Name", overflow="fold", min_width=12)
    table.add_column("Seen since (GMT+8)", no_wrap=True)
    table.add_column("Ago", no_wrap=True)
    if view.room_id is None:
        table.add_row("", "(not in a room)", "", "")
    elif not view.occupants:
        table.add_row("", "(no users yet)", "", "")
    else:
        by_name = sorted(view.occupants, key=lambda o: (o.name.casefold(), o.name))
        for number, occupant in enumerate(by_name, start=1):
            seen = occupant.seen_since.astimezone(DISPLAY_TZ).strftime("%H:%M:%S")
            table.add_row(str(number), occupant.name, seen, relative_time(occupant.seen_since, now))
    return table


def build_history_table(
    rows: Iterable[Snapshot], now: datetime, style: box.Box = box.ASCII
) -> Table:
    """Build the history table, oldest row first.

    Args:
        rows: Snapshots to show.
        now: Time for the caption.
        style: Rich box style.

    Returns:
        The table.
    """
    shown = list(rows)
    table = Table(
        title="User count history",
        caption=(
            f"{len(shown)} {'snapshot' if len(shown) == 1 else 'snapshots'} shown | generated "
            f"{now.astimezone(DISPLAY_TZ).strftime('%Y-%m-%d %H:%M:%S')} GMT+8"
        ),
        box=style,
        header_style="bold",
        show_lines=False,
        min_width=60,  # wide enough for the caption on one line
    )
    table.add_column("Time (GMT+8)", no_wrap=True)
    table.add_column("Ago", no_wrap=True)
    table.add_column("Room", overflow="ellipsis", no_wrap=True, max_width=26)
    table.add_column("Count", justify="right", no_wrap=True)
    if not shown:
        table.add_row("", "", "(no snapshots yet)", "")
    for row in shown:
        table.add_row(
            row.taken_at.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"),
            relative_time(row.taken_at, now),
            row.room_name,
            str(row.count),
        )
    return table


def render_text(table: Table, width: int) -> str:
    """Render a rich table to plain text (no colour codes).

    Args:
        table: The table.
        width: Width in characters.

    Returns:
        The text, ending with a newline.
    """
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=width,
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
        highlight=False,
    )
    console.print(table)
    return buffer.getvalue()


def write_atomic(path: Path, text: str) -> bool:
    """Write a text file so a reader never sees half of it.

    Args:
        path: Target file.
        text: New content (UTF-8).

    Returns:
        ``True`` on success. ``False`` if the OS refused (for example, Windows with the
        file locked by another program). The next write tries again.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return False
    return True


class HistoryStore:
    """Snapshots on disk (JSON Lines) and the newest ones in memory."""

    def __init__(self, path: Path, max_rows: int | None = None) -> None:
        """Load the snapshots from ``path`` (a missing file is fine).

        Args:
            path: The ``snapshots.jsonl`` file.
            max_rows: How many newest snapshots to keep in memory. ``None`` keeps all of them
                (a day is 288 small rows, so memory stays small).
        """
        self.path = path
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._rows: list[Snapshot] = []
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("could not read %s: %s", self.path, exc)
            return
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                self._rows.append(Snapshot.model_validate_json(line))
            except ValidationError:
                log.warning("%s line %d is not a valid snapshot; skipped", self.path, number)
        self._rows = self._trim(self._rows)

    def _trim(self, rows: list[Snapshot]) -> list[Snapshot]:
        return rows if self.max_rows is None else rows[-self.max_rows :]

    def append(self, snapshot: Snapshot) -> None:
        """Save a snapshot to disk and memory.

        Args:
            snapshot: The snapshot.

        Raises:
            OSError: If the file cannot be written.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(snapshot.model_dump_json() + "\n")
        with self._lock:
            self._rows = self._trim([*self._rows, snapshot])

    def recent(self, limit: int | None = None) -> list[Snapshot]:
        """Return snapshots, oldest first.

        Args:
            limit: Return only this many of the newest. ``None`` returns all.

        Returns:
            A copy of the list.
        """
        with self._lock:
            return list(self._rows if limit is None else self._rows[-limit:])


class TableFiles:
    """Writes both table files, and the terminal view when there is one."""

    def __init__(
        self,
        settings: Settings,
        state: RoomState,
        store: HistoryStore,
        clock: Callable[[], datetime] = display_now,
        live: Live | None = None,
    ) -> None:
        """Wire up the sources and targets.

        Args:
            settings: Run settings.
            state: Room state (source of table 1).
            store: Snapshots (source of table 2).
            clock: Returns the current local time.
            live: Optional rich ``Live`` view showing both tables stacked (only when
                stdout is a terminal).
        """
        self.settings = settings
        self.state = state
        self.store = store
        self.clock = clock
        self.live = live
        self.current_writes = 0

    def write_current(self) -> None:
        """Rewrite ``current_users.txt`` and update the terminal view."""
        now = self.clock()
        view = self.state.view()
        text = render_text(build_current_table(view, now), self.settings.table_width)
        if write_atomic(self.settings.current_path, text):
            self.current_writes += 1
        self.update_live(view, now)

    def write_history(self) -> None:
        """Rewrite ``history.txt`` (every snapshot) and update the terminal view."""
        now = self.clock()
        table = build_history_table(self.store.recent(), now)
        write_atomic(self.settings.history_path, render_text(table, self.settings.table_width))
        self.update_live(self.state.view(), now)

    def update_live(self, view: RoomView, now: datetime) -> None:
        """Redraw the terminal view: current users on top, history below.

        The history shows only as many rows as fit under the user table, so the screen
        never scrolls. The file holds every snapshot.

        Args:
            view: Current room view.
            now: Time for the captions and "Ago" values.
        """
        if self.live is None:
            return
        # 6 lines (title, 2 borders, header line, header rule, caption) for each table.
        fit = self.live.console.height - len(view.occupants) - 12 - LIVE_MARGIN
        shown = min(self.settings.terminal_rows, max(fit, MIN_LIVE_HISTORY_ROWS))
        rows = self.store.recent(shown)
        self.live.update(
            Group(
                build_current_table(view, now, box.ROUNDED),
                build_history_table(rows, now, box.ROUNDED),
            ),
            refresh=True,
        )

    def write_all(self) -> None:
        """Rewrite both files."""
        self.write_current()
        self.write_history()


# --------------------------------------------------------------------------------------
# Threads: live writer, 5-minute recorder, file opener
# --------------------------------------------------------------------------------------


def next_mark(now: datetime, interval_minutes: int, tz: tzinfo | None = DISPLAY_TZ) -> datetime:
    """Find the next clock mark strictly after ``now``.

    A mark is a time on the clock of ``tz`` with ``minute % interval == 0`` and second 0.
    The search runs in UTC, so a daylight-saving change cannot produce a time that does
    not exist.

    Args:
        now: Any aware time.
        interval_minutes: Minutes between marks.
        tz: Time zone for the clock (default: GMT+8; ``None`` means this computer's).

    Returns:
        The next mark as an aware time in ``tz``.
    """
    utc = now.astimezone(UTC).replace(second=0, microsecond=0)
    while True:
        utc += timedelta(minutes=1)
        local = utc.astimezone(tz)
        if local.minute % interval_minutes == 0:
            return local


class LiveWriter(threading.Thread):
    """Rewrites ``current_users.txt`` after every change, merging bursts of changes.

    While the user is in a room it also rewrites ``current_users.txt`` every
    ``refresh_seconds``, so its "Ago" column does not go stale. ``history.txt`` changes
    only at each snapshot and on ``:checkuser``.
    """

    def __init__(
        self, files: TableFiles, stop: threading.Event, refresh_seconds: float = 30.0
    ) -> None:
        """Create the thread (call ``start()`` yourself).

        Args:
            files: The table writer.
            stop: Set to end the thread.
            refresh_seconds: Seconds between rewrites when nothing changed.
        """
        super().__init__(name="live-writer", daemon=True)
        self.files = files
        self.stop_event = stop
        self.refresh_seconds = refresh_seconds

    def run(self) -> None:
        """Wait for changes; wait a short time for more; write once. Refresh on a timer."""
        state = self.files.state
        last = time.monotonic()
        while not self.stop_event.is_set():
            if not state.changed.wait(timeout=0.5):
                if time.monotonic() - last >= self.refresh_seconds:
                    last = time.monotonic()
                    if state.view().room_id is not None:
                        self._write(self.files.write_current)
                continue
            if self.stop_event.wait(self.files.settings.burst_seconds):
                break
            state.changed.clear()
            last = time.monotonic()
            self._write(self.files.write_current)

    @staticmethod
    def _write(action: Callable[[], None]) -> None:
        """Run a write and log (not raise) any error."""
        try:
            action()
        except Exception:
            log.exception("could not write the tables")


class Recorder(threading.Thread):
    """Saves a snapshot at every clock mark while the user is in a room."""

    LATE_SECONDS = 60

    def __init__(
        self,
        files: TableFiles,
        stop: threading.Event,
        clock: Callable[[], datetime] = display_now,
    ) -> None:
        """Create the thread (call ``start()`` yourself).

        Args:
            files: The table writer (also holds settings, state and store).
            stop: Set to end the thread.
            clock: Returns the current local time.
        """
        super().__init__(name="recorder", daemon=True)
        self.files = files
        self.stop_event = stop
        self.clock = clock

    def take(self, mark: datetime) -> Snapshot | None:
        """Save one snapshot, if the user is in a room.

        Args:
            mark: The clock mark that was due.

        Returns:
            The snapshot, or ``None`` if the user is not in a room.
        """
        view = self.files.state.view()
        if view.room_id is None:
            log.info("%s: not in a room; no snapshot", mark.strftime("%H:%M"))
            return None
        now = self.clock()
        late = (now - mark).total_seconds()
        if late > self.LATE_SECONDS:
            # The computer slept or the thread stalled. The state is not the state at the
            # mark, so record the real time instead of pretending.
            log.warning("mark %s missed by %.0f s; recording the real time", mark, late)
            mark = now.replace(microsecond=0)
        snapshot = Snapshot.from_view(view, mark)
        self.files.store.append(snapshot)
        self.files.write_history()
        log.info(
            "snapshot %s: %d users in %s",
            mark.strftime("%H:%M"),
            snapshot.count,
            snapshot.room_name,
        )
        return snapshot

    def run(self) -> None:
        """Sleep to each mark, then take a snapshot. Never raises."""
        interval = self.files.settings.interval_minutes
        while not self.stop_event.is_set():
            mark = next_mark(self.clock(), interval)
            while not self.stop_event.is_set():
                remaining = (mark - self.clock()).total_seconds()
                if remaining <= 0:
                    break
                # Short waits: after the computer sleeps, the wall clock jumps ahead.
                self.stop_event.wait(min(remaining, 15))
            if self.stop_event.is_set():
                break
            try:
                self.take(mark)
            except Exception:
                log.exception("could not take a snapshot")


def open_files(paths: list[Path]) -> None:
    """Open files with the default app of this OS.

    The paths come from the settings, never from chat text. No shell is used.

    Args:
        paths: Files to open.
    """
    if sys.platform == "darwin":
        subprocess.Popen(["open", *map(str, paths)])  # noqa: S603, S607
    elif sys.platform == "win32":
        for path in paths:
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    else:
        opener = shutil.which("xdg-open")
        if opener is None:
            raise OSError("xdg-open not found")
        for path in paths:
            subprocess.Popen([opener, str(path)])  # noqa: S603


class Opener(threading.Thread):
    """Opens the table files when the user types ``:checkuser`` (at most once per 2 s)."""

    GAP_SECONDS = 2.0

    def __init__(
        self,
        files: TableFiles,
        stop: threading.Event,
        launch: Callable[[list[Path]], None] = open_files,
        gap: float = GAP_SECONDS,
    ) -> None:
        """Create the thread (call ``start()`` yourself).

        Args:
            files: The table writer (makes sure the files exist).
            stop: Set to end the thread.
            launch: Opens the files (tests pass a fake).
            gap: Seconds to wait after each open.
        """
        super().__init__(name="opener", daemon=True)
        self.files = files
        self.stop_event = stop
        self.launch = launch
        self.gap = gap
        self._requests: queue.Queue[None] = queue.Queue(maxsize=1)

    def request(self) -> None:
        """Ask for the files to be opened. Never blocks; extra requests are dropped."""
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            log.debug("open request dropped: one is already waiting")

    def run(self) -> None:
        """Serve open requests."""
        settings = self.files.settings
        while not self.stop_event.is_set():
            try:
                self._requests.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.files.write_all()  # fresh content, and the files exist
                self.launch([settings.current_path, settings.history_path])
            except Exception:
                log.exception("could not open the table files")
            self.stop_event.wait(self.gap)


# --------------------------------------------------------------------------------------
# The extension
# --------------------------------------------------------------------------------------


class App:
    """Registers the intercepts and connects packets to the room state."""

    def __init__(self, ext: Any, state: RoomState, opener: Opener) -> None:
        """Wire up. Nothing happens until :meth:`register` and a game connection.

        Args:
            ext: The G-Python ``Extension`` (or a fake in tests).
            state: Room state to update.
            opener: Opens the files for ``:checkuser``.
        """
        self.ext = ext
        self.state = state
        self.opener = opener

    def register(self) -> None:
        """Register every intercept and event. Call before ``ext.start()``."""
        to_server, to_client = Direction.TO_SERVER, Direction.TO_CLIENT
        pairs = [
            (to_server, "Chat", self.on_chat),
            (to_server, "Shout", self.on_chat),
            (to_client, "Users", self.on_users),
            (to_client, "UserRemove", self.on_user_remove),
            (to_client, "RoomReady", self.on_room_ready),
            (to_client, "CloseConnection", self.on_close),
            (to_client, "GetGuestRoomResult", self.on_guest_room),
        ]
        for direction, name, handler in pairs:
            self.ext.intercept(direction, guarded(name, handler), name)
        self.ext.on_event("connection_start", self.on_connection_start)
        self.ext.on_event("connection_end", self.on_connection_end)

    def known(self, direction: Direction, name: str) -> bool:
        """Check whether G-Earth's packet catalogue for this client has ``name``.

        Args:
            direction: Packet direction.
            name: Packet name.

        Returns:
            ``True`` if the name is known.
        """
        infos = getattr(self.ext, "packet_infos", None) or {}
        return name in infos.get(direction, {})

    def notice(self, text: str, color: str = "black") -> None:
        """Write a line to G-Earth's extension console and the log.

        Args:
            text: The line.
            color: Console colour name.
        """
        log.info("notice: %s", text)
        try:
            self.ext.write_to_console(text, color)
        except Exception:
            log.debug("console write failed", exc_info=True)

    # --- events ---------------------------------------------------------------------

    def on_connection_start(self) -> None:
        """The game connected through G-Earth: reset state and check packet names."""
        try:
            reset_reports()
            self.state.leave_room()
            info = getattr(self.ext, "connection_info", None) or {}
            log.info("connected to %s (%s)", info.get("host"), info.get("hotel_version"))
            missing = [
                f"{'out' if d == Direction.TO_SERVER else 'in'}:{n}"
                for d, names in NEEDS.items()
                for n in names
                if not self.known(d, n)
            ]
            if missing:
                self.notice(f"Unknown packet names for this client: {', '.join(missing)}", "red")
            else:
                self.notice(f"Checkuser ready. Type {COMMAND} to open the tables.", "blue")
        except Exception:
            log.exception("connection_start handler failed")

    def on_connection_end(self) -> None:
        """The game disconnected: forget the room."""
        try:
            self.state.leave_room()
            log.info("disconnected from the hotel")
        except Exception:
            log.exception("connection_end handler failed")

    # --- packets (fast: parse, update state, return) --------------------------------

    def on_chat(self, message: HMessage) -> None:
        """The user's own chat or shout: block ``:checkuser`` and open the files.

        Args:
            message: The intercepted packet.
        """
        if is_command(parse_chat_text(payload_of(message.packet))):
            message.is_blocked = True
            self.opener.request()

    def on_users(self, message: HMessage) -> None:
        """People entered the room.

        Args:
            message: The intercepted ``Users`` packet.

        Raises:
            ProtocolError: If no layout matched. The players read before the problem are
                still added.
        """
        result = parse_users(payload_of(message.packet))
        self.state.add_players(result.players)
        if result.error is not None:
            raise ProtocolError(result.error)

    def on_user_remove(self, message: HMessage) -> None:
        """Someone left the room.

        Args:
            message: The intercepted ``UserRemove`` packet.
        """
        self.state.remove(parse_user_remove(payload_of(message.packet)))

    def on_room_ready(self, message: HMessage) -> None:
        """The user entered a room.

        Args:
            message: The intercepted ``RoomReady`` packet.
        """
        self.state.enter_room(parse_room_ready(payload_of(message.packet)))

    def on_close(self, message: HMessage) -> None:
        """The user left the room.

        Args:
            message: The intercepted ``CloseConnection`` packet.
        """
        self.state.leave_room()

    def on_guest_room(self, message: HMessage) -> None:
        """The client received room info: remember the room name.

        Args:
            message: The intercepted ``GetGuestRoomResult`` packet.
        """
        room_id, name = parse_guest_room(payload_of(message.packet))
        self.state.remember_room_name(room_id, name)


# --------------------------------------------------------------------------------------
# G-Earth install, logging, and the command line
# --------------------------------------------------------------------------------------

MAC_GEARTH_DIR = Path("~/Library/Application Support/G-Earth").expanduser()
FOLDER_PREFIX = "Checkuser_"
DS_STORE = ".DS_Store"


def build_command(uv_path: str, script: Path) -> list[str]:
    """Build the argument list G-Earth runs (``command.txt``).

    G-Earth gives its programs no shell ``PATH``, so every path is absolute.

    Args:
        uv_path: Absolute path of ``uv``.
        script: Absolute path of this file.

    Returns:
        The argv with G-Earth's ``{cookie}``, ``{port}`` and ``{filename}`` placeholders.
    """
    return [
        uv_path, "run", "--script", str(script),
        "-c", "{cookie}", "-p", "{port}", "-f", "{filename}",
    ]  # fmt: skip


def install(gearth_dir: Path | None, dry_run: bool, uninstall: bool) -> int:
    """Write (or remove) G-Earth's ``Extensions/Checkuser_<version>/command.txt``.

    Args:
        gearth_dir: G-Earth data folder, or ``None`` for the macOS default.
        dry_run: Print what would happen and change nothing.
        uninstall: Remove instead of write.

    Returns:
        Process exit code.
    """
    base = gearth_dir or (MAC_GEARTH_DIR if sys.platform == "darwin" else None)
    if base is None:
        print("Pass --gearth-dir: the folder with G-Earth.exe or G-Earth.jar.", file=sys.stderr)
        return 1
    extensions = base.expanduser().resolve() / "Extensions"
    if not extensions.is_dir():
        print(f"{extensions} not found; start G-Earth once or check --gearth-dir", file=sys.stderr)
        return 1
    old = sorted(p for p in extensions.iterdir() if p.is_dir() and p.name.startswith(FOLDER_PREFIX))
    if uninstall:
        for folder in old:
            print(f"{'would remove' if dry_run else 'removing'} {folder}")
            if not dry_run:
                shutil.rmtree(folder)
        return 0
    uv_path = shutil.which("uv")
    if uv_path is None:
        print("uv not found on PATH. Install uv first.", file=sys.stderr)
        return 1
    # Do not resolve symlinks: a Homebrew upgrade removes the versioned Cellar path.
    command = build_command(str(Path(uv_path).absolute()), Path(__file__).resolve())
    target = extensions / f"{FOLDER_PREFIX}{__version__}"
    content = json.dumps(command, separators=(",", ":"))
    print(f"{'would write' if dry_run else 'writing'} {target / 'command.txt'}:\n  {content}")
    if dry_run:
        return 0
    for folder in old:
        if folder != target:
            shutil.rmtree(folder)
    (target / "extension").mkdir(parents=True, exist_ok=True)
    (target / "command.txt").write_text(content, encoding="utf-8")
    print("Done. Restart G-Earth, then restart the Habbo client through it.")
    return 0


def setup_logging(data_dir: Path, debug: bool) -> None:
    """Log to ``checkuser.log`` (rotated) and stderr, and set the bad-packet folder.

    Args:
        data_dir: The data folder.
        debug: Log debug lines too.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        data_dir / "checkuser.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream)
    configure_dump_dir(data_dir / "bad-packets")


def start_extension(ext: Any, stop: threading.Event) -> bool:
    """Run ``ext.start()`` with a way out if G-Earth refuses the connection.

    g-python 0.1.6 waits in ``start()`` on a barrier until G-Earth sends INIT. If the user
    declines the "allow this extension" prompt, the socket closes and ``start()`` would wait
    forever. This watches for that and breaks the barrier. It uses private attributes of
    0.1.6: check them again if you change the pin.

    Args:
        ext: The G-Python extension.
        stop: Set when the process should end.

    Returns:
        ``True`` if connected to G-Earth.

    Raises:
        BaseException: Whatever ``ext.start()`` raised.
    """
    errors: list[BaseException] = []

    def run() -> None:
        try:
            ext.start()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            errors.append(exc)

    thread = threading.Thread(target=run, name="gearth-start", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(0.5)
        sock = getattr(ext, "_Extension__sock", None)
        if stop.is_set() or (sock is not None and ext.is_closed()):
            barrier = getattr(ext, "_Extension__start_barrier", None)
            if barrier is not None:
                barrier.abort()
            thread.join(1.0)
            return False
    if errors:
        raise errors[0]
    return not ext.is_closed()


def exit_now(code: int) -> None:
    """Flush logs and end the process even if g-python's threads are still running.

    Args:
        code: Process exit code.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    logging.shutdown()
    os._exit(code)


def demo_snapshots(now: datetime) -> list[Snapshot]:
    """Make sample history rows (fake names) to check the table layout.

    Args:
        now: Time of the last row.

    Returns:
        Sample snapshots.
    """
    sample = [
        ["Alice", "bob", "Charlie"],
        ["Alice", "bob", "Charlie", "Dora", "Eve", "Frank", "Grace", "Heidi", "Ivan", "Judy"],
        [],
    ]
    rows = []
    for i, names in enumerate(sample):
        names = sorted(names, key=lambda n: (n.casefold(), n))
        when = (now - timedelta(minutes=5 * (len(sample) - 1 - i))).replace(second=0, microsecond=0)
        rows.append(
            Snapshot(
                taken_at=when,
                room_id=1,
                room_name="Demo room with a rather long name",
                count=len(names),
                names=names,
            )
        )
    return rows


class ConsoleLogHandler(logging.Handler):
    """Prints log lines above the live tables, through the same rich console."""

    def __init__(self, console: Console) -> None:
        """Create the handler.

        Args:
            console: The console that owns the ``Live`` view.
        """
        super().__init__()
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        """Print one log line above the live view."""
        try:
            self.console.print(self.format(record), markup=False, highlight=False, soft_wrap=True)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


def route_logs_through(console: Console) -> None:
    """Swap the plain stderr log handler for one that is safe next to a ``Live`` view.

    A raw write to the terminal while ``Live`` is drawing shifts the screen by one line.
    ``Live`` then erases the wrong lines and leaves old frames behind.

    Args:
        console: The console that owns the ``Live`` view.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if type(handler) is logging.StreamHandler:  # not the file handlers (subclasses)
            root.removeHandler(handler)
            safe = ConsoleLogHandler(console)
            safe.setFormatter(handler.formatter)
            root.addHandler(safe)


def run_extension(settings: Settings, gearth_args: list[str]) -> int:
    """Connect to G-Earth and run until it closes the connection.

    Args:
        settings: Run settings.
        gearth_args: ``-p``, ``-c``, ``-f`` arguments for G-Python.

    Returns:
        Process exit code.
    """
    state = RoomState()
    store = HistoryStore(settings.snapshots_path)
    stop = threading.Event()
    live = None
    if sys.stdout.isatty():
        console = Console()
        # "crop": a frame taller than the window is cut, never scrolled (a scrolled frame
        # leaves its top lines behind on every redraw).
        live = Live(console=console, auto_refresh=False, vertical_overflow="crop")
        live.start()
        route_logs_through(console)
    files = TableFiles(settings, state, store, live=live)
    files.write_all()  # the files exist from the start
    opener = Opener(files, stop)
    threads = [LiveWriter(files, stop), Recorder(files, stop), opener]

    ext = Extension(EXTENSION_INFO, gearth_args, silent=True)
    App(ext, state, opener).register()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        if not start_extension(ext, stop):
            log.warning("G-Earth refused or closed the connection")
            return 1
    except OSError as exc:
        log.error("cannot reach G-Earth: %s", exc)
        return 1
    log.info("connected to G-Earth")
    for thread in threads:
        thread.start()
    while not stop.is_set() and not ext.is_closed():
        stop.wait(0.5)
    stop.set()
    if live is not None:
        live.stop()
    log.info("bye")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run the chosen mode.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(prog="checkuser", description=EXTENSION_INFO["description"])
    parser.add_argument("-p", "--port", help="G-Earth's extension port")
    parser.add_argument("-c", "--auth-token", dest="cookie", help="G-Earth's cookie")
    parser.add_argument("-f", "--filename", help="extension folder name")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--interval", type=int, default=5, help="minutes between snapshots")
    parser.add_argument("--show", action="store_true", help="print the history table and exit")
    parser.add_argument("--demo", action="store_true", help="print both tables with sample data")
    parser.add_argument("--install", action="store_true", help="write G-Earth's command.txt")
    parser.add_argument("--uninstall", action="store_true", help="remove G-Earth's command.txt")
    parser.add_argument("--dry-run", action="store_true", help="with --install: change nothing")
    parser.add_argument("--gearth-dir", type=Path, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.install or args.uninstall:
        return install(args.gearth_dir, args.dry_run, args.uninstall)
    try:
        settings = Settings(
            interval_minutes=args.interval, data_dir=args.data_dir.expanduser().resolve()
        )
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 2

    console = Console()
    now = display_now()
    if args.demo:
        rows = demo_snapshots(now)
        state = RoomState()
        state.enter_room(1)
        state.remember_room_name(1, "Demo room with a rather long name")
        state.add_players(Player(i, n, i) for i, n in enumerate(rows[1].names))
        console.print(build_current_table(state.view(), now, box.ROUNDED))
        console.print(build_history_table(rows, now, box.ROUNDED))
        return 0
    if args.show:
        store = HistoryStore(settings.snapshots_path)
        console.print(build_history_table(store.recent(settings.terminal_rows), now, box.ROUNDED))
        return 0
    if not args.port:
        parser.error("-p/--port is required when G-Earth starts this script")

    setup_logging(settings.data_dir, args.debug)
    log.info("Checkuser %s starting (port %s)", __version__, args.port)
    gearth_args = ["-p", str(args.port)]
    if args.cookie:
        gearth_args += ["-c", args.cookie]
    if args.filename:
        gearth_args += ["-f", args.filename]
    return run_extension(settings, gearth_args)


if __name__ == "__main__":
    exit_now(main())
