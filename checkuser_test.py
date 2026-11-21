"""Tests for checkuser.py. They need no Habbo and no G-Earth."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import checkuser as cu

# --- packet byte builders -------------------------------------------------------------


def s(text: str) -> bytes:
    data = text.encode("utf-8")
    return len(data).to_bytes(2, "big") + data


def i(n: int) -> bytes:
    return n.to_bytes(4, "big", signed=True)


def b(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def head(uid: int, name: str, index: int, kind: int) -> bytes:
    return (
        i(uid) + s(name) + s("motto") + s("figure") + i(index)
        + i(1) + i(2) + s("0.0") + i(2) + i(kind)
    )  # fmt: skip


def player(uid: int, name: str, index: int, live: bool = True) -> bytes:
    tail = s("M") + i(0) + i(0) + s("") + s("") + i(0) + b(False)
    return head(uid, name, index, 1) + tail + (i(-1) if live else b"")


def pet(uid: int, name: str, index: int) -> bytes:
    tail = i(0) + i(5) + s("owner") + i(1) + b"".join(b(False) for _ in range(6)) + i(1) + s("std")
    return head(uid, name, index, 2) + tail


def user_bot(uid: int, name: str, index: int) -> bytes:
    return head(uid, name, index, 4) + s("M") + i(5) + s("owner") + i(2) + bytes(4)


def old_bot(uid: int, name: str, index: int) -> bytes:
    return head(uid, name, index, 3)


def users(*entries: bytes) -> bytes:
    return i(len(entries)) + b"".join(entries)


NOW = datetime(2026, 11, 21, 12, 3, 30, tzinfo=UTC)


def mkview(names: list[str], room_id: int | None = 7, room_name: str = "Lobby") -> cu.RoomView:
    return cu.RoomView(
        room_id=room_id,
        room_name=room_name,
        occupants=tuple(cu.Occupant(name=n, seen_since=NOW) for n in names),
    )


def settings(tmp_path: Path, **kw: object) -> cu.Settings:
    return cu.Settings(data_dir=tmp_path, burst_seconds=0.2, **kw)  # type: ignore[arg-type]


# --- Users parsing --------------------------------------------------------------------


def test_users_keeps_only_players() -> None:
    payload = users(
        player(1, "Alice", 10),
        pet(2, "Rex", 11),
        user_bot(3, "Botty", 12),
        old_bot(4, "Oldie", 13),
        player(5, "bob", 14),
    )
    result = cu.parse_users(payload)
    assert result.error is None
    assert [p.name for p in result.players] == ["Alice", "bob"]
    assert [p.index for p in result.players] == [10, 14]


def test_users_documented_tail_without_extra_int() -> None:
    payload = users(player(1, "Alice", 10, live=False), player(2, "Bob", 11, live=False))
    result = cu.parse_users(payload)
    assert result.error is None
    assert [p.name for p in result.players] == ["Alice", "Bob"]


def test_users_empty_and_utf8() -> None:
    assert cu.parse_users(users()).players == ()
    assert cu.parse_users(users(player(1, "Zoë", 1))).players[0].name == "Zoë"


def test_users_truncated_keeps_earlier_players_and_reports() -> None:
    payload = users(player(1, "Alice", 10), player(2, "Bob", 11))[:-5]
    result = cu.parse_users(payload)
    assert result.error is not None
    assert [p.name for p in result.players] == ["Alice"]


def test_users_unknown_type_and_bad_count() -> None:
    assert "unknown entity type" in (cu.parse_users(users(head(1, "X", 1, 9))).error or "")
    assert "implausible" in (cu.parse_users(i(5000)).error or "")
    assert cu.parse_users(b"").error is not None


def test_users_trailing_bytes_are_an_error() -> None:
    result = cu.parse_users(users(player(1, "Alice", 10)) + b"\x00\x00")
    assert result.error is not None
    assert [p.name for p in result.players] == ["Alice"]


def test_other_parsers() -> None:
    assert cu.parse_user_remove(s("42")) == 42
    with pytest.raises(cu.ProtocolError):
        cu.parse_user_remove(s("abc"))
    assert cu.parse_room_ready(s("model_a") + i(99)) == 99
    assert cu.parse_guest_room(b(True) + i(99) + s("My Room") + b"junk") == (99, "My Room")
    with pytest.raises(cu.ProtocolError):
        cu.parse_guest_room(b(True))
    assert cu.parse_chat_text(s("hello") + i(0) + i(-1)) == "hello"


def test_is_command() -> None:
    assert cu.is_command(":checkuser")
    assert cu.is_command("  :CheckUser  now")
    assert not cu.is_command(":checkusers")
    assert not cu.is_command("hello :checkuser")
    assert not cu.is_command("")


# --- models ---------------------------------------------------------------------------


def test_snapshot_validation_and_utc_round_trip() -> None:
    local = datetime(2026, 11, 21, 20, 5, tzinfo=timezone(timedelta(hours=8)))
    snap = cu.Snapshot(taken_at=local, room_id=1, room_name="R", count=2, names=["a", "B"])
    assert snap.taken_at.utcoffset() == timedelta(0)
    assert snap.taken_at.hour == 12
    again = cu.Snapshot.model_validate_json(snap.model_dump_json())
    assert again == snap
    with pytest.raises(ValidationError):
        cu.Snapshot(taken_at=NOW, room_id=1, room_name="R", count=3, names=["a", "B"])
    with pytest.raises(ValidationError):
        cu.Snapshot(taken_at=NOW, room_id=1, room_name="R", count=2, names=["B", "a"])
    with pytest.raises(ValidationError):
        cu.Snapshot(taken_at=NOW, room_id=1, room_name="R", count=2, names=["a", "a"])
    with pytest.raises(ValidationError):
        cu.Snapshot(taken_at=datetime(2026, 1, 1), room_id=1, room_name="R", count=0, names=[])


def test_snapshot_from_view_sorts_and_rejects_no_room() -> None:
    snap = cu.Snapshot.from_view(mkview(["bob", "Alice", "Charlie"]), NOW)
    assert snap.names == ["Alice", "bob", "Charlie"]
    assert snap.count == 3
    with pytest.raises(ValueError, match="no room"):
        cu.Snapshot.from_view(mkview([], room_id=None), NOW)


def test_room_label_falls_back_to_id() -> None:
    assert mkview([], room_name="").label == "room 7"
    assert mkview([], room_id=None, room_name="").label == ""


def test_settings_interval_must_divide_60(tmp_path: Path) -> None:
    assert settings(tmp_path, interval_minutes=15).interval_minutes == 15
    with pytest.raises(ValidationError):
        settings(tmp_path, interval_minutes=7)
    with pytest.raises(ValidationError):
        settings(tmp_path, interval_minutes=0)


# --- clock marks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ((12, 3, 30), (12, 5)),
        ((12, 5, 0), (12, 10)),  # strictly after
        ((12, 4, 59), (12, 5)),
        ((12, 58, 0), (13, 0)),
        ((23, 57, 10), (0, 0)),  # next day
    ],
)
def test_next_mark(now: tuple[int, int, int], expected: tuple[int, int]) -> None:
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 11, 21, *now, tzinfo=tz)
    mark = cu.next_mark(start, 5, tz)
    assert (mark.hour, mark.minute, mark.second) == (*expected, 0)
    assert mark > start


def test_next_mark_across_spring_forward() -> None:
    london = ZoneInfo("Europe/London")
    start = datetime(2026, 3, 29, 0, 58, tzinfo=london)  # 01:00 GMT does not exist
    mark = cu.next_mark(start, 5, london)
    assert (mark.hour, mark.minute) == (2, 0)
    assert mark.utcoffset() == timedelta(hours=1)
    assert mark.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=2)


def test_next_mark_odd_offset_zone() -> None:
    kathmandu = ZoneInfo("Asia/Kathmandu")  # +05:45
    mark = cu.next_mark(datetime(2026, 11, 21, 12, 0, tzinfo=UTC), 5, kathmandu)
    assert mark.minute % 5 == 0
    assert mark.astimezone(UTC).minute == 5


# --- room state -----------------------------------------------------------------------


def test_room_state_version_changes_only_on_real_change() -> None:
    state = cu.RoomState(clock=lambda: NOW)
    state.enter_room(7)
    base = state.version
    state.add_players([cu.Player(1, "Alice", 100)])
    assert state.version == base + 1
    state.add_players([cu.Player(1, "Alice", 100)])  # same again
    state.remove(555)  # unknown index
    assert state.version == base + 1
    state.add_players([cu.Player(2, "Bob", 101)])
    state.remove(1)  # Player(index, name, user_id): UserRemove uses the room index
    assert state.view().names == ["Bob"]
    state.enter_room(7)  # re-enter: users are cleared (a real change)
    assert state.view().names == []
    here = state.version
    state.enter_room(7)  # nothing changed now
    assert state.version == here


def test_room_name_arrives_before_room_ready() -> None:
    state = cu.RoomState(clock=lambda: NOW)
    state.remember_room_name(7, "Lobby")  # GetGuestRoomResult comes first
    assert state.view().room_id is None
    state.enter_room(7)
    assert state.view().label == "Lobby"
    state.remember_room_name(7, "Lobby 2")  # updates the current room
    assert state.view().label == "Lobby 2"


def test_leave_room_and_name_cache_limit() -> None:
    state = cu.RoomState(clock=lambda: NOW)
    state.enter_room(1)
    state.add_players([cu.Player(1, "A", 1)])
    state.leave_room()
    assert state.view().room_id is None
    assert state.view().names == []
    for n in range(cu.ROOM_NAME_CACHE + 10):
        state.remember_room_name(n, f"r{n}")
    assert len(state._names) == cu.ROOM_NAME_CACHE


# --- tables ---------------------------------------------------------------------------


def lines_of(text: str) -> list[str]:
    return text.rstrip("\n").split("\n")


def test_current_table_text_is_clean() -> None:
    view = mkview(["bob", "Alice", "A" * 40])
    text = cu.render_text(cu.build_current_table(view, NOW), 60)
    assert "\x1b" not in text
    assert text.endswith("\n")
    assert all(len(line) <= 60 for line in lines_of(text))
    assert "Current users - Lobby" in text
    assert "3 users" in text
    assert text.index("Alice") < text.index("bob")
    body = [ln for ln in lines_of(text) if ln.startswith("|")]
    assert len({len(ln) for ln in body}) == 1  # every row has the same width


def test_captions_use_singular_for_one() -> None:
    one = cu.render_text(cu.build_current_table(mkview(["Alice"]), NOW), 80)
    assert "1 user |" in one
    row = cu.Snapshot(taken_at=NOW, room_id=1, room_name="R", count=0, names=[])
    assert "1 snapshot shown" in cu.render_text(cu.build_history_table([row], NOW), 100)
    assert "0 snapshots shown" in cu.render_text(cu.build_history_table([], NOW), 100)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-30, "just now"),
        (0, "just now"),
        (4, "just now"),
        (5, "5 seconds ago"),
        (34, "34 seconds ago"),
        (59, "59 seconds ago"),
        (60, "1 min ago"),
        (180, "3 mins ago"),
        (3599, "59 mins ago"),
        (3600, "1 hour ago"),
        (3780, "1 hour 3 mins ago"),
        (7200, "2 hours ago"),
        (7260, "2 hours 1 min ago"),
        (86400, "1 day ago"),
        (90000, "1 day 1 hour ago"),
        (176400, "2 days 1 hour ago"),
    ],
)
def test_relative_time(seconds: int, expected: str) -> None:
    assert cu.relative_time(NOW - timedelta(seconds=seconds), NOW) == expected


def test_tables_have_an_ago_column() -> None:
    view = cu.RoomView(
        room_id=7,
        room_name="Lobby",
        occupants=(
            cu.Occupant(name="Alice", seen_since=NOW - timedelta(minutes=3)),
            cu.Occupant(name="bob", seen_since=NOW - timedelta(seconds=34)),
        ),
    )
    current = cu.render_text(cu.build_current_table(view, NOW), 80)
    assert "| Ago" in current
    assert "3 mins ago" in current
    assert "34 seconds ago" in current
    old = cu.Snapshot(
        taken_at=NOW - timedelta(hours=1, minutes=3),
        room_id=1,
        room_name="R",
        count=1,
        names=["Alice"],
    )
    history = cu.render_text(cu.build_history_table([old], NOW), 120)
    assert "Ago" in history
    assert "1 hour 3 mins ago" in history


def test_history_table_has_four_columns_and_no_names() -> None:
    names = sorted((f"SomeUserName{n:02d}" for n in range(28)), key=str.casefold)
    row = cu.Snapshot(
        taken_at=NOW - timedelta(hours=1, minutes=3),
        room_id=1,
        room_name="DAILY $ JOIN NOW",
        count=len(names),
        names=names,
    )
    text = cu.render_text(cu.build_history_table([row], NOW), 100)
    out = lines_of(text)
    header = next(ln for ln in out if "Time (GMT+8)" in ln)
    assert [c.strip() for c in header.strip("|").split("|")] == [
        "Time (GMT+8)",
        "Ago",
        "Room",
        "Count",
    ]
    assert "SomeUserName" not in text  # names stay in snapshots.jsonl only
    assert "28" in text
    table_lines = [ln for ln in out if ln[:1] in "+|"]
    assert len({len(ln) for ln in table_lines}) == 1
    assert len(table_lines[0]) <= 100


def test_current_table_empty_states() -> None:
    assert "(not in a room)" in cu.render_text(
        cu.build_current_table(mkview([], room_id=None), NOW), 80
    )
    assert "(no users yet)" in cu.render_text(cu.build_current_table(mkview([]), NOW), 80)


def test_history_table_long_room_name_wraps_inside_width() -> None:
    rows = [
        cu.Snapshot(
            taken_at=NOW + timedelta(minutes=5 * k),
            room_id=1,
            room_name="A very long room name that must wrap in its cell",
            count=40,
            names=[f"User{n:03d}" for n in range(40)],
        )
        for k in range(3)
    ]
    text = cu.render_text(cu.build_history_table(rows, NOW), 100)
    assert "\x1b" not in text
    out = lines_of(text)
    assert all(len(line) <= 100 for line in out)
    table_lines = [ln for ln in out if ln[:1] in "+|"]
    assert len({len(ln) for ln in table_lines}) == 1


def test_history_table_empty_state() -> None:
    assert "(no snapshots yet)" in cu.render_text(cu.build_history_table([], NOW), 100)


def test_tables_show_gmt_plus_8_while_storage_is_utc() -> None:
    saved = cu.Snapshot(taken_at=NOW, room_id=1, room_name="R", count=0, names=[])
    assert saved.taken_at.utcoffset() == timedelta(0)  # stored in UTC
    history = cu.render_text(cu.build_history_table([saved], NOW), 100)
    assert "2026-11-21 20:03" in history  # 12:03 UTC is 20:03 GMT+8
    assert "Time (GMT+8)" in history
    assert "generated 2026-11-21 20:03:30 GMT+8" in history
    current = cu.render_text(cu.build_current_table(mkview(["Alice"]), NOW), 80)
    assert "updated 20:03:30 GMT+8" in current
    assert "Seen since (GMT+8)" in current
    assert "20:03:30" in current  # Alice's "seen since" is NOW too


def test_display_now_and_default_marks_use_gmt_plus_8() -> None:
    assert cu.display_now().utcoffset() == timedelta(hours=8)
    mark = cu.next_mark(datetime(2026, 11, 21, 15, 58, tzinfo=UTC), 5)  # 23:58 GMT+8
    assert (mark.day, mark.hour, mark.minute) == (22, 0, 0)
    assert mark.utcoffset() == timedelta(hours=8)


def test_write_atomic(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "x.txt"
    assert cu.write_atomic(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert not list(target.parent.glob("*.tmp"))
    assert not cu.write_atomic(tmp_path / "sub" / "x.txt" / "y.txt", "no")  # parent is a file


# --- history store --------------------------------------------------------------------


def snap(minute: int, names: list[str] | None = None) -> cu.Snapshot:
    names = names or ["Alice"]
    return cu.Snapshot(
        taken_at=NOW.replace(minute=minute, second=0),
        room_id=1,
        room_name="R",
        count=len(names),
        names=names,
    )


def test_history_store_round_trip_skip_bad_lines_and_limit(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.jsonl"
    store = cu.HistoryStore(path, max_rows=3)
    for minute in (0, 5, 10, 15):
        store.append(snap(minute))
    assert [r.taken_at.minute for r in store.recent()] == [5, 10, 15]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n\n" + json.dumps({"taken_at": "x"}) + "\n")
    store2 = cu.HistoryStore(path, max_rows=3)
    assert [r.taken_at.minute for r in store2.recent()] == [5, 10, 15]
    # The file is never trimmed: 4 snapshots + 3 junk lines (one is blank).
    assert len(path.read_text(encoding="utf-8").splitlines()) == 7


# --- recorder, writer, opener ---------------------------------------------------------


def make_files(tmp_path: Path, state: cu.RoomState | None = None) -> cu.TableFiles:
    st = settings(tmp_path)
    state = state or cu.RoomState(clock=lambda: NOW)
    return cu.TableFiles(st, state, cu.HistoryStore(st.snapshots_path, st.max_rows), lambda: NOW)


def test_recorder_take(tmp_path: Path) -> None:
    state = cu.RoomState(clock=lambda: NOW)
    files = make_files(tmp_path, state)
    rec = cu.Recorder(files, threading.Event(), clock=lambda: NOW)
    mark = NOW.replace(minute=5, second=0)
    assert rec.take(mark) is None  # not in a room: no row
    assert not files.settings.snapshots_path.exists()

    state.enter_room(7)
    state.remember_room_name(7, "Lobby")
    state.add_players([cu.Player(1, "bob", 1), cu.Player(2, "Alice", 2)])
    rec2 = cu.Recorder(files, threading.Event(), clock=lambda: mark + timedelta(seconds=2))
    saved = rec2.take(mark)
    assert saved is not None
    assert saved.names == ["Alice", "bob"]
    assert saved.count == 2
    assert saved.taken_at == mark
    history = files.settings.history_path.read_text(encoding="utf-8")
    assert "Lobby" in history
    assert "Alice" not in history  # the history table shows counts only
    assert len(files.settings.snapshots_path.read_text(encoding="utf-8").splitlines()) == 1


def test_recorder_late_mark_records_real_time(tmp_path: Path) -> None:
    state = cu.RoomState(clock=lambda: NOW)
    state.enter_room(7)
    files = make_files(tmp_path, state)
    mark = NOW.replace(minute=5, second=0)
    woke = mark + timedelta(minutes=12, seconds=7, microseconds=5)
    saved = cu.Recorder(files, threading.Event(), clock=lambda: woke).take(mark)
    assert saved is not None
    assert saved.taken_at == woke.replace(microsecond=0)


def test_live_writer_merges_a_burst_into_one_write(tmp_path: Path) -> None:
    state = cu.RoomState(clock=lambda: NOW)
    files = make_files(tmp_path, state)
    files.write_current()
    base = files.current_writes
    stop = threading.Event()
    writer = cu.LiveWriter(files, stop)
    writer.start()
    try:
        state.enter_room(7)
        for n in range(20):
            state.add_players([cu.Player(n, f"User{n:02d}", n)])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and files.current_writes == base:
            time.sleep(0.02)
        time.sleep(0.5)  # a second write would show up by now
    finally:
        stop.set()
        writer.join(2)
    assert files.current_writes == base + 1
    text = files.settings.current_path.read_text(encoding="utf-8")
    assert "User19" in text
    assert "20 users" in text


def test_live_writer_refreshes_only_while_in_a_room(tmp_path: Path) -> None:
    state = cu.RoomState(clock=lambda: NOW)
    files = make_files(tmp_path, state)
    files.write_current()
    base = files.current_writes
    stop = threading.Event()
    writer = cu.LiveWriter(files, stop, refresh_seconds=0.2)
    writer.start()
    try:
        time.sleep(1.3)
        assert files.current_writes == base  # no room: nothing to refresh
        state.enter_room(7)  # this change itself causes one write
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and files.current_writes < base + 3:
            time.sleep(0.05)
    finally:
        stop.set()
        writer.join(2)
    assert files.current_writes >= base + 3  # the change, then timed refreshes
    assert not files.settings.history_path.exists()  # history changes only at a snapshot


def test_opener_writes_files_then_launches(tmp_path: Path) -> None:
    files = make_files(tmp_path)
    seen: list[list[Path]] = []
    stop = threading.Event()
    opener = cu.Opener(files, stop, launch=seen.append, gap=0.01)
    opener.start()
    try:
        opener.request()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not seen:
            time.sleep(0.02)
    finally:
        stop.set()
        opener.join(2)
    assert seen == [[files.settings.current_path, files.settings.history_path]]
    assert files.settings.current_path.exists()
    assert files.settings.history_path.exists()


# --- app wiring with a fake extension -------------------------------------------------


class FakeExt:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.events: dict[str, object] = {}
        self.packet_infos = {cu.Direction.TO_SERVER: {"Chat": 1}, cu.Direction.TO_CLIENT: {}}
        self.console: list[str] = []

    def intercept(self, direction: object, callback: object, name: str) -> None:
        self.handlers[name] = callback

    def on_event(self, name: str, callback: object) -> None:
        self.events[name] = callback

    def write_to_console(self, text: str, color: str) -> None:
        self.console.append(text)


class FakeOpener:
    def __init__(self) -> None:
        self.requests = 0

    def request(self) -> None:
        self.requests += 1


def message(payload: bytes) -> SimpleNamespace:
    packet = SimpleNamespace(bytearray=bytearray(bytes(6) + payload))
    return SimpleNamespace(packet=packet, is_blocked=False)


def make_app() -> tuple[cu.App, FakeExt, cu.RoomState, FakeOpener]:
    ext, state, opener = FakeExt(), cu.RoomState(clock=lambda: NOW), FakeOpener()
    app = cu.App(ext, state, opener)  # type: ignore[arg-type]
    app.register()
    return app, ext, state, opener


def test_app_registers_every_needed_name() -> None:
    _, ext, _, _ = make_app()
    needed = {n for names in cu.NEEDS.values() for n in names}
    assert needed == set(ext.handlers)
    assert set(ext.events) == {"connection_start", "connection_end"}


def test_app_command_is_blocked_and_opens_files() -> None:
    _, ext, _, opener = make_app()
    for name in ("Chat", "Shout"):
        msg = message(s(":checkuser") + i(0) + i(-1))
        ext.handlers[name](msg)  # type: ignore[operator]
        assert msg.is_blocked
    plain = message(s("hello") + i(0) + i(-1))
    ext.handlers["Chat"](plain)  # type: ignore[operator]
    near = message(s(":checkusers") + i(0) + i(-1))
    ext.handlers["Chat"](near)  # type: ignore[operator]
    assert not plain.is_blocked
    assert not near.is_blocked
    assert opener.requests == 2


def test_app_tracks_a_room_from_packets() -> None:
    _, ext, state, _ = make_app()
    h = ext.handlers
    h["GetGuestRoomResult"](message(b(True) + i(7) + s("Lobby")))  # type: ignore[operator]
    h["RoomReady"](message(s("model_a") + i(7)))  # type: ignore[operator]
    h["Users"](message(users(player(1, "Alice", 10), pet(2, "Rex", 11), user_bot(3, "Bot", 12))))  # type: ignore[operator]
    h["Users"](message(users(player(5, "bob", 14))))  # type: ignore[operator]
    view = state.view()
    assert (view.room_id, view.label, view.names) == (7, "Lobby", ["Alice", "bob"])
    h["UserRemove"](message(s("10")))  # type: ignore[operator]
    assert state.view().names == ["bob"]
    h["CloseConnection"](message(b""))  # type: ignore[operator]
    assert state.view().room_id is None


def test_bad_users_packet_never_raises_and_is_saved(tmp_path: Path) -> None:
    _, ext, state, _ = make_app()
    cu.configure_dump_dir(tmp_path / "bad")
    cu.reset_reports()
    try:
        payload = users(player(1, "Alice", 10), player(2, "Bob", 11))[:-5]
        ext.handlers["Users"](message(payload))  # type: ignore[operator]  # must not raise
        ext.handlers["Users"](message(payload))  # second one: no second file
    finally:
        cu.configure_dump_dir(None)
    assert state.view().names == ["Alice"]  # the entry before the problem is kept
    assert len(list((tmp_path / "bad").glob("Users-*.hex"))) == 1


def test_guard_swallows_other_errors() -> None:
    def boom(_: object) -> None:
        raise RuntimeError("x")

    cu.guarded("Boom", boom)(message(b""))  # type: ignore[arg-type]


def test_connection_start_warns_about_unknown_names() -> None:
    app, ext, state, _ = make_app()
    state.enter_room(1)
    app.on_connection_start()
    assert state.view().room_id is None
    assert "Unknown packet names" in ext.console[-1]
    assert "in:Users" in ext.console[-1]


def test_build_command_uses_placeholders_and_absolute_paths() -> None:
    cmd = cu.build_command("/usr/local/bin/uv", Path("/x/checkuser.py"))
    assert cmd[:4] == ["/usr/local/bin/uv", "run", "--script", "/x/checkuser.py"]
    assert cmd[4:] == ["-c", "{cookie}", "-p", "{port}", "-f", "{filename}"]


# --- terminal view ----------------------------------------------------------------------


class FakeLive:
    """Stands in for rich's ``Live``: keeps the last renderable and a fake console height."""

    def __init__(self, height: int) -> None:
        self.console = type("C", (), {"height": height})()
        self.shown: object = None

    def update(self, renderable: object, refresh: bool = False) -> None:
        self.shown = renderable


def live_text(live: FakeLive) -> str:
    return cu.render_text(live.shown, 100)  # type: ignore[arg-type]


def test_history_room_name_is_cut_with_an_ellipsis_not_wrapped() -> None:
    row = cu.Snapshot(taken_at=NOW, room_id=1, room_name="R" * 60, count=1, names=["Alice"])
    out = lines_of(cu.render_text(cu.build_history_table([row], NOW), 100))
    assert "…" in "\n".join(out)
    assert sum("| 2026-11-21" in ln for ln in out) == 1  # the row stays on one line


def test_live_view_stacks_both_tables_and_fits_the_screen(tmp_path: Path) -> None:
    state = cu.RoomState(clock=lambda: NOW)
    state.enter_room(7)
    state.remember_room_name(7, "Lobby")
    state.add_players(cu.Player(i, f"user{i:02d}", i) for i in range(29))
    files = make_files(tmp_path, state)
    for k in range(40):
        files.store.append(snap(5 * (k % 12)))
    live = FakeLive(height=57)
    files.live = live  # type: ignore[assignment]
    files.write_current()
    out = lines_of(live_text(live))
    assert "Current users" in out[0] or any("Current users" in ln for ln in out)
    assert any("User count history" in ln for ln in out)
    assert len(out) <= 57 - 1  # leaves one line for a log message
    assert files.current_writes == 1


def test_live_view_keeps_a_few_history_rows_on_a_small_screen(tmp_path: Path) -> None:
    state = cu.RoomState(clock=lambda: NOW)
    state.enter_room(7)
    state.add_players(cu.Player(i, f"user{i:02d}", i) for i in range(29))
    files = make_files(tmp_path, state)
    for k in range(10):
        files.store.append(snap(5 * k))
    live = FakeLive(height=20)
    files.live = live  # type: ignore[assignment]
    files.write_history()
    text = live_text(live)
    assert text.count("2026-11-21") >= cu.MIN_LIVE_HISTORY_ROWS
