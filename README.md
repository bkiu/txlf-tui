# txlf-tui

Offline TUI for the **Texas Linux Fest 2026** schedule, built with Python's
standard library only (curses). No dependencies, no network required at run
time — the full schedule is cached locally.

This was written for the (term/deck)[https://github.com/bkiu/termdeck], my Raspberry Pi 3A+ powered cyberdeck.

## Quick start

```sh
./txlf.py            # browse the schedule (uses local cache)
./txlf.py --fetch    # re-download the schedule from pretalx, then browse
```

If no cache exists yet, the app falls back to the `data/schedule.json` shipped
in this repo, so it works out of the box offline.

## Keys

| Key         | Action                                  |
|-------------|-----------------------------------------|
| `j` / `k`   | Move up / down (also arrows, PgUp/PgDn) |
| `1` / `2`   | Switch day                              |
| `n`         | Jump to the current time                |
| `m`         | Toggle "my schedule" (saved sessions)   |
| `Enter`     | Open session details                    |
| `s`         | Save / unsave the session (★)           |
| `F`         | Fetch latest schedule (needs network)   |
| `?`         | Help overlay                            |
| `q` / `Esc` | Quit / back                             |

Other: `./txlf.py --clear-saved` wipes your saved list.

## Features

- **Live highlight** — sessions happening right now get a `▶` marker and bold
  text; the header shows the conference-timezone (US/Central) clock.
- **Saved sessions** — star anything with `s`; your picks persist in
  `~/.local/share/txlf-tui/saved.json` and are viewable via `m`, sorted by
  time with room info.
- **Per-day views** — day tabs in the header, tracks color-coded.
- **Details** — title, room + capacity, track/type, speaker bios, full
  abstract, links, and the pretalx URL.
- **Small-screen friendly** — degrades gracefully down to ~30×8: room names
  shorten to 3-letter codes and columns drop as width shrinks.

## License

MIT — see [LICENSE](LICENSE).

## Data locations

| File                                      | Contents               |
|-------------------------------------------|------------------------|
| `~/.local/share/txlf-tui/schedule.json`   | Cached pretalx export  |
| `~/.local/share/txlf-tui/saved.json`      | Your saved session GUIDs |

Schedule source: <https://pretalx.com/txlf2026/schedule/export/schedule.json>
