#!/usr/bin/env python3
"""Curses console for the HKMP server running in this container.

The server itself is driven the only way it can be driven: by typing into the
screen session that owns its pty. Everything this console shows is read back
either from the rolling log or from the JSON state files the server maintains,
so nothing here is a second source of truth.

Launched by the `hkmp` wrapper, which restarts it if it dies.
"""

import curses
import json
import locale
import os
import re
import subprocess
import time
from collections import deque

SESSION = os.environ.get("HKMP_SESSION", "hkmp")
DATA_DIR = os.environ.get("HKMP_DATA", "/data")
PAYLOAD_DIR = os.environ.get("HKMP_HOME", "/opt/hkmp")
PORT = os.environ.get("PORT", "2222")

LOG_FILE = os.path.join(DATA_DIR, "logs", "server.log")
SETTINGS_FILE = os.path.join(DATA_DIR, "serversettings.json")
WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
BANNED_FILE = os.path.join(DATA_DIR, "banned.json")
AUTHORIZED_FILE = os.path.join(DATA_DIR, "authorized.json")

SCROLLBACK = 2000
MIN_HEIGHT, MIN_WIDTH = 16, 60

# Upstream command set, for tab completion only.
COMMANDS = [
    "announce", "auth", "ban", "banip", "copysave", "deauth", "exit", "kick",
    "list", "log", "set", "skin", "team", "unban", "unbanip", "whitelist",
]

PLAYERS_RE = re.compile(r"^\[INFO\] Online players \((\d+)\):\s*(.*)$")
LEVEL_RE = re.compile(r"^\[(\w+)\]")

C_HEADER, C_INFO, C_WARN, C_ERROR, C_DIM, C_SELECT, C_TITLE = range(1, 8)


# ---------------------------------------------------------------------------
# Talking to the server
# ---------------------------------------------------------------------------

class Server:
    """The screen session and the state files beside it."""

    def __init__(self):
        self.version = self._read_text(os.path.join(PAYLOAD_DIR, "version"), "unknown")

    @staticmethod
    def _read_text(path, default=""):
        try:
            with open(path) as handle:
                return handle.read().strip()
        except OSError:
            return default

    @staticmethod
    def _read_json(path, default):
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def send(self, command):
        """Type a command into the server console."""
        try:
            subprocess.run(
                ["screen", "-S", SESSION, "-p", "0", "-X", "stuff", command + "\r"],
                check=True, capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    def alive(self):
        try:
            result = subprocess.run(
                ["screen", "-S", SESSION, "-Q", "select", "."],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    def settings(self):
        return self._read_json(SETTINGS_FILE, {})

    def whitelist(self):
        return self._read_json(WHITELIST_FILE, {})

    def bans(self):
        return self._read_json(BANNED_FILE, {})

    def authorized(self):
        return self._read_json(AUTHORIZED_FILE, {})

    @staticmethod
    def pid():
        """The mono process running the server, or None."""
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    if b"HKMPServer.exe" in handle.read():
                        return int(entry)
            except OSError:
                continue
        return None

    @staticmethod
    def uptime(pid):
        """Seconds since the server process started, or None."""
        try:
            with open(f"/proc/{pid}/stat") as handle:
                # The comm field may itself contain spaces and brackets, so the
                # remaining fields are taken from after the final ')'.
                fields = handle.read().rsplit(")", 1)[1].split()
            start_ticks = int(fields[19])
            with open("/proc/uptime") as handle:
                up = float(handle.read().split()[0])
            return up - start_ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None


class LogTail:
    """Follows the rolling log, surviving the roll."""

    def __init__(self, path):
        self.path = path
        self._handle = None
        self._inode = None

    def _open(self):
        try:
            handle = open(self.path, "r", errors="replace")
        except OSError:
            return None
        self._handle = handle
        self._inode = os.fstat(handle.fileno()).st_ino
        return handle

    def start(self, lines=400):
        """Open the log and return the tail of what is already in it."""
        handle = self._open()
        if handle is None:
            return []
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - 64 * 1024))
        if size > 64 * 1024:
            handle.readline()  # discard the partial line the seek landed in
        existing = handle.read().splitlines()
        return existing[-lines:]

    def read(self):
        """Whatever has been appended since the last call."""
        if self._handle is None and self._open() is None:
            return []

        new = self._handle.read().splitlines()

        # A roll replaces the file under us; reopen when the name no longer
        # points at the inode we hold.
        try:
            if os.stat(self.path).st_ino != self._inode:
                self._handle.close()
                if self._open() is not None:
                    new += self._handle.read().splitlines()
        except OSError:
            pass
        return new


# ---------------------------------------------------------------------------
# Small curses helpers
# ---------------------------------------------------------------------------

def init_colors():
    if not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    curses.init_pair(C_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_INFO, curses.COLOR_WHITE, background)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, background)
    curses.init_pair(C_ERROR, curses.COLOR_RED, background)
    curses.init_pair(C_DIM, curses.COLOR_BLUE, background)
    curses.init_pair(C_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_TITLE, curses.COLOR_CYAN, background)
    return True


def line_attr(line, colored):
    if not colored:
        return curses.A_NORMAL
    match = LEVEL_RE.match(line)
    level = match.group(1).upper() if match else ""
    if level == "ERROR":
        return curses.color_pair(C_ERROR) | curses.A_BOLD
    if level == "WARN":
        return curses.color_pair(C_WARN)
    if level in ("DEBUG", "FINE"):
        return curses.color_pair(C_DIM)
    return curses.A_NORMAL


def put(win, y, x, text, attr=curses.A_NORMAL):
    """Write text, clipped to the window, swallowing the bottom-right error."""
    height, width = win.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    text = text[: max(0, width - x - 1)]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def swallow_escape_tail(win, restore=-1):
    """Eat the rest of a key sequence whose Esc was reported on its own.

    Over a slow hop the bytes of a function or arrow key can arrive far enough
    apart that curses stops waiting and hands back a bare Esc. Without this the
    remainder ("[B" and friends) would be typed into the command line.
    """
    win.timeout(60)
    try:
        first = win.getch()
        if first == -1:
            return
        if first not in (ord("["), ord("O")):
            curses.ungetch(first)
            return
        for _ in range(16):
            following = win.getch()
            if following == -1 or 0x40 <= following <= 0x7E:
                return
    finally:
        win.timeout(restore)


def popup(stdscr, height, width, title, colored):
    screen_h, screen_w = stdscr.getmaxyx()
    height = min(height, screen_h - 2)
    width = min(width, screen_w - 2)
    win = curses.newwin(height, width, (screen_h - height) // 2, (screen_w - width) // 2)
    win.keypad(True)
    win.erase()
    win.box()
    attr = curses.color_pair(C_TITLE) | curses.A_BOLD if colored else curses.A_BOLD
    put(win, 0, 2, f" {title} ", attr)
    return win


def prompt(stdscr, title, label, colored):
    """Ask for one line of text. Returns None when cancelled."""
    win = popup(stdscr, 7, max(44, len(label) + 8), title, colored)
    put(win, 2, 2, label)
    put(win, 5, 2, "Enter confirm · Esc cancel", curses.A_DIM)
    win.refresh()

    buffer = ""
    while True:
        put(win, 3, 2, " " * (win.getmaxyx()[1] - 4))
        put(win, 3, 2, "> " + buffer[-(win.getmaxyx()[1] - 7):], curses.A_BOLD)
        win.refresh()
        try:
            key = win.getch()
        except KeyboardInterrupt:
            return None
        if key == 27:
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return buffer.strip() or None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            buffer = buffer[:-1]
        elif 32 <= key < 127:
            buffer += chr(key)


def confirm(stdscr, question, colored):
    win = popup(stdscr, 7, max(44, len(question) + 6), "Confirm", colored)
    put(win, 2, 2, question)
    put(win, 4, 2, "y confirm · any other key cancel", curses.A_DIM)
    win.refresh()
    try:
        return win.getch() in (ord("y"), ord("Y"))
    except KeyboardInterrupt:
        return False


def chooser(stdscr, title, rows, actions, colored, empty="Nothing here.",
            selectable=True, start=0):
    """Scrollable list. Returns (action_char, index) or None when closed.

    `rows` is a list of strings; `actions` is the hint line along the bottom
    and doubles as the set of keys that are passed back to the caller. Set
    `selectable` to False for a panel that is only text.
    """
    # Enter arrives as a newline, which must never reach the hint line: curses
    # would act on it and push the rest of the hint onto the border.
    labels = [(("Enter" if char == "\n" else char) + " " + label)
              for char, label in actions]
    hint = " · ".join(labels + ["Esc/q close"])

    screen_h, screen_w = stdscr.getmaxyx()
    content = max((len(r) for r in rows), default=0)
    width = min(max(50, content + 6, len(hint) + 6), screen_w - 2)
    height = min(max(len(rows), 1) + 5, screen_h - 2)
    win = popup(stdscr, height, width, title, colored)

    visible = height - 5
    selected = min(max(0, start), max(0, len(rows) - 1))
    top = max(0, selected - visible + 1)
    accepted = {char for char, _ in actions}

    while True:
        for row in range(visible):
            put(win, 2 + row, 1, " " * (width - 2))
        if not rows:
            put(win, 2, 2, empty, curses.A_DIM)
        for row in range(min(visible, len(rows))):
            index = top + row
            if index >= len(rows):
                break
            chosen = selectable and index == selected
            attr = (curses.color_pair(C_SELECT) if colored else curses.A_REVERSE) \
                if chosen else curses.A_NORMAL
            marker = ("> " if chosen else "  ") if selectable else "  "
            put(win, 2 + row, 2, marker + rows[index], attr)

        put(win, height - 2, 2, " " * (width - 4))
        put(win, height - 2, 2, hint[: width - 4], curses.A_DIM)
        win.refresh()

        try:
            key = win.getch()
        except KeyboardInterrupt:
            return None

        if key == 27:
            swallow_escape_tail(win)
            return None
        if key == curses.KEY_F10 or (0 < key < 256 and chr(key) == "q"
                                     and "q" not in accepted):
            return None
        if key == curses.KEY_UP and selected > 0:
            selected -= 1
        elif key == curses.KEY_DOWN and selected < len(rows) - 1:
            selected += 1
        elif key == curses.KEY_PPAGE:
            selected = max(0, selected - visible)
        elif key == curses.KEY_NPAGE:
            selected = min(len(rows) - 1, selected + visible) if rows else 0
        elif 0 < key < 256 and chr(key).lower() in accepted:
            return chr(key).lower(), selected
        elif key in (curses.KEY_ENTER, 10, 13) and "\n" in accepted:
            return "\n", selected

        if selected < top:
            top = selected
        elif selected >= top + visible:
            top = selected - visible + 1


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

class Console:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.server = Server()
        self.tail = LogTail(LOG_FILE)
        self.lines = deque(maxlen=SCROLLBACK)
        self.history = []
        self.history_index = None
        self.buffer = ""
        self.cursor = 0
        self.scroll = 0           # lines scrolled up from the bottom
        self.players = []
        self.player_count = None
        self.quiet_until = 0.0    # suppress the echo of our own `list`
        self.status = ""
        self.status_until = 0.0
        self.colored = False

    # -- plumbing ----------------------------------------------------------

    def notify(self, message, seconds=4):
        self.status = message
        self.status_until = time.time() + seconds

    def send(self, command):
        if not self.server.send(command):
            self.notify("could not reach the server console")
            return False
        return True

    def refresh_players(self):
        """Ask for the player list, hiding the question and the answer."""
        self.quiet_until = time.time() + 3
        self.send("list")

    def pump(self):
        """Drain the log into the buffer, picking out what we asked for."""
        for line in self.tail.read():
            match = PLAYERS_RE.match(line)
            if match:
                self.player_count = int(match.group(1))
                names = match.group(2).strip()
                self.players = [n.strip() for n in names.split(",") if n.strip()]
                if time.time() < self.quiet_until:
                    self.quiet_until = 0.0
                    continue
            elif line.strip() == "[INFO] list" and time.time() < self.quiet_until:
                continue
            self.lines.append(line)

            # Someone joined or left: the count we show is now stale.
            if "connection request from" in line or "PlayerDisconnect" in line:
                self.refresh_players()

        if self.scroll:
            # Keep the viewport anchored while the user is reading history.
            self.scroll = min(self.scroll, max(0, len(self.lines) - 1))

    # -- drawing -----------------------------------------------------------

    def draw(self):
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()

        if height < MIN_HEIGHT or width < MIN_WIDTH:
            put(self.stdscr, 0, 0,
                f"Terminal too small: need at least {MIN_WIDTH}x{MIN_HEIGHT}.")
            self.stdscr.refresh()
            return

        self.draw_header(width)
        self.draw_log(height, width)
        self.draw_input(height, width)
        self.draw_footer(height, width)
        self.stdscr.refresh()

    def draw_header(self, width):
        pid = self.server.pid()
        if pid is None:
            state = "server DOWN"
        else:
            seconds = self.server.uptime(pid)
            state = f"up {format_duration(seconds)}" if seconds else "running"

        players = "?" if self.player_count is None else str(self.player_count)
        left = f" HKMP {self.server.version} · {PORT}/udp · players {players} · {state}"
        attr = curses.color_pair(C_HEADER) if self.colored else curses.A_REVERSE
        put(self.stdscr, 0, 0, left.ljust(width - 1), attr)

    def draw_log(self, height, width):
        rows = height - 3
        end = len(self.lines) - self.scroll
        start = max(0, end - rows)
        view = list(self.lines)[start:end]
        for index, line in enumerate(view):
            put(self.stdscr, 1 + index, 0, line, line_attr(line, self.colored))
        if self.scroll:
            marker = f" scrolled {self.scroll} lines · End to follow "
            put(self.stdscr, height - 3, max(0, width - len(marker) - 1), marker,
                curses.A_REVERSE)

    def draw_input(self, height, width):
        put(self.stdscr, height - 2, 0, "> ", curses.A_BOLD)
        room = width - 4
        shown = self.buffer
        offset = 0
        if self.cursor > room:
            offset = self.cursor - room
            shown = self.buffer[offset:]
        put(self.stdscr, height - 2, 2, shown[:room])
        try:
            self.stdscr.move(height - 2, 2 + self.cursor - offset)
        except curses.error:
            pass

    def draw_footer(self, height, width):
        if self.status and time.time() < self.status_until:
            text = " " + self.status
        else:
            text = (" F1 Help · F2 Players · F3 Settings · F4 Whitelist · "
                    "F5 Bans · F10 Quit")
        attr = curses.color_pair(C_HEADER) if self.colored else curses.A_REVERSE
        put(self.stdscr, height - 1, 0, text.ljust(width - 1), attr)

    # -- panels ------------------------------------------------------------

    def panel_help(self):
        rows = [
            "Type any server command below and press Enter.",
            "Upstream commands: list, set, announce, kick, ban, unban,",
            "banip, unbanip, whitelist, auth, deauth, team, skin, copysave, log.",
            "",
            "F2   players, with kick / ban / authorize",
            "F3   server settings, toggled in place",
            "F4   whitelist and pre-list",
            "F5   bans, by auth key and by IP",
            "F10  quit this console (the server keeps running)",
            "",
            "Tab        complete a command name",
            "Up/Down    command history",
            "PgUp/PgDn  scroll the log · End follows it again",
            "Esc or q   close a panel",
            "",
            "Ctrl-C does nothing here, so it cannot stop the server by accident.",
            "Typing 'exit' does stop it, and asks first.",
        ]
        chooser(self.stdscr, "Help", rows, [], self.colored,
                selectable=False)

    def panel_players(self):
        self.refresh_players()
        time.sleep(0.6)
        self.pump()

        while True:
            rows = self.players or []
            result = chooser(
                self.stdscr, f"Players online ({len(rows)})", rows,
                [("k", "kick"), ("b", "ban"), ("i", "ban IP"), ("a", "authorize"),
                 ("r", "refresh")],
                self.colored, empty="Nobody is connected.")
            if result is None:
                return
            action, index = result

            if action == "r":
                self.refresh_players()
                time.sleep(0.6)
                self.pump()
                continue
            if not rows:
                continue

            name = rows[index]
            if action == "k" and confirm(self.stdscr, f"Kick {name}?", self.colored):
                self.send(f"kick {name}")
            elif action == "b" and confirm(self.stdscr, f"Ban {name}?", self.colored):
                self.send(f"ban {name}")
            elif action == "i" and confirm(self.stdscr, f"Ban the IP of {name}?", self.colored):
                self.send(f"banip {name}")
            elif action == "a":
                self.send(f"auth {name}")
            time.sleep(0.4)
            self.pump()
            if action in ("k", "b", "i"):
                self.refresh_players()
                time.sleep(0.6)
                self.pump()

    def panel_settings(self):
        position = 0
        while True:
            settings = self.server.settings()
            if not settings:
                self.notify("serversettings.json is not readable yet")
                return

            # Booleans first: those are the ones anyone actually flips.
            names = sorted(settings, key=lambda n: (not isinstance(settings[n], bool), n))
            rows = [f"{name:<42} {format_value(settings[name])}" for name in names]

            result = chooser(
                self.stdscr, "Server settings", rows,
                [("\n", "change")], self.colored, start=position)
            if result is None:
                return
            _, index = result
            position = index

            name = names[index]
            value = settings[name]
            if isinstance(value, bool):
                self.send(f"set {name} {str(not value).lower()}")
            else:
                entered = prompt(self.stdscr, name, f"New value (now {value}):",
                                 self.colored)
                if entered is None:
                    continue
                self.send(f"set {name} {entered}")
            # The server rewrites the file as part of handling `set`.
            time.sleep(0.5)
            self.pump()

    def panel_whitelist(self):
        position = 0
        while True:
            data = self.server.whitelist()
            enabled = bool(data.get("enabled"))
            approved = sorted(data.get("approved", []))
            prelisted = sorted(data.get("pre-listed", []))

            rows = [f"[{'x' if enabled else ' '}] whitelist enabled"]
            rows += [f"    pre-listed  {name}" for name in prelisted]
            rows += [f"    key         {key}" for key in approved]

            result = chooser(
                self.stdscr, "Whitelist", rows,
                [("\n", "toggle"), ("a", "add name"), ("d", "remove")],
                self.colored, start=position)
            if result is None:
                return
            action, index = result
            position = index

            if action == "\n" and index == 0:
                self.send("whitelist " + ("off" if enabled else "on"))
            elif action == "a":
                name = prompt(self.stdscr, "Whitelist", "Name or auth key to add:",
                              self.colored)
                if name:
                    self.send(f"whitelist add {name}")
            elif action == "d" and index > 0:
                entry = rows[index].split(None, 1)[1].strip()
                if confirm(self.stdscr, f"Remove {entry} from the whitelist?",
                           self.colored):
                    self.send(f"whitelist remove {entry}")
            time.sleep(0.5)
            self.pump()

    def panel_bans(self):
        position = 0
        while True:
            data = self.server.bans()
            keys = sorted(data.get("approved", []))
            addresses = sorted(data.get("ip-addresses", []))

            rows = [f"key  {key}" for key in keys]
            rows += [f"ip   {address}" for address in addresses]

            result = chooser(
                self.stdscr, "Bans", rows, [("u", "unban")], self.colored,
                empty="Nobody is banned.", start=position)
            if result is None:
                return
            action, index = result
            position = index
            if action != "u" or not rows:
                continue

            kind, value = rows[index].split(None, 1)
            value = value.strip()
            if confirm(self.stdscr, f"Unban {value}?", self.colored):
                self.send(f"unban {value}" if kind == "key" else f"unbanip {value}")
                time.sleep(0.5)
                self.pump()

    # -- input -------------------------------------------------------------

    def complete(self):
        word = self.buffer[: self.cursor]
        if " " in word.strip():
            return
        matches = [c for c in COMMANDS if c.startswith(word.strip())]
        if len(matches) == 1:
            self.buffer = matches[0] + " " + self.buffer[self.cursor:]
            self.cursor = len(matches[0]) + 1
        elif matches:
            self.notify("  ".join(matches))

    def submit(self):
        command = self.buffer.strip()
        self.buffer, self.cursor = "", 0
        if not command:
            return
        self.history.append(command)
        self.history_index = None

        # `exit` is a real server command and stops it for everyone.
        if command.split()[0].lower() == "exit":
            if not confirm(self.stdscr,
                           "This stops the server for everyone. Continue?",
                           self.colored):
                return
        self.send(command)

    def recall(self, step):
        if not self.history:
            return
        if self.history_index is None:
            self.history_index = len(self.history)
        self.history_index = max(0, min(len(self.history), self.history_index + step))
        self.buffer = "" if self.history_index == len(self.history) \
            else self.history[self.history_index]
        self.cursor = len(self.buffer)

    def handle(self, key):
        height = self.stdscr.getmaxyx()[0]
        page = max(1, height - 5)

        if key == curses.KEY_F10:
            return False
        if key == curses.KEY_F1:
            self.panel_help()
        elif key == curses.KEY_F2:
            self.panel_players()
        elif key == curses.KEY_F3:
            self.panel_settings()
        elif key == curses.KEY_F4:
            self.panel_whitelist()
        elif key == curses.KEY_F5:
            self.panel_bans()
        elif key == curses.KEY_PPAGE:
            self.scroll = min(len(self.lines) - 1, self.scroll + page)
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - page)
        elif key == curses.KEY_END:
            self.scroll = 0
        elif key in (curses.KEY_ENTER, 10, 13):
            self.submit()
        elif key == 9:
            self.complete()
        elif key == curses.KEY_UP:
            self.recall(-1)
        elif key == curses.KEY_DOWN:
            self.recall(1)
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1:]
        elif key == 21:  # Ctrl-U
            self.buffer, self.cursor = self.buffer[self.cursor:], 0
        elif key == 27:
            swallow_escape_tail(self.stdscr, restore=250)
            self.buffer, self.cursor = "", 0
        elif key == curses.KEY_RESIZE:
            pass
        elif 32 <= key < 127:
            self.buffer = self.buffer[: self.cursor] + chr(key) + self.buffer[self.cursor:]
            self.cursor += 1
        return True

    # -- main loop ---------------------------------------------------------

    def run(self):
        self.colored = init_colors()
        # The default second-long wait to prove a lone Esc is not the start of
        # a key sequence makes closing a panel feel stuck. Too short a wait is
        # worse though: over a slow ssh hop the bytes of a real sequence arrive
        # split up and its tail lands in the command line. ncurses' own
        # ESCDELAY is honoured so a laggy link can raise it.
        try:
            curses.set_escdelay(int(os.environ.get("ESCDELAY", "100")))
        except (AttributeError, ValueError, curses.error):
            pass
        curses.curs_set(1)
        self.stdscr.keypad(True)
        self.stdscr.timeout(250)

        for line in self.tail.start():
            self.lines.append(line)
        self.refresh_players()

        while True:
            self.pump()
            self.draw()
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                # Ctrl-C must not reach the server, and must not end the
                # console either: F10 is the only way out.
                self.notify("Ctrl-C ignored — press F10 to quit")
                continue
            if key == -1:
                continue
            if not self.handle(key):
                return


def format_duration(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_value(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def main():
    # curses only handles characters wider than one byte once the locale has
    # been taken from the environment.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    server = Server()
    if not server.alive():
        raise SystemExit("hkmp: the server console is not running")
    curses.wrapper(lambda stdscr: Console(stdscr).run())


if __name__ == "__main__":
    main()
