#!/usr/bin/env python3
"""Simple terminal todo app — curses + SQLite, single file, stdlib only."""

import curses
import locale
import sqlite3
from pathlib import Path

locale.setlocale(locale.LC_ALL, "")

DB_PATH = Path.home() / ".todo.db"
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, "\x7f", "\x08")
UNDO_DEPTH = 100

HELP_INPUT_BAR = (
    " ⏎ add · ↓ tasks · ⌃w word · esc clear · ⌫ undo · ⌃c quit "
)
HELP_TASK_BAR = (
    " ↑↓ nav · ⇧↑↓ move · ␣ done · ⏎ notes · F2 rename · x del · ⌫ undo · q quit "
)
HELP_RENAME_BAR = " type · ⏎/esc save · ⌃w word "
HELP_NOTES = " ⏎ newline · esc save & close "

INPUT_PREFIX = " + "
INPUT_INDENT = "   "
INPUT_PLACEHOLDER = "New task..."

TASK_PREFIX_LEN = 7  # " [x] · "
TASK_INDENT = " " * TASK_PREFIX_LEN

F2_KEY = curses.KEY_F0 + 2
CTRL_W = "\x17"


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
                done INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "done" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN done INTEGER NOT NULL DEFAULT 0"
            )
    return conn


def list_tasks(conn):
    return conn.execute(
        "SELECT id, title, notes, done FROM tasks "
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


def snapshot(conn):
    return conn.execute(
        "SELECT id, title, notes, position, done FROM tasks"
    ).fetchall()


def restore(conn, snap):
    with conn:
        conn.execute("DELETE FROM tasks")
        conn.executemany(
            "INSERT INTO tasks (id, title, notes, position, done) "
            "VALUES (?, ?, ?, ?, ?)",
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


def is_backspace(ch):
    return ch in BACKSPACE_KEYS


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


def read_alt_modifier(stdscr):
    """Peek for an immediately-following char after Esc. Returns char or None.
    Lets us detect Alt+key combos (sent as ESC+key by terminals)."""
    stdscr.nodelay(True)
    try:
        nxt = stdscr.get_wch()
    except curses.error:
        nxt = None
    finally:
        stdscr.nodelay(False)
    return nxt


# --- Notes editor ---

def edit_notes(stdscr, title, initial_text):
    """Returns the edited text. Esc always saves. None only if nothing changed."""
    original = initial_text
    lines = initial_text.split("\n") if initial_text else [""]
    if not lines:
        lines = [""]
    cy, cx = 0, 0
    scroll_y = 0  # in display rows
    set_cursor(True)

    def word_delete_back():
        nonlocal cy, cx
        if cx == 0:
            if cy > 0:
                prev = lines[cy - 1]
                lines[cy - 1] = prev + lines[cy]
                del lines[cy]
                cy -= 1
                cx = len(prev)
            return
        line = lines[cy]
        new_cx = cx
        while new_cx > 0 and line[new_cx - 1] == " ":
            new_cx -= 1
        while new_cx > 0 and line[new_cx - 1] != " ":
            new_cx -= 1
        lines[cy] = line[:new_cx] + line[cx:]
        cx = new_cx

    def layout(width):
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
                # End of this segment. If the next row in this line starts at
                # the same position, there's no eaten space — cursor goes there.
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

    try:
        while True:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            fill_line(stdscr, 0, 0, w, " notes — " + title + " ", curses.A_REVERSE)

            display_rows, cur_row, cur_col = layout(w)
            edit_h = max(1, h - 2)
            if cur_row < scroll_y:
                scroll_y = cur_row
            elif cur_row >= scroll_y + edit_h:
                scroll_y = cur_row - edit_h + 1
            scroll_y = max(0, min(scroll_y, max(0, len(display_rows) - edit_h)))

            for i in range(edit_h):
                ri = scroll_y + i
                if ri >= len(display_rows):
                    break
                _, _, text = display_rows[ri]
                fill_line(stdscr, 1 + i, 0, w, text)

            fill_line(stdscr, h - 1, 0, w - 1, HELP_NOTES, curses.A_REVERSE)
            try:
                stdscr.move(1 + (cur_row - scroll_y), min(cur_col, w - 1))
            except curses.error:
                pass
            stdscr.refresh()

            ch = stdscr.get_wch()
            if ch == "\x03":
                new_text = "\n".join(lines)
                return None if new_text == original else new_text
            if ch == "\x1b":
                nxt = read_alt_modifier(stdscr)
                if nxt is None:
                    new_text = "\n".join(lines)
                    return None if new_text == original else new_text
                if is_backspace(nxt):
                    word_delete_back()
                continue
            if ch == CTRL_W:
                word_delete_back()
            elif ch == curses.KEY_UP:
                if cur_row > 0:
                    tli, toff, ttext = display_rows[cur_row - 1]
                    cy = tli
                    cx = toff + min(cur_col, len(ttext))
            elif ch == curses.KEY_DOWN:
                if cur_row < len(display_rows) - 1:
                    tli, toff, ttext = display_rows[cur_row + 1]
                    cy = tli
                    cx = toff + min(cur_col, len(ttext))
            elif ch == curses.KEY_LEFT:
                if cx > 0:
                    cx -= 1
                elif cy > 0:
                    cy -= 1
                    cx = len(lines[cy])
            elif ch == curses.KEY_RIGHT:
                if cx < len(lines[cy]):
                    cx += 1
                elif cy < len(lines) - 1:
                    cy += 1
                    cx = 0
            elif ch == curses.KEY_HOME:
                cx = 0
            elif ch == curses.KEY_END:
                cx = len(lines[cy])
            elif is_backspace(ch):
                if cx > 0:
                    lines[cy] = lines[cy][: cx - 1] + lines[cy][cx:]
                    cx -= 1
                elif cy > 0:
                    prev = lines[cy - 1]
                    lines[cy - 1] = prev + lines[cy]
                    del lines[cy]
                    cy -= 1
                    cx = len(prev)
            elif ch == curses.KEY_DC:
                if cx < len(lines[cy]):
                    lines[cy] = lines[cy][:cx] + lines[cy][cx + 1 :]
                elif cy < len(lines) - 1:
                    lines[cy] = lines[cy] + lines[cy + 1]
                    del lines[cy + 1]
            elif ch == "\n" or ch == "\r":
                tail = lines[cy][cx:]
                lines[cy] = lines[cy][:cx]
                lines.insert(cy + 1, tail)
                cy += 1
                cx = 0
            elif ch == curses.KEY_RESIZE:
                pass
            elif isinstance(ch, str) and len(ch) == 1 and ch.isprintable():
                lines[cy] = lines[cy][:cx] + ch + lines[cy][cx:]
                cx += 1
    finally:
        set_cursor(False)


# --- Main view ---

def build_render(tasks, w, rename_idx=None, rename_text=None, rename_cursor=0):
    """Return list of row dicts: {'kind', 'task_idx', 'text', 'attr'}."""
    rows = []
    split_idx = next(
        (i for i, t in enumerate(tasks) if t[3] == 1), len(tasks)
    )
    avail = max(1, w - TASK_PREFIX_LEN)
    for i, task in enumerate(tasks):
        if i == split_idx and i > 0:
            rows.append({"kind": "gap", "task_idx": None, "text": "", "attr": 0})
        _tid, title, notes, done = task
        if rename_idx == i and rename_text is not None:
            title = rename_text
        checkbox = "[x]" if done else "[ ]"
        marker = "·" if notes else " "
        title_lines = hard_wrap(title, avail)
        if rename_idx == i and rename_cursor > 0 \
                and rename_cursor == len(title) and rename_cursor % avail == 0:
            title_lines.append("")
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
                }
            )
    return rows


def render_main(stdscr, tasks, selected, scroll, input_buf, input_cursor,
                renaming, rename_buf, rename_cursor):
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    input_focused = (selected == 0) and not renaming
    prefix_len = len(INPUT_PREFIX)
    avail_input = max(1, w - prefix_len)
    input_text = "".join(input_buf)

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

    max_input_rows = max(1, h - 2)
    input_lines = input_lines[:max_input_rows]
    input_rows = len(input_lines)

    for i, line in enumerate(input_lines):
        p = INPUT_PREFIX if i == 0 else INPUT_INDENT
        attr = curses.A_NORMAL if input_focused else curses.A_DIM
        fill_line(stdscr, i, 0, w, p + line, attr)

    # --- Tasks ---
    rename_idx = (selected - 1) if (renaming and selected > 0) else None
    rename_text = "".join(rename_buf) if renaming else None
    rows = build_render(
        tasks, w,
        rename_idx=rename_idx,
        rename_text=rename_text,
        rename_cursor=rename_cursor if renaming else 0,
    )

    task_area_start = input_rows
    task_area_end = h - 1
    task_area_h = max(1, task_area_end - task_area_start)

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
            attr = curses.A_REVERSE
        fill_line(stdscr, task_area_start + i, 0, w, r["text"], attr)
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
        help_text = HELP_RENAME_BAR
    elif input_focused:
        help_text = HELP_INPUT_BAR
    else:
        help_text = HELP_TASK_BAR
    fill_line(stdscr, h - 1, 0, w - 1, help_text, curses.A_REVERSE)

    # --- Cursor placement (LAST) ---
    cursor_pos = None
    if input_focused:
        c_line = input_cursor // avail_input
        c_col = prefix_len + (input_cursor % avail_input)
        c_line = min(c_line, max_input_rows - 1)
        cursor_pos = (c_line, min(c_col, w - 1))
    elif renaming and sel_screen_first_row is not None:
        avail_task = max(1, w - TASK_PREFIX_LEN)
        c_line = rename_cursor // avail_task
        c_col = TASK_PREFIX_LEN + (rename_cursor % avail_task)
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
                    nxt = read_alt_modifier(stdscr)
                    if nxt is None:
                        commit_rename()
                    elif is_backspace(nxt):
                        rename_cursor = delete_word_back(rename_buf, rename_cursor)
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
                    nxt = read_alt_modifier(stdscr)
                    if nxt is None:
                        input_buf = []
                        input_cursor = 0
                    elif is_backspace(nxt):
                        input_cursor = delete_word_back(input_buf, input_cursor)
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
                tid, title, notes, _done = tasks[task_idx]

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
                elif ch == "\n" or ch == "\r":
                    new = edit_notes(stdscr, title, notes)
                    if new is not None and new != notes:
                        push_undo()
                        update_notes(conn, tid, new)
                        tasks = list_tasks(conn)
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
                    read_alt_modifier(stdscr)
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
