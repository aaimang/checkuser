from datetime import UTC, datetime, timedelta

from usergraph import dst_note, parse_lines, pick_zone, select, utc_offset

LINES = [
    '{"taken_at":"2026-11-25T10:00:00Z","room_id":1,"room_name":"A","count":5,"names":[]}',
    "",
    '{"taken_at":"2026-11-25T11:00:00Z","room_id":2,"room_name":"B","count":9,"names":[]}',
    '{"taken_at":"2026-11-25T11:30:00Z","room_id":1,"room_name":"A","count":7,"names":[]}',
    '{"taken_at":"2026-11-25T11:35',  # half-written line
    '{"room_id":1,"count":3}',  # no taken_at
]
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def test_parse_skips_bad_lines() -> None:
    points = parse_lines(LINES)
    assert [p.count for p in points] == [5, 9, 7]
    assert points[0].taken_at == datetime(2026, 11, 25, 10, 0, tzinfo=UTC)


def test_select_defaults_to_newest_room() -> None:
    points = select(parse_lines(LINES), hours=0, room_id=None, now=NOW)
    assert [p.count for p in points] == [5, 7]


def test_select_room_and_window() -> None:
    points = parse_lines(LINES)
    assert [p.count for p in select(points, 1, 1, NOW)] == [7]
    assert [p.count for p in select(points, 0, 2, NOW)] == [9]
    assert select([], 24, None, NOW) == []


def test_labels_follow_dst() -> None:
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    # The UK clocks moved forward at 01:00 UTC on 2026-03-29.
    before = datetime(2026, 3, 29, 0, 30, tzinfo=UTC).astimezone(london)
    after = datetime(2026, 3, 29, 1, 30, tzinfo=UTC).astimezone(london)
    assert (before.strftime("%H:%M"), before.tzname()) == ("00:30", "GMT")
    assert (after.strftime("%H:%M"), after.tzname()) == ("02:30", "BST")


def test_dst_note() -> None:
    from zoneinfo import ZoneInfo

    assert dst_note(ZoneInfo("Asia/Kuala_Lumpur"), NOW).endswith("UTC+08:00, no DST")
    assert dst_note(ZoneInfo("Europe/London"), NOW) == "GMT UTC+00:00, DST: BST UTC+01:00"
    summer = datetime(2026, 7, 1, tzinfo=UTC)
    assert dst_note(ZoneInfo("Europe/London"), summer) == (
        "BST UTC+01:00 (DST now), winter: GMT UTC+00:00"
    )
    # Southern hemisphere: Sydney has DST in November.
    assert "(DST now)" in dst_note(ZoneInfo("Australia/Sydney"), NOW)


def test_utc_offset_negative_and_half_hour() -> None:
    from zoneinfo import ZoneInfo

    assert utc_offset(NOW.astimezone(ZoneInfo("America/New_York"))) == "UTC-05:00"
    assert utc_offset(NOW.astimezone(ZoneInfo("Asia/Kolkata"))) == "UTC+05:30"


def test_pick_zone() -> None:
    choices = [("UTC", "x"), ("Europe/London", "y")]
    assert pick_zone("", choices) == "Asia/Kuala_Lumpur"
    assert pick_zone("2", choices) == "Europe/London"
    assert pick_zone("3", choices) is None
    assert pick_zone("0", choices) is None
    assert pick_zone("asia/tokyo", choices) == "Asia/Tokyo"
    assert pick_zone("Mars/Base", choices) is None


def test_ask_zone_retries_until_valid(monkeypatch, capsys) -> None:
    import usergraph

    answers = iter(["Mars/Base", "99", "Europe/London"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert usergraph.ask_zone(NOW) == "Europe/London"
    assert capsys.readouterr().out.count("Try again.") == 2


def test_pick_range() -> None:
    from usergraph import RANGES, pick_range, range_hours

    assert pick_range("") == "day"
    assert pick_range("1") == "1h"
    assert pick_range(str(len(RANGES))) == "all"
    assert pick_range("99") is None
    assert pick_range("WEEK") == "week"
    assert pick_range("fortnight") is None
    assert range_hours("3months") == ("3 months (91 days)", 91 * 24)
    assert range_hours("year")[1] == 365 * 24


def test_time_ticks_format_by_span() -> None:
    from zoneinfo import ZoneInfo

    from usergraph import time_ticks

    tz = ZoneInfo("Asia/Kuala_Lumpur")
    hour = time_ticks(NOW - timedelta(hours=1), NOW, tz)
    assert len(hour) == 9
    assert (hour[0], hour[-1]) == ((0, "19:00"), (60, "20:00"))
    assert time_ticks(NOW - timedelta(days=7), NOW, tz)[-1][1] == "25/11 20:00"
    assert time_ticks(NOW - timedelta(days=365), NOW, tz)[0][1] == "25/11/25"


def test_time_ticks_follow_dst() -> None:
    from zoneinfo import ZoneInfo

    from usergraph import time_ticks

    # A 2-hour axis across the UK change: 00:00 GMT to 03:00 BST is 2 real hours.
    start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    ticks = time_ticks(start, start + timedelta(hours=2), ZoneInfo("Europe/London"), count=2)
    assert [text for _, text in ticks] == ["00:00", "02:00", "03:00"]


def test_fit() -> None:
    from usergraph import fit

    assert fit("abc", 5) == "abc"
    assert fit("abcdef", 4) == "abc…"
