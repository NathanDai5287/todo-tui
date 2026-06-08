#!/usr/bin/env python3
"""Simple terminal todo app — curses + SQLite, single file, stdlib only."""

import curses
import locale
import os
import plistlib
import re
import sqlite3
import subprocess
from pathlib import Path

locale.setlocale(locale.LC_ALL, "")

DB_PATH = Path.home() / ".todo.db"
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, "\x7f", "\x08")
UNDO_DEPTH = 100

HELP_INPUT = [
    ("⏎", "add"), ("↓", "tasks"), ("ctrl+w", "word"),
    ("esc", "clear"), ("⌫", "undo"), ("ctrl+c", "quit"),
]
HELP_TASK = [
    ("↑↓", "nav"), ("shift+↑↓", "move"), ("space", "done"),
    ("tab", "status"), ("⏎", "notes"), ("→", "links"),
    ("c", "copy"), ("F2", "rename"), ("x", "del"), ("⌫", "undo"),
    ("q", "quit"),
]
HELP_RENAME = [("type", ""), ("⏎/esc", "save"), ("ctrl+w", "word")]
HELP_NOTES_ITEMS = [
    ("⏎", "newline"), ("tab", "indent"), ("⇧tab", "dedent"),
    ("esc", "save & close"),
]
HELP_LINKS_ITEMS = [("↑↓", "nav"), ("⏎/o", "open"), ("esc/←", "back")]

INPUT_PREFIX = " + "
INPUT_INDENT = "   "
INPUT_PLACEHOLDER = "New task..."

TASK_PREFIX_LEN = 7  # " [x] · "
TASK_INDENT = " " * TASK_PREFIX_LEN

EDITOR_INDENT = "    "  # one indent level in the notes editor (Tab / Shift+Tab)

HEADER_ROWS = 1  # the "todo" heading occupies the top row
TODO_LABEL = " todo "
HEADING_ACTIVE_LEAD = "──▶"  # heading lead-in for the focused panel
HEADING_PLAIN_LEAD = "──"  # heading lead-in for an unfocused panel

F2_KEY = curses.KEY_F0 + 2
CTRL_W = "\x17"

STATUS_NONE = 0
STATUS_WIP = 1
STATUS_PR = 2
STATUS_MERGED = 3
STATUS_WONT = 4
STATUS_COUNT = 5
STATUS_COLOR_PAIR = {
    STATUS_NONE: 0, STATUS_WIP: 1, STATUS_PR: 2, STATUS_MERGED: 3, STATUS_WONT: 4,
}


# --- Database ---

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                status INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "done" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN done INTEGER NOT NULL DEFAULT 0"
            )
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN status INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
    return conn



def list_tasks(conn):
    return conn.execute(
        "SELECT id, title, notes, done, status FROM tasks "
        "ORDER BY done ASC, position ASC, id ASC"
    ).fetchall()


def add_task(conn, title):
    with conn:
        conn.execute("UPDATE tasks SET position = position + 1 WHERE done = 0")
        conn.execute(
            "INSERT INTO tasks (title, notes, position, done) VALUES (?, '', 0, 0)",
            (title,),
        )


def delete_task(conn, task_id):
    row = conn.execute(
        "SELECT position, done FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return
    pos, done = row
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.execute(
            "UPDATE tasks SET position = position - 1 "
            "WHERE position > ? AND done = ?",
            (pos, done),
        )


def update_title(conn, task_id, title):
    with conn:
        conn.execute("UPDATE tasks SET title=? WHERE id=?", (title, task_id))


def update_notes(conn, task_id, notes):
    with conn:
        conn.execute("UPDATE tasks SET notes=? WHERE id=?", (notes, task_id))


def move_task(conn, task_id, direction):
    row = conn.execute(
        "SELECT position, done FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    pos, done = row
    new_pos = pos + direction
    swap = conn.execute(
        "SELECT id FROM tasks WHERE position=? AND done=?",
        (new_pos, done),
    ).fetchone()
    if swap is None:
        return False
    with conn:
        conn.execute("UPDATE tasks SET position=? WHERE id=?", (new_pos, task_id))
        conn.execute("UPDATE tasks SET position=? WHERE id=?", (pos, swap[0]))
    return True


def toggle_done(conn, task_id):
    row = conn.execute(
        "SELECT position, done FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return
    pos, done = row
    new_done = 1 - done
    with conn:
        conn.execute(
            "UPDATE tasks SET position = position - 1 "
            "WHERE position > ? AND done = ?",
            (pos, done),
        )
        max_new = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE done=?",
            (new_done,),
        ).fetchone()[0]
        new_pos = max_new + 1
        conn.execute(
            "UPDATE tasks SET done=?, position=? WHERE id=?",
            (new_done, new_pos, task_id),
        )


def cycle_status(conn, task_id, direction=1):
    row = conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        return
    new_status = (row[0] + direction) % STATUS_COUNT
    with conn:
        conn.execute(
            "UPDATE tasks SET status=? WHERE id=?", (new_status, task_id)
        )


def snapshot(conn):
    return conn.execute(
        "SELECT id, title, notes, position, done, status FROM tasks"
    ).fetchall()


def restore(conn, snap):
    with conn:
        conn.execute("DELETE FROM tasks")
        conn.executemany(
            "INSERT INTO tasks (id, title, notes, position, done, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            snap,
        )


# --- UI helpers ---

def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    avail = w - x
    if y == h - 1:
        avail -= 1
    if avail <= 0:
        return
    try:
        win.addnstr(y, x, text, avail, attr)
    except curses.error:
        pass


def fill_line(win, y, x, width, text, attr=0):
    padded = (text + " " * width)[:width]
    safe_addstr(win, y, x, padded, attr)


def render_help_bar(win, y, items, width):
    fill_line(win, y, 0, width, "", curses.A_REVERSE)
    key_attr = curses.A_REVERSE | curses.A_BOLD
    label_attr = curses.A_REVERSE
    x = 1
    for key, label in items:
        item_len = len(key) + (1 + len(label) if label else 0)
        if x + item_len > width - 1:
            break
        safe_addstr(win, y, x, key, key_attr)
        x += len(key)
        if label:
            safe_addstr(win, y, x, " " + label, label_attr)
            x += 1 + len(label)
        x += 2


def draw_modal_header(stdscr, w, text, status):
    """Draw a full-width reversed header bar at row 0 with a colored status block
    on the left when the task has a status."""
    cp = STATUS_COLOR_PAIR.get(status, 0)
    chip_w = 2 if cp else 0
    fill_line(stdscr, 0, 0, w, " " * chip_w + " " + text + " ", curses.A_REVERSE)
    if chip_w:
        try:
            stdscr.chgat(0, 0, chip_w, curses.color_pair(cp))
        except curses.error:
            pass


def is_backspace(ch):
    return ch in BACKSPACE_KEYS


_URL_RE = re.compile(r'https?://[^\s<>"\'`]+')
# Trailing punctuation that's usually sentence/markdown noise, not part of a URL.
_URL_TRAILING = ".,;:!?)]}>\"'"
# Absolute (/…) or home (~/…) file paths, including drag-and-dropped paths
# whose spaces/specials are backslash-escaped (e.g. /Users/me/My\ File.txt).
_FILE_RE = re.compile(r'(?:~/|/)(?:\\.|[^\s\\])*')


def _unescape_path(token):
    """Strip the backslash escaping a terminal adds to a dragged-in file path."""
    return re.sub(r'\\(.)', r'\1', token).rstrip(_URL_TRAILING)


def find_links(*texts):
    """De-duplicated, openable links found in the texts, in appearance order.

    Detects http(s) URLs and local file paths (absolute or ~-relative,
    including drag-and-dropped paths with backslash-escaped spaces). A file
    path is included only when it currently exists on disk, which also filters
    out the slashes inside URLs. The returned file targets are unescaped and
    ~-expanded so they can be handed straight to `open`."""
    links = []
    seen = set()
    for text in texts:
        matches = []
        for m in _URL_RE.finditer(text):
            url = m.group().rstrip(_URL_TRAILING)
            if url:
                matches.append((m.start(), url))
        for m in _FILE_RE.finditer(text):
            target = os.path.expanduser(_unescape_path(m.group()))
            if target and os.path.lexists(target):
                matches.append((m.start(), target))
        matches.sort(key=lambda it: it[0])
        for _pos, target in matches:
            if target not in seen:
                seen.add(target)
                links.append(target)
    return links


def _default_browser_bundle_id():
    """Bundle id of the default web browser, read from the LaunchServices
    http(s) scheme handler. Returns None if it can't be determined."""
    plist = os.path.expanduser(
        "~/Library/Preferences/com.apple.LaunchServices/"
        "com.apple.launchservices.secure.plist"
    )
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    for h in data.get("LSHandlers", []):
        if h.get("LSHandlerURLScheme") in ("http", "https"):
            bid = h.get("LSHandlerRoleAll") or h.get("LSHandlerRoleViewer")
            if bid:
                return bid
    return None


def open_link(target):
    """Open a URL or local file via `open`. Local PDFs are opened in the
    default browser rather than the default PDF app (Preview); remote URLs
    already route to the browser."""
    cmd = ["open", target]
    is_remote = target.startswith(("http://", "https://"))
    if not is_remote and target.lower().endswith(".pdf"):
        bid = _default_browser_bundle_id()
        if bid:
            cmd = ["open", "-b", bid, target]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    except (OSError, subprocess.CalledProcessError):
        pass


def set_cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def hard_wrap(text, width):
    """Hard wrap (no word breaking). Returns list with at least one entry.
    Simple cursor math: line = cursor // width, col = cursor % width."""
    if width <= 0:
        return [text] if text else [""]
    if not text:
        return [""]
    return [text[i : i + width] for i in range(0, len(text), width)]


def word_wrap_segments(text, width):
    """Word-aware wrap. Returns list of (start_pos, end_pos, segment_text).

    - start_pos / end_pos are indices into `text`; the segment is text[start:end].
    - The space between two segments (if any) is eaten — it's the position
      `end_pos` of one segment and the gap before `start_pos` of the next.
    - When a single token doesn't fit in `width`, it's hard-broken.
    """
    if width <= 0:
        return [(0, len(text), text)]
    if not text:
        return [(0, 0, "")]
    segs = []
    pos = 0
    while pos < len(text):
        if len(text) - pos <= width:
            segs.append((pos, len(text), text[pos:]))
            break
        if text[pos + width] == " ":
            segs.append((pos, pos + width, text[pos : pos + width]))
            pos += width + 1
            continue
        chunk = text[pos : pos + width]
        space_at = chunk.rfind(" ")
        if space_at > 0:
            segs.append((pos, pos + space_at, text[pos : pos + space_at]))
            pos += space_at + 1
        else:
            segs.append((pos, pos + width, chunk))
            pos += width
    return segs


def word_wrap_line(text, width, cursor=None):
    """Word-wrap a single line. Returns (lines, cur_row, cur_col).

    `lines` is a list of segment strings (at least one). When `cursor` is None,
    cur_row/cur_col are 0. Otherwise they give the display position of the
    cursor, mirroring the editor's layout() logic (including a parking row when
    the cursor sits just past the right edge of a full-width segment)."""
    avail = max(1, width)
    if not text:
        return [""], 0, 0
    segs = word_wrap_segments(text, avail)
    lines = [t for _s, _e, t in segs]
    if cursor is None:
        return lines, 0, 0

    cur_row = 0
    cur_col = 0
    for k, (off, _e, t) in enumerate(segs):
        seg_end = off + len(t)
        if off <= cursor < seg_end:
            cur_row = k
            cur_col = cursor - off
            break
        if cursor == seg_end:
            if k + 1 < len(segs) and segs[k + 1][0] == cursor:
                continue
            cur_row = k
            cur_col = cursor - off
            break
    else:
        cur_row = len(segs) - 1
        cur_col = cursor - segs[-1][0]

    if cur_col == len(lines[cur_row]) == avail and cur_row == len(lines) - 1:
        lines.append("")
        cur_row += 1
        cur_col = 0

    return lines, cur_row, cur_col


def delete_word_back(buf, cursor):
    """Mutate buf list in place; return new cursor position."""
    if cursor == 0:
        return 0
    new_cursor = cursor
    while new_cursor > 0 and buf[new_cursor - 1] == " ":
        new_cursor -= 1
    while new_cursor > 0 and buf[new_cursor - 1] != " ":
        new_cursor -= 1
    del buf[new_cursor:cursor]
    return new_cursor


def word_left(buf, cursor):
    """Index of the start of the word before the cursor (macOS Option+Left)."""
    i = cursor
    while i > 0 and buf[i - 1] == " ":
        i -= 1
    while i > 0 and buf[i - 1] != " ":
        i -= 1
    return i


def word_right(buf, cursor):
    """Index just past the end of the word after the cursor (Option+Right)."""
    n = len(buf)
    i = cursor
    while i < n and buf[i] == " ":
        i += 1
    while i < n and buf[i] != " ":
        i += 1
    return i


# Tokens returned by decode_escape().
ESC_BARE = None
ESC_OTHER = "other"
ESC_ALT_BACKSPACE = "alt-backspace"
ESC_WORD_LEFT = "word-left"
ESC_WORD_RIGHT = "word-right"


def decode_escape(stdscr):
    """Decode the bytes following an ESC that get_wch() already returned.

    Returns one of the ESC_* tokens. Recognizes Option/Alt + Left/Right word
    motion (xterm 'ESC [ 1 ; 3 D|C' and meta 'ESC b' / 'ESC f'), Alt+Backspace
    ('ESC' + backspace), and a bare Escape (no bytes follow). Other escape
    sequences are consumed and reported as ESC_OTHER."""
    stdscr.nodelay(True)
    try:
        try:
            c = stdscr.get_wch()
        except curses.error:
            return ESC_BARE
        if is_backspace(c):
            return ESC_ALT_BACKSPACE
        if c in ("b", "B"):
            return ESC_WORD_LEFT
        if c in ("f", "F"):
            return ESC_WORD_RIGHT
        if c in ("[", "O"):
            params = []
            final = None
            for _ in range(16):
                try:
                    nxt = stdscr.get_wch()
                except curses.error:
                    break
                if isinstance(nxt, str) and len(nxt) == 1 and (
                    nxt.isalpha() or nxt == "~"
                ):
                    final = nxt
                    break
                params.append(nxt if isinstance(nxt, str) else "")
            if final in ("C", "D"):
                mod = "".join(params).split(";")[-1]
                if mod == "3":  # Alt / Option
                    return ESC_WORD_RIGHT if final == "C" else ESC_WORD_LEFT
        return ESC_OTHER
    finally:
        stdscr.nodelay(False)


# --- Text editor core ---
#
# A small word-wrapping multi-line editor shared by the full-screen task-notes
# modal (edit_notes). State lives
# in a TextBuffer; layout and key handling are free functions so a caller can
# mount the editor in any rectangle it owns and drive its own draw loop.

class TextBuffer:
    """Mutable multi-line text + cursor (cy, cx) + display scroll (scroll_y)."""

    def __init__(self, text=""):
        self.lines = text.split("\n") if text else [""]
        if not self.lines:
            self.lines = [""]
        self.cy = 0
        self.cx = 0
        self.scroll_y = 0

    @property
    def text(self):
        return "\n".join(self.lines)


def editor_layout(lines, cy, cx, width):
    """Word-wrap each logical line. Return (display_rows, cur_row, cur_col).
    display_rows is a list of (logical_idx, char_offset, text)."""
    avail = max(1, width)
    rows = []
    for li, line in enumerate(lines):
        if not line:
            rows.append((li, 0, ""))
            continue
        for s, _e, text in word_wrap_segments(line, avail):
            rows.append((li, s, text))

    # Locate cursor among the rows belonging to logical line cy.
    cy_row_indices = [ri for ri, (li, _o, _t) in enumerate(rows) if li == cy]
    cur_row = cy_row_indices[0] if cy_row_indices else 0
    cur_col = 0
    for k, ri in enumerate(cy_row_indices):
        _li, off, text = rows[ri]
        seg_end = off + len(text)
        if off <= cx < seg_end:
            cur_row = ri
            cur_col = cx - off
            break
        if cx == seg_end:
            # End of this segment. If the next row in this line starts at the
            # same position, there's no eaten space — cursor goes there.
            # Otherwise (eaten space, or last row), cursor sits at line end.
            if k + 1 < len(cy_row_indices):
                next_off = rows[cy_row_indices[k + 1]][1]
                if next_off == cx:
                    continue
            cur_row = ri
            cur_col = cx - off
            break
    else:
        # Cursor at end-of-line (shouldn't normally fall through but be safe).
        if cy_row_indices:
            ri = cy_row_indices[-1]
            _li, off, _t = rows[ri]
            cur_row = ri
            cur_col = cx - off

    # Parking row: cursor visually past the right edge of a full-width row.
    cur_row_text = rows[cur_row][2] if rows else ""
    if cur_col == len(cur_row_text) == avail:
        is_last_of_li = (
            cur_row == len(rows) - 1 or rows[cur_row + 1][0] != cy
        )
        if is_last_of_li:
            rows.insert(cur_row + 1, (cy, cx, ""))
            cur_row += 1
            cur_col = 0

    return rows, cur_row, cur_col


def editor_scroll(buf, display_rows, cur_row, view_h):
    """Adjust buf.scroll_y so cur_row stays visible within view_h rows."""
    if cur_row < buf.scroll_y:
        buf.scroll_y = cur_row
    elif cur_row >= buf.scroll_y + view_h:
        buf.scroll_y = cur_row - view_h + 1
    buf.scroll_y = max(0, min(buf.scroll_y, max(0, len(display_rows) - view_h)))


def editor_word_delete_back(buf):
    """Delete the word before the cursor (or merge into the previous line)."""
    lines = buf.lines
    if buf.cx == 0:
        if buf.cy > 0:
            prev = lines[buf.cy - 1]
            lines[buf.cy - 1] = prev + lines[buf.cy]
            del lines[buf.cy]
            buf.cy -= 1
            buf.cx = len(prev)
        return
    line = lines[buf.cy]
    new_cx = buf.cx
    while new_cx > 0 and line[new_cx - 1] == " ":
        new_cx -= 1
    while new_cx > 0 and line[new_cx - 1] != " ":
        new_cx -= 1
    lines[buf.cy] = line[:new_cx] + line[buf.cx:]
    buf.cx = new_cx


def editor_word_left(buf):
    """Move the cursor to the start of the previous word (Option+Left).
    At the start of a line, wrap to the end of the previous line."""
    if buf.cx == 0:
        if buf.cy > 0:
            buf.cy -= 1
            buf.cx = len(buf.lines[buf.cy])
        return
    line = buf.lines[buf.cy]
    buf.cx = word_left(line, buf.cx)


def editor_word_right(buf):
    """Move the cursor past the end of the next word (Option+Right).
    At the end of a line, wrap to the start of the next line."""
    line = buf.lines[buf.cy]
    if buf.cx >= len(line):
        if buf.cy < len(buf.lines) - 1:
            buf.cy += 1
            buf.cx = 0
        return
    buf.cx = word_right(line, buf.cx)


def editor_handle_key(buf, ch, width):
    """Apply one content/navigation keystroke to buf at the given wrap width.

    Handles printable input, arrows, Home/End, Backspace, Delete, newline and
    Ctrl-W. Returns True if the key was consumed. Callers own Esc / Ctrl-C and
    the Alt-Backspace (Esc-then-Backspace) word delete, since what "leaving the
    editor" means differs per mount."""
    lines = buf.lines
    if ch == CTRL_W:
        editor_word_delete_back(buf)
    elif ch == curses.KEY_UP:
        rows, cur_row, cur_col = editor_layout(lines, buf.cy, buf.cx, width)
        if cur_row > 0:
            tli, toff, ttext = rows[cur_row - 1]
            buf.cy = tli
            buf.cx = toff + min(cur_col, len(ttext))
        else:
            # Already on the first line: jump to its start (macOS behavior).
            buf.cy = 0
            buf.cx = 0
    elif ch == curses.KEY_DOWN:
        rows, cur_row, cur_col = editor_layout(lines, buf.cy, buf.cx, width)
        if cur_row < len(rows) - 1:
            tli, toff, ttext = rows[cur_row + 1]
            buf.cy = tli
            buf.cx = toff + min(cur_col, len(ttext))
        else:
            # Already on the last line: jump to its end (macOS behavior).
            buf.cy = len(lines) - 1
            buf.cx = len(lines[buf.cy])
    elif ch == curses.KEY_LEFT:
        if buf.cx > 0:
            buf.cx -= 1
        elif buf.cy > 0:
            buf.cy -= 1
            buf.cx = len(lines[buf.cy])
    elif ch == curses.KEY_RIGHT:
        if buf.cx < len(lines[buf.cy]):
            buf.cx += 1
        elif buf.cy < len(lines) - 1:
            buf.cy += 1
            buf.cx = 0
    elif ch == curses.KEY_HOME:
        buf.cx = 0
    elif ch == curses.KEY_END:
        buf.cx = len(lines[buf.cy])
    elif is_backspace(ch):
        if buf.cx > 0:
            lines[buf.cy] = lines[buf.cy][: buf.cx - 1] + lines[buf.cy][buf.cx:]
            buf.cx -= 1
        elif buf.cy > 0:
            prev = lines[buf.cy - 1]
            lines[buf.cy - 1] = prev + lines[buf.cy]
            del lines[buf.cy]
            buf.cy -= 1
            buf.cx = len(prev)
    elif ch == curses.KEY_DC:
        if buf.cx < len(lines[buf.cy]):
            lines[buf.cy] = lines[buf.cy][: buf.cx] + lines[buf.cy][buf.cx + 1:]
        elif buf.cy < len(lines) - 1:
            lines[buf.cy] = lines[buf.cy] + lines[buf.cy + 1]
            del lines[buf.cy + 1]
    elif ch == "\t":
        # Insert one indent level at the cursor.
        lines[buf.cy] = (
            lines[buf.cy][: buf.cx] + EDITOR_INDENT + lines[buf.cy][buf.cx:]
        )
        buf.cx += len(EDITOR_INDENT)
    elif ch == curses.KEY_BTAB:
        # Unindent the current line by up to one level.
        line = lines[buf.cy]
        remove = min(len(EDITOR_INDENT), len(line) - len(line.lstrip(" ")))
        if remove:
            lines[buf.cy] = line[remove:]
            buf.cx = max(0, buf.cx - remove)
    elif ch == "\n" or ch == "\r":
        tail = lines[buf.cy][buf.cx:]
        lines[buf.cy] = lines[buf.cy][: buf.cx]
        lines.insert(buf.cy + 1, tail)
        buf.cy += 1
        buf.cx = 0
    elif isinstance(ch, str) and len(ch) == 1 and ch.isprintable():
        lines[buf.cy] = lines[buf.cy][: buf.cx] + ch + lines[buf.cy][buf.cx:]
        buf.cx += 1
    else:
        return False
    return True


# --- Notes editor ---

def edit_notes(stdscr, title, initial_text, status=STATUS_NONE):
    """Full-screen editor for a task's notes. Esc/Ctrl-C save & close.
    Returns the edited text, or None if nothing changed."""
    original = initial_text
    buf = TextBuffer(initial_text)
    set_cursor(True)

    def result():
        return None if buf.text == original else buf.text

    try:
        while True:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            draw_modal_header(stdscr, w, "notes — " + title, status)

            display_rows, cur_row, cur_col = editor_layout(
                buf.lines, buf.cy, buf.cx, w
            )
            edit_h = max(1, h - 2)
            editor_scroll(buf, display_rows, cur_row, edit_h)

            for i in range(edit_h):
                ri = buf.scroll_y + i
                if ri >= len(display_rows):
                    break
                _, _, text = display_rows[ri]
                fill_line(stdscr, 1 + i, 0, w, text)

            render_help_bar(stdscr, h - 1, HELP_NOTES_ITEMS, w - 1)
            try:
                stdscr.move(1 + (cur_row - buf.scroll_y), min(cur_col, w - 1))
            except curses.error:
                pass
            stdscr.refresh()

            ch = stdscr.get_wch()
            if ch == "\x03":
                return result()
            if ch == "\x1b":
                tok = decode_escape(stdscr)
                if tok is ESC_BARE:
                    return result()
                if tok == ESC_ALT_BACKSPACE:
                    editor_word_delete_back(buf)
                elif tok == ESC_WORD_LEFT:
                    editor_word_left(buf)
                elif tok == ESC_WORD_RIGHT:
                    editor_word_right(buf)
                continue
            if ch == curses.KEY_RESIZE:
                continue
            editor_handle_key(buf, ch, w)
    finally:
        set_cursor(False)


def pick_link(stdscr, title, urls, status=STATUS_NONE):
    """Modal list of links detected in a task. Up/down to navigate, enter/o to
    open the selected link in the browser, esc/left to close."""
    sel = 0
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_modal_header(stdscr, w, "links — " + title, status)

        list_h = max(1, h - 2)
        if sel < scroll:
            scroll = sel
        elif sel >= scroll + list_h:
            scroll = sel - list_h + 1

        for i in range(list_h):
            idx = scroll + i
            if idx >= len(urls):
                break
            selected_row = idx == sel
            marker = " · " if selected_row else "   "
            attr = curses.A_BOLD if selected_row else curses.A_NORMAL
            fill_line(stdscr, 1 + i, 0, w, marker + urls[idx], attr)

        render_help_bar(stdscr, h - 1, HELP_LINKS_ITEMS, w - 1)
        stdscr.refresh()

        ch = stdscr.get_wch()
        if ch == "\x03":
            return
        if ch == "\x1b":
            tok = decode_escape(stdscr)
            if tok is ESC_BARE:
                return
            continue
        if ch in (curses.KEY_UP, "k"):
            sel = (sel - 1) % len(urls)
        elif ch in (curses.KEY_DOWN, "j"):
            sel = (sel + 1) % len(urls)
        elif ch == curses.KEY_HOME:
            sel = 0
        elif ch == curses.KEY_END:
            sel = len(urls) - 1
        elif ch in ("\n", "\r", "o", "O"):
            open_link(urls[sel])
        elif ch in (curses.KEY_LEFT, "q", "Q"):
            return
        elif ch == curses.KEY_RESIZE:
            continue


# --- Main view ---

def heading_bar(label, w, active):
    """Build a full-width section heading like '──▶ todo ───────'.
    The focused panel gets an arrow lead-in; otherwise a plain rule."""
    lead = HEADING_ACTIVE_LEAD if active else HEADING_PLAIN_LEAD
    text = lead + label
    return (text + "─" * max(0, w - len(text)))[:w]


def build_render(tasks, w, rename_idx=None, rename_text=None, rename_cursor=0):
    """Return (rows, rename_pos).

    rows is a list of row dicts: {'kind', 'task_idx', 'text', 'attr'}.
    rename_pos is (row_offset, col) of the rename cursor relative to the first
    row of the renamed task, or None when not renaming."""
    rows = []
    rename_pos = None
    split_idx = next(
        (i for i, t in enumerate(tasks) if t[3] == 1), len(tasks)
    )
    avail = max(1, w - TASK_PREFIX_LEN)
    for i, task in enumerate(tasks):
        if i == split_idx and i > 0:
            rows.append({"kind": "gap", "task_idx": None, "text": "", "attr": 0})
        _tid, title, notes, done, status = task
        if rename_idx == i and rename_text is not None:
            title = rename_text
        checkbox = "[x]" if done else "[ ]"
        marker = "·" if notes else " "
        if rename_idx == i and rename_text is not None:
            title_lines, cur_row, cur_col = word_wrap_line(
                title, avail, rename_cursor
            )
            rename_pos = (cur_row, TASK_PREFIX_LEN + cur_col)
        else:
            title_lines, _cr, _cc = word_wrap_line(title, avail)
        for j, line in enumerate(title_lines):
            if j == 0:
                prefix = " " + checkbox + " " + marker + " "
            else:
                prefix = TASK_INDENT
            attr = curses.A_DIM if done else curses.A_NORMAL
            rows.append(
                {
                    "kind": "task",
                    "task_idx": i,
                    "text": prefix + line,
                    "attr": attr,
                    "status": status if j == 0 else STATUS_NONE,
                }
            )
    return rows, rename_pos


def render_main(stdscr, tasks, selected, scroll, input_buf, input_cursor,
                renaming, rename_buf, rename_cursor,
                flash_task_idx=None):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    input_focused = (selected == 0) and not renaming
    prefix_len = len(INPUT_PREFIX)
    avail_input = max(1, w - prefix_len)
    input_text = "".join(input_buf)

    # --- "todo" heading ---
    safe_addstr(
        stdscr, 0, 0, heading_bar(TODO_LABEL, w, True),
        curses.A_BOLD,
    )

    # --- Input field ---
    if input_focused:
        input_lines = hard_wrap(input_text, avail_input) if input_text else [""]
        if (
            input_cursor > 0
            and input_cursor == len(input_text)
            and input_cursor % avail_input == 0
        ):
            input_lines.append("")
    else:
        if input_text:
            input_lines = hard_wrap(input_text, avail_input)
        else:
            input_lines = [INPUT_PLACEHOLDER]

    max_input_rows = max(1, h - HEADER_ROWS - 2)
    input_lines = input_lines[:max_input_rows]
    input_rows = len(input_lines)

    for i, line in enumerate(input_lines):
        p = INPUT_PREFIX if i == 0 else INPUT_INDENT
        attr = curses.A_NORMAL if input_focused else curses.A_DIM
        fill_line(stdscr, HEADER_ROWS + i, 0, w, p + line, attr)

    task_area_start = HEADER_ROWS + input_rows
    task_area_end = h - 1
    task_area_h = max(1, task_area_end - task_area_start)

    # --- Tasks ---
    rename_idx = (selected - 1) if (renaming and selected > 0) else None
    rename_text = "".join(rename_buf) if renaming else None
    rows, rename_pos = build_render(
        tasks, w,
        rename_idx=rename_idx,
        rename_text=rename_text,
        rename_cursor=rename_cursor if renaming else 0,
    )

    if selected > 0:
        sel_first = sel_last = None
        for ri, r in enumerate(rows):
            if r["kind"] == "task" and r["task_idx"] == selected - 1:
                if sel_first is None:
                    sel_first = ri
                sel_last = ri
        if sel_first is not None:
            if sel_first < scroll:
                scroll = sel_first
            elif sel_last >= scroll + task_area_h:
                scroll = sel_last - task_area_h + 1
    scroll = max(0, min(scroll, max(0, len(rows) - task_area_h)))

    sel_screen_first_row = None
    for i in range(task_area_h):
        ri = scroll + i
        if ri >= len(rows):
            break
        r = rows[ri]
        attr = r["attr"]
        if (
            r["kind"] == "task"
            and r["task_idx"] == (selected - 1)
            and selected > 0
            and not renaming
        ):
            if flash_task_idx is not None and r["task_idx"] == flash_task_idx:
                attr = curses.A_BOLD
            else:
                attr = curses.A_REVERSE
        fill_line(stdscr, task_area_start + i, 0, w, r["text"], attr)
        status_cp = STATUS_COLOR_PAIR.get(r.get("status", STATUS_NONE), 0)
        if status_cp:
            try:
                stdscr.chgat(task_area_start + i, 1, 3, curses.color_pair(status_cp))
            except curses.error:
                pass
        if (
            r["kind"] == "task"
            and r["task_idx"] == (selected - 1)
            and sel_screen_first_row is None
        ):
            sel_screen_first_row = task_area_start + i

    if not tasks and not input_focused:
        msg = "type at the top, press ⏎ to add"
        if h > 5:
            safe_addstr(
                stdscr,
                task_area_start + 1,
                max(0, (w - len(msg)) // 2),
                msg,
                curses.A_DIM,
            )

    # --- Help bar ---
    if renaming:
        help_items = HELP_RENAME
    elif input_focused:
        help_items = HELP_INPUT
    else:
        help_items = HELP_TASK
    render_help_bar(stdscr, h - 1, help_items, w - 1)

    # --- Cursor placement (LAST) ---
    cursor_pos = None
    if input_focused:
        c_line = input_cursor // avail_input
        c_col = prefix_len + (input_cursor % avail_input)
        c_line = min(c_line, max_input_rows - 1)
        cursor_pos = (HEADER_ROWS + c_line, min(c_col, w - 1))
    elif renaming and sel_screen_first_row is not None and rename_pos is not None:
        c_line, c_col = rename_pos
        cursor_pos = (sel_screen_first_row + c_line, min(c_col, w - 1))

    if cursor_pos is not None:
        try:
            stdscr.move(cursor_pos[0], cursor_pos[1])
        except curses.error:
            pass
    set_cursor(cursor_pos is not None)
    stdscr.refresh()

    return scroll


def run(stdscr):
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)
    curses.start_color()
    curses.use_default_colors()
    # Pin the chip palette to Ghostty's built-in default RGB values so the
    # status colors look identical across platforms instead of inheriting each
    # terminal's own palette (Windows console renders the named ANSI colors
    # much darker/duller than Ghostty's pastel defaults). Values from Ghostty's
    # default palette (src/terminal/color.zig), scaled from 0-255 to 0-1000.
    if curses.can_change_color():
        for color, (r, g, b) in (
            (curses.COLOR_BLACK, (29, 31, 33)),     # #1D1F21
            (curses.COLOR_RED, (204, 102, 102)),    # #CC6666
            (curses.COLOR_GREEN, (181, 189, 104)),  # #B5BD68
            (curses.COLOR_YELLOW, (240, 198, 116)), # #F0C674
            (curses.COLOR_MAGENTA, (178, 148, 187)),# #B294BB
        ):
            try:
                curses.init_color(color, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
            except curses.error:
                pass
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_MAGENTA)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_RED)
    set_cursor(False)
    stdscr.keypad(True)

    conn = db_connect()
    tasks = list_tasks(conn)
    selected = 1 if tasks else 0
    scroll = 0

    input_buf = []
    input_cursor = 0

    renaming = False
    rename_buf = []
    rename_cursor = 0
    rename_target_id = None

    undo_stack = []

    def push_undo():
        undo_stack.append((snapshot(conn), selected))
        if len(undo_stack) > UNDO_DEPTH:
            undo_stack.pop(0)

    def apply_undo():
        if not undo_stack:
            return None
        snap, sel = undo_stack.pop()
        restore(conn, snap)
        return sel

    def commit_rename():
        nonlocal renaming, rename_buf, rename_cursor, rename_target_id, tasks
        new_title = "".join(rename_buf).strip()
        if rename_target_id is not None and new_title:
            row = next(
                (t for t in tasks if t[0] == rename_target_id), None
            )
            if row is not None and new_title != row[1]:
                push_undo()
                update_title(conn, rename_target_id, new_title)
                tasks = list_tasks(conn)
        renaming = False
        rename_buf = []
        rename_cursor = 0
        rename_target_id = None

    try:
        while True:
            h, w = stdscr.getmaxyx()
            max_sel = len(tasks)
            selected = max(0, min(selected, max_sel))

            scroll = render_main(
                stdscr, tasks, selected, scroll,
                input_buf, input_cursor,
                renaming, rename_buf, rename_cursor,
            )

            ch = stdscr.get_wch()

            # --- RENAME MODE ---
            if renaming:
                if ch == "\n" or ch == "\r":
                    commit_rename()
                    continue
                if ch == "\x1b":
                    tok = decode_escape(stdscr)
                    if tok is ESC_BARE:
                        commit_rename()
                    elif tok == ESC_ALT_BACKSPACE:
                        rename_cursor = delete_word_back(rename_buf, rename_cursor)
                    elif tok == ESC_WORD_LEFT:
                        rename_cursor = word_left(rename_buf, rename_cursor)
                    elif tok == ESC_WORD_RIGHT:
                        rename_cursor = word_right(rename_buf, rename_cursor)
                    continue
                if ch == "\x03":
                    commit_rename()
                    break
                if ch == CTRL_W:
                    rename_cursor = delete_word_back(rename_buf, rename_cursor)
                elif is_backspace(ch):
                    if rename_cursor > 0:
                        del rename_buf[rename_cursor - 1]
                        rename_cursor -= 1
                elif ch == curses.KEY_DC:
                    if rename_cursor < len(rename_buf):
                        del rename_buf[rename_cursor]
                elif ch == curses.KEY_LEFT:
                    rename_cursor = max(0, rename_cursor - 1)
                elif ch == curses.KEY_RIGHT:
                    rename_cursor = min(len(rename_buf), rename_cursor + 1)
                elif ch == curses.KEY_HOME:
                    rename_cursor = 0
                elif ch == curses.KEY_END:
                    rename_cursor = len(rename_buf)
                elif ch == curses.KEY_RESIZE:
                    pass
                elif isinstance(ch, str) and len(ch) == 1 and ch.isprintable():
                    rename_buf.insert(rename_cursor, ch)
                    rename_cursor += 1
                # any other key ignored
                continue

            if ch == "\x03":
                break

            # --- INPUT MODE ---
            if selected == 0:
                avail_input = max(1, w - len(INPUT_PREFIX))
                if ch == curses.KEY_DOWN:
                    text_len = len(input_buf)
                    col = input_cursor % avail_input
                    target_line = input_cursor // avail_input + 1
                    target_cursor = target_line * avail_input + col
                    if target_cursor <= text_len:
                        input_cursor = target_cursor
                    elif target_line * avail_input <= text_len:
                        input_cursor = text_len
                    else:
                        if tasks:
                            selected = 1
                elif ch == curses.KEY_UP:
                    if input_cursor >= avail_input:
                        input_cursor -= avail_input
                elif ch == curses.KEY_LEFT:
                    input_cursor = max(0, input_cursor - 1)
                elif ch == curses.KEY_RIGHT:
                    input_cursor = min(len(input_buf), input_cursor + 1)
                elif ch == curses.KEY_HOME:
                    input_cursor = 0
                elif ch == curses.KEY_END:
                    input_cursor = len(input_buf)
                elif ch == "\n" or ch == "\r":
                    title = "".join(input_buf).strip()
                    if title:
                        push_undo()
                        add_task(conn, title)
                        tasks = list_tasks(conn)
                        input_buf = []
                        input_cursor = 0
                elif ch == "\x1b":
                    tok = decode_escape(stdscr)
                    if tok is ESC_BARE:
                        input_buf = []
                        input_cursor = 0
                    elif tok == ESC_ALT_BACKSPACE:
                        input_cursor = delete_word_back(input_buf, input_cursor)
                    elif tok == ESC_WORD_LEFT:
                        input_cursor = word_left(input_buf, input_cursor)
                    elif tok == ESC_WORD_RIGHT:
                        input_cursor = word_right(input_buf, input_cursor)
                elif ch == CTRL_W:
                    input_cursor = delete_word_back(input_buf, input_cursor)
                elif is_backspace(ch):
                    if input_buf:
                        if input_cursor > 0:
                            del input_buf[input_cursor - 1]
                            input_cursor -= 1
                    else:
                        sel = apply_undo()
                        if sel is not None:
                            tasks = list_tasks(conn)
                            selected = max(0, min(sel, len(tasks)))
                elif ch == curses.KEY_DC:
                    if input_cursor < len(input_buf):
                        del input_buf[input_cursor]
                elif ch == curses.KEY_RESIZE:
                    pass
                elif isinstance(ch, str) and len(ch) == 1 and ch.isprintable():
                    input_buf.insert(input_cursor, ch)
                    input_cursor += 1

            # --- TASK MODE ---
            else:
                task_idx = selected - 1
                if task_idx >= len(tasks):
                    selected = len(tasks)
                    continue
                tid, title, notes, _done, status = tasks[task_idx]

                if ch == "q" or ch == "Q":
                    break
                elif ch == curses.KEY_UP:
                    selected = max(0, selected - 1)
                elif ch == curses.KEY_DOWN:
                    selected = min(max_sel, selected + 1)
                elif ch == curses.KEY_HOME:
                    selected = 0
                elif ch == curses.KEY_END:
                    selected = max_sel
                elif ch == curses.KEY_PPAGE:
                    selected = max(0, selected - max(1, h - 2))
                elif ch == curses.KEY_NPAGE:
                    selected = min(max_sel, selected + max(1, h - 2))
                elif ch == curses.KEY_SR:
                    push_undo()
                    if move_task(conn, tid, -1):
                        tasks = list_tasks(conn)
                        selected -= 1
                    else:
                        undo_stack.pop()
                elif ch == curses.KEY_SF:
                    push_undo()
                    if move_task(conn, tid, 1):
                        tasks = list_tasks(conn)
                        selected += 1
                    else:
                        undo_stack.pop()
                elif ch == " ":
                    push_undo()
                    toggle_done(conn, tid)
                    tasks = list_tasks(conn)
                    new_idx = next(
                        (i for i, t in enumerate(tasks) if t[0] == tid), None
                    )
                    if new_idx is not None:
                        selected = new_idx + 1
                elif ch == "\t":
                    push_undo()
                    cycle_status(conn, tid, 1)
                    tasks = list_tasks(conn)
                elif ch == curses.KEY_BTAB:
                    push_undo()
                    cycle_status(conn, tid, -1)
                    tasks = list_tasks(conn)
                elif ch == "\n" or ch == "\r":
                    new = edit_notes(stdscr, title, notes, status)
                    if new is not None and new != notes:
                        push_undo()
                        update_notes(conn, tid, new)
                        tasks = list_tasks(conn)
                elif ch == "c" or ch == "C":
                    if notes:
                        copy_to_clipboard(notes)
                        for _ in range(2):
                            scroll = render_main(
                                stdscr, tasks, selected, scroll,
                                input_buf, input_cursor,
                                False, rename_buf, rename_cursor,
                                flash_task_idx=task_idx,
                            )
                            curses.napms(80)
                            scroll = render_main(
                                stdscr, tasks, selected, scroll,
                                input_buf, input_cursor,
                                False, rename_buf, rename_cursor,
                            )
                            curses.napms(80)
                elif ch == curses.KEY_RIGHT:
                    urls = find_links(notes, title)
                    if urls:
                        pick_link(stdscr, title, urls, status)
                elif ch == F2_KEY:
                    renaming = True
                    rename_buf = list(title)
                    rename_cursor = len(rename_buf)
                    rename_target_id = tid
                elif ch == "x" or ch == "X" or ch == curses.KEY_DC:
                    push_undo()
                    delete_task(conn, tid)
                    tasks = list_tasks(conn)
                    if not tasks:
                        selected = 0
                    else:
                        selected = min(selected, len(tasks))
                elif is_backspace(ch):
                    sel = apply_undo()
                    if sel is not None:
                        tasks = list_tasks(conn)
                        selected = max(0, min(sel, len(tasks)))
                elif ch == "\x1b":
                    decode_escape(stdscr)
                elif ch == curses.KEY_RESIZE:
                    pass
    finally:
        conn.close()


def main():
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
