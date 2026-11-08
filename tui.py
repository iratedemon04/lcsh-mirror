#!/usr/bin/env python3
"""
Interactive LCSH browser over the database built by load.py. No harvest needed:
load a release downloaded from the mirror, then browse it.

    python load.py lcsh-YYYYMMDD.mrc.gz
    python tui.py <words>

  up/down j/k   move          enter   send record to the outbox
  pgdn/right n  next page     space   full record (enter sends from there too)
  pgup/left p   previous page /       new search
  s             A-Z / relevance       q       quit

"Send" writes <lccn>.mrc into the outbox (LCSH_OUTBOX, default data/outbox). Anything
watching that folder can pick it up; if it drops <lccn>.ok / <lccn>.err back, the
status line shows the result.
"""
import curses
import os
import re
import sys
import time

import load

DB = load.DB
OUTBOX = os.environ.get("LCSH_OUTBOX", os.path.join(load.DATA, "outbox"))
LIMIT = 5000


def find(c, words, alpha):
    q = " ".join(f'"{t}"*' for t in re.findall(r"\w+", words))
    if not q:
        return []
    order = "r.heading COLLATE NOCASE" if alpha else "bm25(fts, 0, 10, 1)"
    return c.execute(f"SELECT f.lccn, r.heading FROM fts f JOIN records r USING(lccn) "
                     f"WHERE fts MATCH ? ORDER BY {order} LIMIT {LIMIT}", (q,)).fetchall()


def send(c, lccn):
    r = c.execute("SELECT marc FROM records WHERE lccn=?", (lccn,)).fetchone()
    tmp = os.path.join(OUTBOX, f"{lccn}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(r[0])
    os.replace(tmp, os.path.join(OUTBOX, f"{lccn}.mrc"))


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            win.addnstr(y, x, text, w - x - 1, attr)
        except curses.error:
            pass


def prompt(scr, label, initial=""):
    h, w = scr.getmaxyx()
    curses.curs_set(1)
    buf = initial
    while True:
        scr.move(h - 1, 0)
        scr.clrtoeol()
        put(scr, h - 1, 0, label + buf, curses.A_BOLD)
        ch = scr.get_wch()
        if ch in ("\n", "\r", curses.KEY_ENTER):
            break
        if ch == "\x1b":
            buf = None
            break
        if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            buf = buf[:-1]
        elif isinstance(ch, str) and ch.isprintable():
            buf += ch
    curses.curs_set(0)
    return buf


def check_markers(status):
    """Pick up delivery results written by the host wrapper."""
    for f in os.listdir(OUTBOX):
        name, ext = os.path.splitext(f)
        if ext in (".ok", ".err"):
            path = os.path.join(OUTBOX, f)
            msg = open(path, encoding="utf-8", errors="replace").read().strip()
            os.remove(path)
            status[0] = (f"sent {name}.mrc -> {msg}" if ext == ".ok" else f"FAILED {name}: {msg}")
            status[1] = ext == ".ok"


def show_record(scr, c, lccn, status):
    text = load.preview(c.execute("SELECT marc FROM records WHERE lccn=?", (lccn,)).fetchone()[0])
    lines, top = text.splitlines(), 0
    while True:
        h, w = scr.getmaxyx()
        scr.erase()
        put(scr, 0, 0, f" {lccn}   enter send · up/down scroll · q back", curses.A_REVERSE)
        for i, ln in enumerate(lines[top:top + h - 2]):
            put(scr, i + 1, 0, ln)
        put(scr, h - 1, 0, status[0], curses.color_pair(1 if status[1] else 2))
        ch = scr.getch()
        check_markers(status)
        if ch in (ord("q"), 27, curses.KEY_LEFT):
            return
        if ch in (10, 13, curses.KEY_ENTER):
            send(c, lccn)
            status[:] = [f"sending {lccn}…", True]
        elif ch in (curses.KEY_DOWN, ord("j")):
            top = min(top + 1, max(0, len(lines) - (h - 2)))
        elif ch in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)


def main(scr, words):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    scr.timeout(300)   # wake up to show delivery results
    c = load.connect(DB)
    folder = os.environ.get("LCSH_FOLDER", OUTBOX)
    alpha, rows, sel = True, find(c, words, True), 0
    status = [f"→ {folder}", True]
    while True:
        h, w = scr.getmaxyx()
        per = max(1, h - 3)
        page, pages = sel // per, max(1, -(-len(rows) // per))
        scr.erase()
        more = "+" if len(rows) == LIMIT else ""
        put(scr, 0, 0, f" lcsh: {words}   {len(rows):,}{more} headings   page {page + 1}/{pages}   "
                       f"{'A-Z' if alpha else 'relevance'}".ljust(w), curses.A_REVERSE)
        for i, (lccn, head) in enumerate(rows[page * per:(page + 1) * per]):
            idx = page * per + i
            attr = curses.A_REVERSE if idx == sel else 0
            put(scr, i + 1, 0, f" {lccn:<13} ".ljust(15), attr | curses.A_DIM)
            put(scr, i + 1, 15, head, attr)
        if not rows:
            put(scr, 2, 1, "no matches — press / to search again")
        put(scr, h - 2, 0, " enter send · space view · n/p page · / search · s sort · q quit", curses.A_DIM)
        put(scr, h - 1, 0, status[0], curses.color_pair(1 if status[1] else 2))
        ch = scr.getch()
        check_markers(status)
        if ch == -1:
            continue
        if ch in (ord("q"), 27):
            return
        if not rows and ch not in (ord("/"),):
            continue
        if ch in (curses.KEY_DOWN, ord("j")):
            sel = min(sel + 1, len(rows) - 1)
        elif ch in (curses.KEY_UP, ord("k")):
            sel = max(sel - 1, 0)
        elif ch in (curses.KEY_NPAGE, curses.KEY_RIGHT, ord("n")):
            sel = min((page + 1) * per, len(rows) - 1)
        elif ch in (curses.KEY_PPAGE, curses.KEY_LEFT, ord("p")):
            sel = max((page - 1) * per, 0)
        elif ch == curses.KEY_HOME:
            sel = 0
        elif ch == curses.KEY_END:
            sel = len(rows) - 1
        elif ch in (10, 13, curses.KEY_ENTER):
            send(c, rows[sel][0])
            status[:] = [f"sending {rows[sel][0]}…", True]
        elif ch == ord(" "):
            show_record(scr, c, rows[sel][0], status)
        elif ch == ord("s"):
            alpha = not alpha
            rows, sel = find(c, words, alpha), 0
        elif ch == ord("/"):
            q = prompt(scr, " search: ", words)
            if q:
                words, sel = q, 0
                rows = find(c, words, alpha)


if __name__ == "__main__":
    os.makedirs(OUTBOX, exist_ok=True)
    curses.wrapper(main, " ".join(sys.argv[1:]))
