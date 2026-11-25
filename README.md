# Checkuser

A G-Earth extension (G-Python) that counts the Habbo users in your room.

> Unofficial fan project, not affiliated with or endorsed by Sulake Oy.
> G-Earth is a man-in-the-middle on the game connection. Using it breaks Habbo's
> Terms of Service (sections 5 and 8). You use it at your own risk.

## What it does

- Tracks the users in the room you are in. It counts Habbo users only. It does not count bots or pets. You are counted too.
- `data/current_users.txt`: a table of who is in the room now. The file is rewritten after every join and leave.
- `data/history.txt`: a table with one row every 5 minutes on the clock (:00, :05, :10 ... :55). Each row has the time, "Ago", room name and count. The file keeps every snapshot, so it grows by 288 rows a day. It does not list names.
- `data/snapshots.jsonl`: every snapshot with the names, one JSON object per line (times in UTC). Nothing is trimmed.
- Type `:checkuser` in the room chat. The command is blocked, so nobody sees it. Both tables open in your default text app.
- All times in the tables are GMT+8. Stored times are UTC.
- An "Ago" column shows how long ago each time was: `just now` (under 5 seconds), `34 seconds ago`, `3 mins ago`, `1 hour 3 mins ago`, `1 day 2 hours ago`. `current_users.txt` is rewritten every 30 seconds while you are in a room. `history.txt` is rewritten at each snapshot and when you type `:checkuser`, so its "Ago" values can be up to 5 minutes old until you open it.

- In a terminal, the script shows both tables stacked: current users on top, the history below. The terminal history shows the newest 50 snapshots, or fewer if the window is small (at least 3). The files are not cut. A long room name is cut with `…`.

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and G-Earth.

```bash
uv run --script checkuser.py --install --dry-run
uv run --script checkuser.py --install
```

On Windows and Linux, add `--gearth-dir "<folder with G-Earth.exe or G-Earth.jar>"`.
Then restart G-Earth, then restart the Habbo client. Developer mode is not needed.

Remove it with `uv run --script checkuser.py --install --uninstall`.

## Try the tables without Habbo

```bash
uv run --script checkuser.py --demo
uv run --script checkuser.py --show
```

## Live graph

`usergraph.py` draws the user count from `data/snapshots.jsonl` as a live, coloured graph in the terminal. It redraws when a new snapshot is saved.

![usergraph.py demo: time zone menu, time range menu, then a live graph that redraws when a snapshot is added](docs/demo/usergraph.gif)

```bash
uv run --script usergraph.py
```

- At start, select the time zone and the time range (1 hour, 1 day, 1 week, 1 month, 3 months, 6 months, 1 year or all data).
- Time labels follow the daylight saving time rules of the zone you select.
- Skip the menus with `--tz` and `--range`, for example `--tz Europe/London --range week`. Run `uv run --script usergraph.py --help` for all options.
- Press Ctrl+C to stop.

The demo uses fake data. Make the GIF again with `vhs docs/demo/usergraph.tape` ([vhs](https://github.com/charmbracelet/vhs)).

## Limits

- It sees only the room you are in. No row is saved when you are not in a room.
- Enter (or re-enter) a room after G-Earth connects. The script cannot ask the hotel for the user list of a room you are already in.
- A snapshot is the room at that moment. If the computer sleeps through a mark, the row shows the real time of the late snapshot.
- Your text app may not reload `current_users.txt` by itself. Use a monospaced font and reload the file.
- The `Users` packet layout comes from public sources plus a note about the live client. Check it with a real capture. A bad packet is saved to `data/bad-packets/` (first one per type).
- The names in `data/` belong to other players. Keep the folder local. Do not publish it.

## Develop

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The dependencies are in two places: the `# /// script` header of `checkuser.py` (used at run time) and `pyproject.toml` (tests and lint). Keep both lists the same.
