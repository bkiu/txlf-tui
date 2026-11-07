#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
TXLF 2026 TUI — offline schedule browser for Texas Linux Fest 2026.

Usage:
    ./txlf.py            # browse the locally cached schedule
    ./txlf.py --fetch    # (re)download the schedule from pretalx first

Keys:
    j/k or arrows     move
    1 / 2             switch day
    n                 jump to "now"
    m                 my schedule (saved sessions)
    Enter             event details
    s                 save / unsave session (star)
    F                 fetch latest schedule (needs network)
    ?                 help
    q / Esc           quit / go back
"""

import curses
import json
import os
import sys
import textwrap
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

SCHEDULE_URL = "https://pretalx.com/txlf2026/schedule/export/schedule.json"
APP_NAME = "txlf-tui"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / APP_NAME
CACHE_FILE = DATA_DIR / "schedule.json"
SAVED_FILE = DATA_DIR / "saved.json"
CONF_TZ_NAME = "US/Central"

# pretalx track color (hex) -> nearest curses color
TRACK_COLORS = {
    "Linux": 6,                    # slate
    "Distributions": 4,             # blue
    "Cloud Native": 6,              # cyan
    "Artificial Intelligence": 7,   # brown -> light gray
    "Observability": 5,             # pink
    "Databases": 2,                 # green
    "Security": 2,                  # teal -> green
    "Automation": 3,                # orange -> yellow
    "Infrastructure": 5,            # purple -> magenta
    "Gaming": 3,                    # yellow
    "Development": 2,               # green
    "Other": 1,                     # red
}


# ---------------------------------------------------------------- data layer

@dataclass
class Event:
    guid: str
    title: str
    subtitle: str
    date: str          # YYYY-MM-DD
    start: datetime    # aware
    end: datetime      # aware
    room: str
    track: str
    etype: str
    abstract: str
    url: str
    persons: list = field(default_factory=list)   # list of dicts
    links: list = field(default_factory=list)

    @property
    def time_range(self):
        return self.start.strftime("%H:%M") + "–" + self.end.strftime("%H:%M")

    def is_live(self, now):
        return self.start <= now < self.end


def parse_schedule(raw):
    conf = raw["schedule"]["conference"]
    rooms = {r["name"]: r for r in conf.get("rooms", [])}
    events = []
    days = []
    for day in conf["days"]:
        date = day["date"]
        days.append(date)
        for room, evs in day["rooms"].items():
            for e in evs:
                events.append(Event(
                    guid=e["guid"],
                    title=e.get("title", "(untitled)"),
                    subtitle=e.get("subtitle") or "",
                    date=date,
                    start=datetime.fromisoformat(e["date"]),
                    end=datetime.fromisoformat(e["end"]),
                    room=room,
                    track=e.get("track") or "",
                    etype=e.get("type", ""),
                    abstract=e.get("abstract") or "",
                    url=e.get("url", ""),
                    persons=e.get("persons") or [],
                    links=e.get("links") or [],
                ))
    events.sort(key=lambda ev: (ev.date, ev.start, ev.room))
    return conf, rooms, days, events


def load_schedule():
    """Load schedule from local cache. Returns (conf, rooms, days, events)."""
    path = CACHE_FILE
    if not path.exists():
        # fall back to a data/schedule.json shipped next to this script
        alt = Path(__file__).resolve().parent / "data" / "schedule.json"
        if alt.exists():
            path = alt
    if not path.exists():
        sys.exit(
            f"No local schedule found (looked in {CACHE_FILE} and data/).\n"
            "Run:  ./txlf.py --fetch"
        )
    return parse_schedule(json.loads(path.read_text()))


def fetch_schedule():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    with urllib.request.urlopen(SCHEDULE_URL, timeout=20) as r:
        tmp.write_bytes(r.read())
    tmp.replace(CACHE_FILE)
    return parse_schedule(json.loads(CACHE_FILE.read_text()))


# saved / starred sessions
def load_saved():
    if SAVED_FILE.exists():
        try:
            return set(json.loads(SAVED_FILE.read_text()).get("guids", []))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_saved(guids):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_FILE.write_text(json.dumps({"guids": sorted(guids)}, indent=1))


# ------------------------------------------------------------------- helpers

def conf_tz():
    # legacy key first, then IANA equivalent (some tzdata builds lack US/* aliases)
    for name in (CONF_TZ_NAME, "America/Chicago", "America/Indiana/Indianapolis"):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            continue
    return timezone.utc


def wrap(text, width):
    return textwrap.wrap(" ".join(str(text).split()), width=max(10, width)) or [""]


def room_code(name):
    return (name[:3] if len(name) > 3 else name).upper()


# ---------------------------------------------------------------------- app

class App:
    def __init__(self, stdscr):
        self.scr = stdscr
        curses.curs_set(0)
        self.scr.keypad(True)
        self.pairs_ok = set()
        if curses.has_colors():
            try:
                curses.start_color()
            except curses.error:
                pass
            else:
                try:
                    curses.use_default_colors()
                except curses.error:
                    pass
                n = curses.tigetnum("colors")
                for i in range(1, min(16, n) + 1):
                    try:
                        curses.init_pair(i, min(i - 1, n - 1), -1)
                        self.pairs_ok.add(i)
                    except curses.error:
                        pass
        self.msg = ""
        self.help = False
        self.saved = load_saved()
        self.refresh_data()
        self.sel = 0
        self.list_off = 0         # first visible entry (sticky, scrolloff=5)
        self.detail = None          # Event or None
        self.dlines = []            # prebuilt detail lines
        self.detail_off = 0
        self.my_sched = False

    # ---------------------------- data

    def refresh_data(self):
        self.conf, self.rooms, self.days, self.events = load_schedule()
        self.day_idx = self.default_day_idx()
        self.sel = 0
        self.list_off = 0
        self.detail = None
        self.dlines = []
        self.my_sched = False

    def default_day_idx(self):
        """Pick the conference day matching today (conference tz), clamped to range."""
        if not self.days:
            return 0
        today = self.now().astimezone(conf_tz()).date()
        for i, d in enumerate(self.days):
            try:
                if date.fromisoformat(d) == today:
                    return i
            except ValueError:
                continue
        first = date.fromisoformat(self.days[0])
        return 0 if today < first else len(self.days) - 1

    def now(self):
        return datetime.now(timezone.utc)

    def conf_now(self):
        return self.now().astimezone(conf_tz())

    def day_events(self, day_idx):
        return [e for e in self.events if e.date == self.days[day_idx]]

    def saved_events(self):
        return [e for e in self.events if e.guid in self.saved]

    def current_list(self):
        if self.my_sched:
            return self.saved_events()
        return self.day_events(self.day_idx)

    def track_attr(self, ev):
        c = TRACK_COLORS.get(ev.track)
        if c is not None and c in self.pairs_ok:
            return curses.color_pair(c) | curses.A_BOLD
        return curses.A_BOLD

    # ---------------------------- rendering

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if w < 30 or h < 8:
            try:
                self.scr.addstr(0, 0, "Terminal too small (need 30x8).")
            except curses.error:
                pass
            self.scr.refresh()
            return

        now = self.now()
        self.scr.addstr(0, 0, " TXLF 2026 ", curses.A_REVERSE | curses.A_BOLD)
        tab_txt = ""
        for i, d in enumerate(self.days):
            label = f"{i + 1}:{d[5:]}"
            if not self.my_sched and i == self.day_idx:
                tab_txt += f" [{label}] "
            else:
                tab_txt += f"  {label}  "
        if self.my_sched:
            tab_txt += " [my schedule] "
        self.scr.addstr(0, 11, tab_txt[: max(0, w - 22)])

        clock = self.conf_now().strftime("%I:%M %p")
        self.scr.addstr(0, max(11, w - len(clock) - 2), clock, curses.A_DIM)

        top = 2
        body_h = max(1, h - 4)
        if self.detail:
            self.draw_detail(body_h, w, top)
        else:
            self.draw_list(body_h, w, top, now)

        try:
            self.scr.addnstr(h - 1, 0, self.footer(w), w - 1, curses.A_DIM)
        except curses.error:
            pass
        if self.msg:
            try:
                self.scr.addnstr(h - 1, 0, self.msg, w - 1, curses.A_REVERSE | curses.A_BOLD)
            except curses.error:
                pass
        if self.help:
            self.draw_help(h, w)
        self.scr.refresh()

    def footer(self, w):
        f = "[j/k]move [1-2]day [n]ow [m]saved [Enter]view [s]ave [F]etch [?]help [q]uit"
        return f[: w]

    def list_row(self, ev, width):
        live = ev.is_live(self.now())
        star = "*" if ev.guid in self.saved else " "
        if width >= 62:
            base = f" {ev.time_range}  {star} {ev.room:<10} {ev.title}"
        elif width >= 44:
            base = (f" {ev.start.strftime('%H:%M')}-{ev.end.strftime('%H:%M')}"
                    f" {star} {room_code(ev.room)} {ev.title}")
        else:
            base = f" {ev.start.strftime('%H:%M')} {star} {room_code(ev.room)} {ev.title}"
        if live:
            base = " ▶ " + base.lstrip()
        return base, live

    def draw_list(self, body_h, w, top, now):
        items = self.current_list()
        vis = body_h
        if self.sel >= len(items):
            self.sel = max(0, len(items) - 1)

        # build display entries: (kind, event_index)
        if self.my_sched:
            entries = []
            last_date = None
            for i, ev in enumerate(items):
                if ev.date != last_date:
                    entries.append(("date", ev.date))
                    last_date = ev.date
                entries.append(("ev", i))
            pos = next(k for k, (kind, i) in enumerate(entries) if kind == "ev" and i == self.sel) if items else 0
        else:
            entries = [("ev", i) for i in range(len(items))]
            pos = self.sel

        # sticky viewport with scrolloff (vim-like): only scroll when the
        # highlighted row is within SCROLLOFF rows of the top or bottom
        soff = 5
        if soff * 2 + 1 >= vis:
            soff = (vis - 1) // 2
        row = pos - self.list_off
        if row > vis - 1 - soff:
            self.list_off = pos - (vis - 1 - soff)
        elif row < soff:
            self.list_off = pos - soff
        off = min(max(0, self.list_off), max(0, len(entries) - vis))
        self.list_off = off
        for k in range(off, min(len(entries), off + vis)):
            kind, i = entries[k]
            row = top + (k - off)
            if kind == "date":
                label = f"\u2500\u2500  {datetime.fromisoformat(i):%a %-d %B}  \u2500\u2500"
                try:
                    self.scr.addnstr(row, 0, label[: w - 1], w - 1, curses.A_DIM | curses.A_BOLD)
                except curses.error:
                    pass
                continue
            ev = items[i]
            text, live = self.list_row(ev, w)
            if i == self.sel:
                text = ">" + text[1:] if text else ">"
                attr = curses.A_REVERSE | curses.A_BOLD
            else:
                attr = 0
            if live:
                attr |= curses.A_BOLD
            try:
                self.scr.addnstr(row, 0, text, w - 1, attr)
            except curses.error:
                pass
        if not items:
            hint = " (press s on a session to save it)" if self.my_sched else ""
            try:
                self.scr.addstr(top, 0, f"Nothing here yet.{hint}")
            except curses.error:
                pass

    def build_detail_lines(self, ev, w):
        tz = conf_tz()
        L = []
        L.append((ev.title, curses.A_BOLD))
        if ev.subtitle:
            L.append((ev.subtitle, curses.A_DIM))
        cap = (self.rooms.get(ev.room) or {}).get("capacity")
        cap_s = f"  (cap {cap})" if cap else ""
        loc = (f"{ev.start.astimezone(tz):%a %b %-d %H:%M}–{ev.end.astimezone(tz):%H:%M}"
               f"  ·  {ev.room}{cap_s}")
        L.append((loc, 0))
        meta = "  ·  ".join(x for x in [ev.track, ev.etype] if x)
        if meta:
            L.append((meta, self.track_attr(ev)))
        for p in ev.persons:
            name = p.get("public_name") or p.get("name") or "Speaker"
            L.append(("", 0))
            L.append((name, curses.A_BOLD))
            bio = (p.get("biography") or "").strip()
            if bio:
                if len(bio) > 400 and "…" not in bio:
                    bio = bio[:400].rsplit(" ", 1)[0] + " …"
                L += [(l, 0) for l in wrap(bio, w - 2)]
        if ev.abstract:
            L.append(("", 0))
            L.append(("Abstract", curses.A_BOLD))
            L += [(l, 0) for l in wrap(ev.abstract, w - 2)]
        if ev.links:
            L.append(("", 0))
            for l in ev.links:
                L.append((f"• {l.get('text', '')}  {l.get('href', '')}", 0))
        L.append(("", 0))
        L.append((ev.url, curses.A_UNDERLINE))
        L.append(("", 0))
        star = "★ saved to my schedule" if ev.guid in self.saved else "☆ save to my schedule"
        L.append((f"[s] {star}", 0))
        return L

    def draw_detail(self, body_h, w, top):
        vis = body_h
        if self.detail_off > max(0, len(self.dlines) - vis):
            self.detail_off = max(0, len(self.dlines) - vis)
        for i in range(self.detail_off, min(len(self.dlines), self.detail_off + vis)):
            text, attr = self.dlines[i]
            try:
                self.scr.addnstr(top + (i - self.detail_off), 0, text, w - 1, attr)
            except curses.error:
                pass

    HELP = [
        "TXLF 2026 — key bindings",
        "",
        "  j / k / ↑ / ↓     move",
        "  1 / 2             switch day",
        "  n                 jump to current time",
        "  m                 my saved schedule",
        "  Enter             open session details",
        "  s                 save / unsave session",
        "  F                 fetch schedule (online)",
        "  ?                 this help",
        "  q / Esc           quit / go back",
    ]

    def draw_help(self, h, w):
        x0 = max(0, (w - 32) // 2)
        y0 = max(0, (h - len(self.HELP) - 2) // 2)
        for i, line in enumerate(self.HELP):
            y = y0 + i
            if y >= h - 1:
                break
            attr = curses.A_REVERSE if i == 0 else (
                curses.A_BOLD if line and not line.startswith("  ") else 0)
            try:
                self.scr.addnstr(y, x0, line, max(0, w - x0 - 1), attr)
            except curses.error:
                pass

    # ---------------------------- input

    def toggle_save(self, ev):
        if ev.guid in self.saved:
            self.saved.discard(ev.guid)
            self.msg = f"Removed: {ev.title}"
        else:
            self.saved.add(ev.guid)
            self.msg = f"★ Saved: {ev.title}"
        save_saved(self.saved)

    def do_fetch(self):
        self.msg = "Fetching schedule… (needs network)"
        self.scr.refresh()
        try:
            self.refresh_data()
            self.msg = "Schedule updated."
        except Exception as ex:
            self.msg = f"Fetch failed: {ex}"

    def jump_now(self):
        now = self.now()
        items = self.current_list()
        target = None
        for i, ev in enumerate(items):
            if ev.end > now:
                target = i
                break
        if target is None and items:
            target = len(items) - 1
        if target is not None:
            self.sel = target

    def open_detail(self):
        items = self.current_list()
        if not items:
            return
        self.sel = min(self.sel, len(items) - 1)
        self.detail = items[self.sel]
        self.dlines = self.build_detail_lines(self.detail, max(10, self.scr.getmaxyx()[1]))
        self.detail_off = 0
        self.msg = ""

    def close_detail(self):
        self.detail = None
        self.dlines = []
        self.detail_off = 0
        self.msg = ""

    def step(self, delta):
        if self.detail:
            vis = max(1, self.scr.getmaxyx()[0] - 4)
            self.detail_off = min(max(0, len(self.dlines) - vis), self.detail_off + delta)
        else:
            n = len(self.current_list())
            self.sel = min(max(0, n - 1), self.sel + delta)
        self.msg = ""

    def run(self):
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch in (ord("q"), ord("Q")):
                if self.detail:
                    self.close_detail()
                else:
                    return
            elif ch in (curses.KEY_UP, ord("k")):
                self.step(-1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.step(1)
            elif ch == curses.KEY_NPAGE:
                self.step(5)
            elif ch == curses.KEY_PPAGE:
                self.step(-5)
            elif ch == curses.KEY_HOME:
                self.sel = 0
                self.detail_off = 0
            elif ch == curses.KEY_END:
                if self.detail:
                    self.detail_off = len(self.dlines)
                else:
                    self.sel = max(0, len(self.current_list()) - 1)
            elif ch == curses.KEY_RESIZE:
                if self.detail:
                    self.dlines = self.build_detail_lines(self.detail, max(10, self.scr.getmaxyx()[1]))
            elif ch in (ord("1"), ord("2")):
                d = ch - ord("1")
                if d < len(self.days):
                    old = None
                    if not self.my_sched and self.current_list() and self.sel < len(self.current_list()):
                        old = self.current_list()[self.sel]
                    self.my_sched = False
                    self.day_idx = d
                    self.sel = 0
                    self.list_off = 0
                    if old:
                        for i, ev in enumerate(self.current_list()):
                            if ev.date == old.date and ev.start >= old.start:
                                self.sel = i
                                break
                    self.msg = ""
            elif ch == ord("m"):
                self.my_sched = not self.my_sched
                self.sel = 0
                self.list_off = 0
                self.msg = ""
            elif ch == ord("n"):
                self.jump_now()
                self.msg = ""
            elif ch == ord("?"):
                self.help = not self.help
            elif ch in (ord("s"), ord("S")):
                items = self.current_list()
                if items and self.sel < len(items):
                    self.toggle_save(items[self.sel])
            elif ch in (ord("F"), ord("f")):
                self.do_fetch()
            elif ch in (curses.KEY_ENTER, 10, 13):
                if not self.detail:
                    self.open_detail()
                self.msg = ""
            elif ch in (curses.KEY_BACKSPACE, 27, 8):
                if self.detail:
                    self.close_detail()


def main():
    if "--fetch" in sys.argv:
        try:
            fetch_schedule()
            print(f"Schedule downloaded to {CACHE_FILE}")
        except Exception as ex:
            sys.exit(f"Fetch failed: {ex}")
        return
    if "--clear-saved" in sys.argv:
        SAVED_FILE.unlink(missing_ok=True)
        print("Cleared saved sessions.")
        return
    curses.wrapper(lambda scr: App(scr).run())


if __name__ == "__main__":
    main()
