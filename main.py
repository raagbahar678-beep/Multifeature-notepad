# -*- coding: utf-8 -*-
"""
LaTeX Notepad — Kivy / Android edition
Ported from a Tkinter desktop app. The LaTeX minipage "code placer" feature
has been intentionally removed per request. Everything else (multi-tab
editing, syntax highlighting, find & replace, bookmarks, go-to-line,
copy/replace line ranges, auto-scroll, font size, text/background colour
presets, session auto-save) has been reimplemented on Kivy widgets so it can
be packaged into an Android APK with buildozer/python-for-android.

Notes on what changed vs. the Tkinter version (mobile realities):
  * No native OS colour-picker / file dialogs exist on Android -> replaced
    with a small in-app colour palette and a Kivy-native file browser.
  * No Tk Canvas gutter widget -> bookmark dots are drawn with Kivy
    Graphics instructions on a small side strip.
  * Toolbars are consolidated into a top ActionBar + slide-out side panel
    plus collapsible tool strips, instead of 5 stacked always-visible bars,
    to keep the phone screen usable.
  * Syntax highlighting is done by rebuilding markup text for the visible
    document (Kivy's CodeInput highlighting API differs from Tk tags), and
    is debounced the same way as before.
"""

import os
import re
import json
import time
from datetime import datetime

os.environ.setdefault("KIVY_NO_ARGS", "1")

from kivy.app import App
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.properties import (
    StringProperty, BooleanProperty, NumericProperty, ListProperty, ObjectProperty
)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.checkbox import CheckBox
from kivy.uix.spinner import Spinner
from kivy.uix.slider import Slider
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.widget import Widget
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.togglebutton import ToggleButton
from kivy.graphics import Color, Ellipse, Rectangle
from kivy.uix.behaviors import ButtonBehavior
from kivy.lang import Builder

# ─── Storage locations ──────────────────────────────────────────────────────
APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def _android_storage_dir():
    """Prefer the app's private external storage on Android so files persist
    across updates and don't need extra runtime permissions."""
    try:
        from android.storage import app_storage_path  # noqa
        return app_storage_path()
    except Exception:
        return APP_DIR


def _request_android_permissions():
    """Runtime storage permission request, required on API 23+ (declaring
    them in buildozer.spec alone is not enough on modern Android)."""
    try:
        from android.permissions import request_permissions, Permission
        request_permissions([
            Permission.READ_EXTERNAL_STORAGE,
            Permission.WRITE_EXTERNAL_STORAGE,
        ])
    except Exception:
        pass


STORAGE_DIR = os.environ.get('NOTEPAD_TEST_DIR') or _android_storage_dir()
STATE_FILE = os.path.join(STORAGE_DIR, "latex_notepad_session.json")

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp', '.svg', '.eps', '.pdf'}

# ─── Editor theme ───────────────────────────────────────────────────────────
BG        = (0.118, 0.118, 0.118, 1)      # #1e1e1e
FG        = (0.831, 0.831, 0.831, 1)      # #d4d4d4
GUTTER_BG = (0.145, 0.145, 0.145, 1)      # #252525
GUTTER_FG = (0.522, 0.522, 0.522, 1)      # #858585
SEL_BG    = (0.149, 0.310, 0.470, 1)      # #264f78
CUR_LINE  = (0.165, 0.165, 0.165, 1)      # #2a2a2a

BM_DOT_COLOUR  = (0.306, 0.604, 0.871, 1)  # #4e9ade
BM_DOT_OUTLINE = (0.165, 0.435, 0.659, 1)  # #2a6fa8

HIGHLIGHT_DEBOUNCE_S = 0.08
LINENUM_DEBOUNCE_S = 0.03
SESSION_SAVE_DEBOUNCE_S = 0.8

# Fixed colour palette (mobile-friendly substitute for a native colour picker)
PRESET_COLOURS = [
    ("White",       (1, 1, 1, 1)),
    ("Light Grey",  (0.83, 0.83, 0.83, 1)),
    ("Black",       (0, 0, 0, 1)),
    ("Dark Grey",   (0.12, 0.12, 0.12, 1)),
    ("Editor Grey", (0.118, 0.118, 0.118, 1)),
    ("Red",         (0.91, 0.30, 0.24, 1)),
    ("Orange",      (0.90, 0.49, 0.13, 1)),
    ("Yellow",      (0.95, 0.77, 0.06, 1)),
    ("Green",       (0.15, 0.68, 0.38, 1)),
    ("Teal",        (0.09, 0.63, 0.52, 1)),
    ("Blue",        (0.16, 0.50, 0.73, 1)),
    ("Purple",      (0.56, 0.27, 0.68, 1)),
]

FONT_SIZES = [10, 11, 12, 13, 14, 16, 18, 20, 24, 28, 32]

MARK_COLOURS = [
    (1.0, 0.4, 0.4, 1), (1.0, 0.8, 0.27, 1), (0.4, 1.0, 0.6, 1),
    (0.4, 0.8, 1.0, 1), (0.8, 0.53, 1.0, 1), (1.0, 0.6, 0.27, 1),
]


def now_ts():
    return datetime.now().strftime("%H:%M:%S")


# ══════════════════════════════════════════════════════════════════════════
#  Session persistence (multi-tab state, like the Tk version's JSON file)
# ══════════════════════════════════════════════════════════════════════════

def load_session():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_session(data):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"Could not save session: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  Small reusable popup helpers
# ══════════════════════════════════════════════════════════════════════════

def info_popup(title, message, size_hint=(0.85, 0.5)):
    content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
    scroller = ScrollView()
    lbl = Label(text=message, size_hint_y=None, halign="left", valign="top",
                color=FG)
    lbl.bind(texture_size=lambda i, v: setattr(lbl, "height", v[1]))
    lbl.bind(width=lambda i, v: setattr(lbl, "text_size", (v, None)))
    scroller.add_widget(lbl)
    content.add_widget(scroller)
    btn = Button(text="OK", size_hint_y=None, height=dp(44))
    content.add_widget(btn)
    popup = Popup(title=title, content=content, size_hint=size_hint,
                   background_color=(0.1, 0.1, 0.1, 1))
    btn.bind(on_release=popup.dismiss)
    popup.open()
    return popup


def confirm_popup(title, message, on_yes, size_hint=(0.85, 0.4)):
    content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
    lbl = Label(text=message, halign="left", valign="middle", color=FG)
    lbl.bind(width=lambda i, v: setattr(lbl, "text_size", (v, None)))
    content.add_widget(lbl)
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
    yes_btn = Button(text="Yes")
    no_btn = Button(text="Cancel")
    row.add_widget(no_btn)
    row.add_widget(yes_btn)
    content.add_widget(row)
    popup = Popup(title=title, content=content, size_hint=size_hint,
                   background_color=(0.1, 0.1, 0.1, 1))

    def _yes(*_):
        popup.dismiss()
        on_yes()

    yes_btn.bind(on_release=_yes)
    no_btn.bind(on_release=popup.dismiss)
    popup.open()
    return popup


def prompt_popup(title, message, initial="", on_ok=None, size_hint=(0.85, 0.4),
                  multiline=False):
    content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
    lbl = Label(text=message, size_hint_y=None, height=dp(24), color=FG)
    content.add_widget(lbl)
    ti = TextInput(text=initial, multiline=multiline, size_hint_y=None,
                    height=dp(90) if multiline else dp(44),
                    background_color=(0.18, 0.18, 0.18, 1), foreground_color=FG,
                    cursor_color=(1, 1, 1, 1))
    content.add_widget(ti)
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
    ok_btn = Button(text="OK")
    cancel_btn = Button(text="Cancel")
    row.add_widget(cancel_btn)
    row.add_widget(ok_btn)
    content.add_widget(row)
    popup = Popup(title=title, content=content, size_hint=size_hint,
                   background_color=(0.1, 0.1, 0.1, 1))

    def _ok(*_):
        val = ti.text
        popup.dismiss()
        if on_ok:
            on_ok(val)

    ok_btn.bind(on_release=_ok)
    cancel_btn.bind(on_release=popup.dismiss)
    ti.bind(on_text_validate=_ok)
    popup.open()
    return popup


# ══════════════════════════════════════════════════════════════════════════
#  Bookmark gutter (side strip, replaces the Tk Canvas gutter)
# ══════════════════════════════════════════════════════════════════════════

class BookmarkGutter(ButtonBehavior, Widget):
    """A thin strip drawn to the left of the editor. Draws a dot next to any
    bookmarked, currently-visible line, and lets the user tap it to jump /
    toggle a bookmark on that line."""

    def __init__(self, pane, **kw):
        super().__init__(**kw)
        self.pane = pane
        self.width = dp(18)
        self.size_hint_x = None
        self._line_y_map = {}
        with self.canvas.before:
            self._bg_color = Color(*GUTTER_BG)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._update_bg, size=self._update_bg)

    def _update_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    def redraw(self):
        self.canvas.after.clear()
        self._line_y_map.clear()
        ti = self.pane.text
        bm_lines = {b["line"] for b in self.pane.bookmarks}
        if not bm_lines:
            return
        window_offset = self.pane._current_window_start if self.pane.large_file_mode else 0
        # Determine visible line range from the TextInput's scroll state
        try:
            total_lines = len(ti._lines)
            line_h = ti.line_height + ti.line_spacing
            scroll_y = ti.scroll_y  # 0 = bottom in kivy TextInput internal coords
            visible_lines = max(1, int(ti.height / line_h) + 2)
            # Kivy TextInput doesn't expose top line directly and cheaply;
            # approximate using cursor / scroll fraction across total lines.
            first_visible = int((1 - min(max(scroll_y, 0), 1)) * max(total_lines - visible_lines, 0))
            first_visible = max(0, first_visible)
        except Exception:
            return
        with self.canvas.after:
            for ln in bm_lines:
                idx0 = ln - 1 - window_offset  # window-relative index
                if idx0 < first_visible - 2 or idx0 > first_visible + visible_lines + 2:
                    continue
                rel = idx0 - first_visible
                y = self.top - (rel + 0.5) * line_h
                if y < self.y - line_h or y > self.top + line_h:
                    continue
                self._line_y_map[ln] = y
                Color(*BM_DOT_OUTLINE)
                Ellipse(pos=(self.center_x - dp(5), y - dp(5)), size=(dp(10), dp(10)))
                Color(*BM_DOT_COLOUR)
                Ellipse(pos=(self.center_x - dp(3.5), y - dp(3.5)), size=(dp(7), dp(7)))

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        best_line, best_dist = None, 1e9
        for ln, y in self._line_y_map.items():
            d = abs(touch.y - y)
            if d < best_dist:
                best_dist, best_line = d, ln
        if best_line is not None:
            self.pane.goto_line(best_line, toggle_bookmark=True)
        return True


# ══════════════════════════════════════════════════════════════════════════
#  LaTeX-highlighting text input
# ══════════════════════════════════════════════════════════════════════════

_ext_pat = '|'.join(re.escape(e.lstrip('.')) for e in IMAGE_EXTS)
RE_CMD     = re.compile(r'\\[a-zA-Z@]+\*?')
RE_COMMENT = re.compile(r'%.*')
RE_IMAGE   = re.compile(r'[^\s]+\.(?:' + _ext_pat + r')', re.IGNORECASE)

CMD_COLOR     = "5aa0ff"
COMMENT_COLOR = "6a9955"
IMAGE_COLOR   = "e8b13c"


class LatexTextInput(TextInput):
    """A TextInput with lightweight LaTeX syntax highlighting via Kivy's
    markup support (bypassing _split_smart / _create_line_label per-line to
    inject bbcode colour tags around matches)."""

    def __init__(self, pane=None, **kw):
        self.pane = pane
        super().__init__(**kw)

    # Kivy calls this per visual line to build the label; we colour keywords,
    # comments, and image filenames inline. Simple & fast enough for a
    # mobile-scale editor.
    def _create_line_label(self, text, hint=False):
        if text and self.pane is not None and self.pane.highlight_enabled:
            text = self._markup_line(text)
        return super()._create_line_label(text, hint=hint)

    def _markup_line(self, text):
        # Escape existing markup-sensitive characters first
        escaped = text.replace('&', '&amp;').replace('[', '&bl;').replace(']', '&br;')
        spans = []  # (start, end, color)

        def collect(pattern, color):
            for m in pattern.finditer(text):
                spans.append((m.start(), m.end(), color))

        collect(RE_CMD, CMD_COLOR)
        collect(RE_IMAGE, IMAGE_COLOR)
        # comments last & take priority to end of line
        cm = RE_COMMENT.search(text)
        if cm:
            spans = [s for s in spans if s[0] < cm.start()]
            spans.append((cm.start(), cm.end(), COMMENT_COLOR))
        if not spans:
            return escaped
        spans.sort(key=lambda s: s[0])
        out = []
        pos = 0
        for s, e, color in spans:
            if s < pos:
                continue
            out.append(self._esc(text[pos:s]))
            out.append(f"[color=#{color}]{self._esc(text[s:e])}[/color]")
            pos = e
        out.append(self._esc(text[pos:]))
        return "".join(out)

    @staticmethod
    def _esc(t):
        return t.replace('&', '&amp;').replace('[', '&bl;').replace(']', '&br;')


# ══════════════════════════════════════════════════════════════════════════
#  Large-file support: windowed loading so multi-GB files stay smooth.
#
#  A plain Kivy TextInput holding the *entire* file content as one Python
#  string is fine up to a few MB, but falls over on huge files: the string
#  alone can exceed available memory, and TextInput's own layout/render
#  work scales with total content, not just what's visible, so the UI
#  thread blocks for a long time (the "freeze" you hit opening a 1GB file).
#
#  Strategy: for files above LARGE_FILE_THRESHOLD, never read the whole
#  file into memory. Instead:
#    1. Stream through the file once (off the UI thread) to build an index
#       of byte offsets for the start of every line. This is the only
#       full-file pass, it's O(n) sequential I/O, and it doesn't hold line
#       content — just 8 bytes (an int) per line.
#    2. Load only a "window" of WINDOW_LINES lines around the visible
#       area into the TextInput. Scrolling near the top/bottom of the
#       loaded window triggers loading the next/previous window.
#    3. Edits made while a window is loaded are recorded as an overlay
#       (window key -> edited text for that window), never written back
#       to the source file until Save, and Save streams the original file
#       through untouched except for windows the user actually edited, so
#       memory use during save stays bounded too.
# ══════════════════════════════════════════════════════════════════════════

LARGE_FILE_THRESHOLD = 1 * 1024 * 1024   # 1 MB — above this, use windowed mode.
                                          # Lowered from 8 MB: setting Kivy
                                          # TextInput.text to several MB of
                                          # content in one shot re-tokenizes
                                          # and re-lays-out the whole buffer
                                          # on the UI thread and can stall
                                          # long enough to look frozen on
                                          # phone hardware, even though the
                                          # file read itself is fast. Routing
                                          # 1-8 MB files into the already
                                          # existing windowed/indexed mode
                                          # keeps them smooth too.
WINDOW_LINES = 2000                       # lines loaded into the TextInput at once
INDEX_CHUNK = 4 * 1024 * 1024             # bytes read per indexing step (keeps UI responsive)


class LineIndex:
    """Maps line number -> byte offset for a file, built incrementally off
    the UI thread so indexing a huge file doesn't freeze the app."""

    def __init__(self, path):
        self.path = path
        self.offsets = [0]   # offsets[i] = byte offset where line i (0-based) starts
        self.total_size = os.path.getsize(path)
        self._build_pos = 0
        self.complete = False

    def build_step(self):
        """Index one chunk's worth of the file. Returns True while more
        work remains, False once indexing is complete."""
        if self.complete:
            return False
        with open(self.path, "rb") as f:
            f.seek(self._build_pos)
            chunk = f.read(INDEX_CHUNK)
        if not chunk:
            self.complete = True
            return False
        base = self._build_pos
        idx = 0
        while True:
            nl = chunk.find(b"\n", idx)
            if nl == -1:
                break
            self.offsets.append(base + nl + 1)
            idx = nl + 1
        self._build_pos += len(chunk)
        if self._build_pos >= self.total_size:
            self.complete = True
        return not self.complete

    @property
    def line_count(self):
        # offsets holds one entry per line start, plus a trailing EOF
        # sentinel *only* when the file ends with a newline (so the last
        # "line start" recorded is actually one-past-the-end). Detect that
        # case and don't count the sentinel as a real line, so line_count
        # is correct whether or not the file ends in a trailing newline.
        n = len(self.offsets)
        if self.complete and n > 1 and self.offsets[-1] == self.total_size:
            return n - 1
        return n

    def line_range_bytes(self, start_line, end_line):
        """Byte range [start, end) covering lines [start_line, end_line)."""
        start = self.offsets[start_line] if start_line < len(self.offsets) else self.total_size
        end = self.offsets[end_line] if end_line < len(self.offsets) else self.total_size
        return start, end


class LargeFileBuffer:
    """Owns windowed access to one large file: which window is currently
    loaded, any edited-but-unsaved windows, and save-back logic that never
    holds the whole file in memory.

    Dirty windows are tracked by the ORIGINAL byte range they replace, not
    by line number. This matters because an edit can add or remove lines
    within a window, which would otherwise desync every line-number-based
    lookup for the rest of the file. Byte ranges from the (immutable,
    already-indexed) original file stay valid regardless of what the user
    typed, so save-back is just "copy bytes, except swap in replacement
    text for these exact byte ranges" — simple and hard to get wrong.
    """

    def __init__(self, path, index):
        self.path = path
        self.index = index
        self.window_start = 0     # first line (0-based) of the currently loaded window
        self.window_lines = WINDOW_LINES
        # (start_byte, end_byte) -> replacement text for that original range
        self.dirty_ranges = {}
        self.encoding = "utf-8"

    def _window_byte_range(self, start_line):
        end_line = start_line + self.window_lines
        return self.index.line_range_bytes(start_line, end_line)

    def read_window(self, start_line):
        start_line = max(0, start_line)
        start_b, end_b = self._window_byte_range(start_line)
        key = (start_b, end_b)
        if key in self.dirty_ranges:
            return self.dirty_ranges[key]
        with open(self.path, "rb") as f:
            f.seek(start_b)
            raw = f.read(max(0, end_b - start_b))
        return raw.decode(self.encoding, errors="replace")

    def mark_window_dirty(self, start_line, text):
        key = self._window_byte_range(start_line)
        self.dirty_ranges[key] = text

    def has_unsaved_changes(self):
        return bool(self.dirty_ranges)

    def save_to(self, dest_path, progress_cb=None):
        """Stream the source file to dest_path, substituting any edited
        byte ranges in place, without ever holding the full file in memory."""
        tmp_path = dest_path + ".savetmp"
        ranges = sorted(self.dirty_ranges.keys())
        pos = 0
        total = self.index.total_size
        with open(self.path, "rb") as src, open(tmp_path, "wb") as out:
            for start_b, end_b in ranges:
                # copy untouched bytes before this dirty range
                self._copy_span(src, out, pos, start_b)
                out.write(self.dirty_ranges[(start_b, end_b)].encode(
                    self.encoding, errors="replace"))
                pos = end_b
                if progress_cb:
                    progress_cb(pos, total)
            # copy whatever remains after the last dirty range
            self._copy_span(src, out, pos, total)
            if progress_cb:
                progress_cb(total, total)
        os.replace(tmp_path, dest_path)
        self.dirty_ranges = {}
        self.path = dest_path
        # Byte offsets for untouched regions are unchanged, but replaced
        # regions may now differ in length, so the index must be rebuilt
        # against the freshly saved file before any further windowed
        # access (editing can safely continue; it will trigger a rebuild).
        self.index = LineIndex(dest_path)

    @staticmethod
    def _copy_span(src, out, start, end):
        if end <= start:
            return
        src.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = src.read(min(INDEX_CHUNK, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)


class EditorPane(BoxLayout):
    """A single editing tab: line numbers + bookmark gutter + text area,
    plus collapsible tool strips (goto, copy-range, replace-range, scroll)
    and a status bar. Mirrors EditorPane from the Tk app minus LaTeX
    placement."""

    is_modified = BooleanProperty(False)
    current_file = StringProperty("")

    def __init__(self, app, tab_id, **kw):
        super().__init__(orientation="vertical", **kw)
        self.app = app
        self.tab_id = tab_id
        self.current_file = ""
        self._display_name = None
        self.is_modified = False
        self.highlight_enabled = True
        self.word_wrap = True
        self.font_size_pt = 14
        self.fg_color = FG
        self.bg_color = BG
        self.bookmarks = []      # [{"line": int, "name": str}]
        self.bm_index = -1
        self.scroll_active = False
        self.scroll_direction = "down"
        self.scroll_speed = 3.0
        self._scroll_ev = None
        self._highlight_ev = None
        self._linenum_ev = None
        self._session_ev = None
        self._suspend_modified = False

        # Large-file (windowed) mode state
        self.large_file_mode = False
        self.large_buffer = None       # LargeFileBuffer, set when large_file_mode is True
        self._index_ev = None          # Clock event driving incremental indexing
        self._loading_popup = None
        self._pending_window_start = None
        self._current_window_start = 0
        self._window_dirty = False     # has the currently-loaded window been edited?


        self._build_tool_strips()
        self._build_editor_row()
        self._build_statusbar()

    # ── UI construction ─────────────────────────────────────────────────
    def _build_tool_strips(self):
        # Collapsible strip container: user can show/hide via the toolbar
        # buttons in the top ActionBar (see NotepadRoot). Each strip is a
        # thin horizontal row so several can be toggled independently
        # without eating the whole screen like 5 stacked bars would.
        self.strips = BoxLayout(orientation="vertical", size_hint_y=None)
        self.strips.bind(minimum_height=self.strips.setter("height"))
        self.add_widget(self.strips)

        self.goto_strip = self._make_goto_strip()
        self.copy_strip = self._make_copy_range_strip()
        self.replace_strip = self._make_replace_range_strip()
        self.scroll_strip = self._make_scroll_strip()
        for s in (self.goto_strip, self.copy_strip, self.replace_strip, self.scroll_strip):
            s.opacity = 0
            s.height = 0
            s.disabled = True
            self.strips.add_widget(s)

    def _toggle_strip(self, strip):
        showing = strip.height > 0
        if showing:
            strip.height = 0
            strip.opacity = 0
            strip.disabled = True
        else:
            strip.height = strip.natural_height
            strip.opacity = 1
            strip.disabled = False

    def _make_goto_strip(self):
        row = BoxLayout(size_hint_y=None, height=dp(44), padding=dp(4), spacing=dp(4))
        row.natural_height = dp(44)
        row.add_widget(Label(text="Go to line:", size_hint_x=None, width=dp(90), color=FG))
        self.goto_entry = TextInput(multiline=False, input_filter="int",
                                     size_hint_x=None, width=dp(70),
                                     background_color=(0.9, 0.9, 0.9, 1))
        self.goto_entry.bind(on_text_validate=lambda *_: self._goto_from_entry())
        row.add_widget(self.goto_entry)
        go_btn = Button(text="Go", size_hint_x=None, width=dp(60),
                         background_color=(0.15, 0.68, 0.38, 1))
        go_btn.bind(on_release=lambda *_: self._goto_from_entry())
        row.add_widget(go_btn)
        self.goto_status = Label(text="", color=(0.95, 0.6, 0.07, 1))
        row.add_widget(self.goto_status)
        with row.canvas.before:
            Color(0.10, 0.145, 0.185, 1)
            row._bg = Rectangle(pos=row.pos, size=row.size)
        row.bind(pos=lambda i, v: setattr(row._bg, "pos", v),
                  size=lambda i, v: setattr(row._bg, "size", v))
        return row

    def _goto_from_entry(self):
        raw = self.goto_entry.text.strip()
        if not raw:
            return
        try:
            n = int(raw)
        except ValueError:
            self.goto_status.text = "Enter a number"
            return
        total = self.total_lines()
        if n < 1 or n > total:
            self.goto_status.text = f"Range 1-{total}"
            return
        self.goto_line(n)
        self.goto_status.text = f"Jumped to line {n}"
        self.goto_entry.text = ""
        Clock.schedule_once(lambda dt: setattr(self.goto_status, "text", ""), 3)

    def _make_copy_range_strip(self):
        row = BoxLayout(size_hint_y=None, height=dp(48), padding=dp(4), spacing=dp(4))
        row.natural_height = dp(48)
        row.add_widget(Label(text="Copy lines:", size_hint_x=None, width=dp(85), color=FG))
        self.copy_from = TextInput(multiline=False, input_filter="int", size_hint_x=None, width=dp(55))
        self.copy_to = TextInput(multiline=False, input_filter="int", size_hint_x=None, width=dp(55))
        row.add_widget(Label(text="from", size_hint_x=None, width=dp(36), color=FG))
        row.add_widget(self.copy_from)
        row.add_widget(Label(text="to", size_hint_x=None, width=dp(20), color=FG))
        row.add_widget(self.copy_to)
        copy_btn = Button(text="Copy", size_hint_x=None, width=dp(64),
                           background_color=(0.16, 0.50, 0.73, 1))
        copy_btn.bind(on_release=lambda *_: self._copy_line_range())
        hl_btn = Button(text="Highlight", size_hint_x=None, width=dp(78),
                         background_color=(0.56, 0.27, 0.68, 1))
        hl_btn.bind(on_release=lambda *_: self._highlight_line_range())
        row.add_widget(copy_btn)
        row.add_widget(hl_btn)
        self.copy_status = Label(text="", color=(0.95, 0.6, 0.07, 1))
        row.add_widget(self.copy_status)
        with row.canvas.before:
            Color(0.106, 0.227, 0.294, 1)
            row._bg = Rectangle(pos=row.pos, size=row.size)
        row.bind(pos=lambda i, v: setattr(row._bg, "pos", v),
                  size=lambda i, v: setattr(row._bg, "size", v))
        return row

    def _parse_range(self, from_ti, to_ti, status_lbl):
        total = self.total_lines()
        try:
            a = int(from_ti.text.strip())
            b = int(to_ti.text.strip())
        except ValueError:
            status_lbl.text = "Numbers only"
            return None
        s, e = min(a, b), max(a, b)
        if s < 1 or e > total:
            status_lbl.text = f"Range must be within 1-{total}"
            return None
        if self.large_file_mode:
            window_end = self._current_window_start + WINDOW_LINES
            if s - 1 < self._current_window_start or e > window_end:
                status_lbl.text = (
                    f"In a large file, use Go to Line to bring L{s}-L{e} into view first")
                return None
        return s, e

    def _copy_line_range(self):
        r = self._parse_range(self.copy_from, self.copy_to, self.copy_status)
        if not r:
            return
        s, e = r
        offset = self._current_window_start if self.large_file_mode else 0
        lines = self.text.text.split("\n")
        content = "\n".join(lines[s - 1 - offset:e - offset])
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(content)
        except Exception:
            pass
        self.copy_status.text = f"Copied {e - s + 1} line(s) (L{s}-L{e})"
        self._highlight_line_range(s, e)
        Clock.schedule_once(lambda dt: setattr(self.copy_status, "text", ""), 3)

    def _highlight_line_range(self, s=None, e=None):
        if s is None:
            r = self._parse_range(self.copy_from, self.copy_to, self.copy_status)
            if not r:
                return
            s, e = r
        self.select_lines(s, e)

    def _make_replace_range_strip(self):
        outer = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(120),
                           padding=dp(4), spacing=dp(4))
        outer.natural_height = dp(120)
        top = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
        top.add_widget(Label(text="Replace lines:", size_hint_x=None, width=dp(100), color=FG))
        self.rr_from = TextInput(multiline=False, input_filter="int", size_hint_x=None, width=dp(55))
        self.rr_to = TextInput(multiline=False, input_filter="int", size_hint_x=None, width=dp(55))
        top.add_widget(Label(text="from", size_hint_x=None, width=dp(36), color=FG))
        top.add_widget(self.rr_from)
        top.add_widget(Label(text="to", size_hint_x=None, width=dp(20), color=FG))
        top.add_widget(self.rr_to)
        prev_btn = Button(text="Preview", size_hint_x=None, width=dp(74),
                           background_color=(0.56, 0.27, 0.68, 1))
        prev_btn.bind(on_release=lambda *_: self._preview_replace_range())
        apply_btn = Button(text="Replace", size_hint_x=None, width=dp(74),
                            background_color=(0.15, 0.68, 0.38, 1))
        apply_btn.bind(on_release=lambda *_: self._apply_replace_range())
        top.add_widget(prev_btn)
        top.add_widget(apply_btn)
        outer.add_widget(top)
        self.rr_text = TextInput(hint_text="Replacement text...", size_hint_y=None, height=dp(52),
                                  background_color=(0.16, 0.16, 0.16, 1), foreground_color=FG,
                                  cursor_color=(1, 1, 1, 1))
        outer.add_widget(self.rr_text)
        self.rr_status = Label(text="", size_hint_y=None, height=dp(20), color=(0.95, 0.6, 0.07, 1))
        outer.add_widget(self.rr_status)
        with outer.canvas.before:
            Color(0.18, 0.29, 0.12, 1)
            outer._bg = Rectangle(pos=outer.pos, size=outer.size)
        outer.bind(pos=lambda i, v: setattr(outer._bg, "pos", v),
                   size=lambda i, v: setattr(outer._bg, "size", v))
        return outer

    def _preview_replace_range(self):
        r = self._parse_range(self.rr_from, self.rr_to, self.rr_status)
        if not r:
            return
        s, e = r
        self.select_lines(s, e)
        self.rr_status.text = f"Previewing L{s}-L{e} ({e - s + 1} line(s))"

    def _apply_replace_range(self):
        r = self._parse_range(self.rr_from, self.rr_to, self.rr_status)
        if not r:
            return
        s, e = r
        offset = self._current_window_start if self.large_file_mode else 0
        lines = self.text.text.split("\n")
        repl_lines = self.rr_text.text.split("\n")
        new_lines = lines[:s - 1 - offset] + repl_lines + lines[e - offset:]
        self.set_text_preserve_view("\n".join(new_lines))
        self.rr_status.text = f"Replaced lines {s}-{e}"
        Clock.schedule_once(lambda dt: setattr(self.rr_status, "text", ""), 3)

    def _make_scroll_strip(self):
        row = BoxLayout(size_hint_y=None, height=dp(48), padding=dp(4), spacing=dp(6))
        row.natural_height = dp(48)
        self.scroll_up_btn = ToggleButton(text="Up", group="scrolldir", size_hint_x=None, width=dp(56))
        self.scroll_down_btn = ToggleButton(text="Down", group="scrolldir", state="down",
                                             size_hint_x=None, width=dp(64))
        self.scroll_up_btn.bind(on_release=lambda *_: setattr(self, "scroll_direction", "up"))
        self.scroll_down_btn.bind(on_release=lambda *_: setattr(self, "scroll_direction", "down"))
        row.add_widget(self.scroll_up_btn)
        row.add_widget(self.scroll_down_btn)
        row.add_widget(Label(text="Speed", size_hint_x=None, width=dp(50), color=FG))
        self.scroll_speed_slider = Slider(min=1, max=15, value=3, size_hint_x=1)
        self.scroll_speed_slider.bind(value=lambda i, v: setattr(self, "scroll_speed", v))
        row.add_widget(self.scroll_speed_slider)
        self.scroll_toggle_btn = ToggleButton(text="Start", size_hint_x=None, width=dp(64),
                                               background_color=(0.15, 0.68, 0.38, 1))
        self.scroll_toggle_btn.bind(state=self._on_scroll_toggle)
        row.add_widget(self.scroll_toggle_btn)
        with row.canvas.before:
            Color(0.10, 0.10, 0.10, 1)
            row._bg = Rectangle(pos=row.pos, size=row.size)
        row.bind(pos=lambda i, v: setattr(row._bg, "pos", v),
                  size=lambda i, v: setattr(row._bg, "size", v))
        return row

    def _on_scroll_toggle(self, btn, state):
        if state == "down":
            btn.text = "Stop"
            self._start_scroll()
        else:
            btn.text = "Start"
            self._stop_scroll()

    def _start_scroll(self):
        self.stop_scroll()
        self.scroll_active = True
        self._scroll_ev = Clock.schedule_interval(self._scroll_tick, 1 / 30.0)

    def _stop_scroll(self):
        self.scroll_active = False
        if self._scroll_ev:
            self._scroll_ev.cancel()
            self._scroll_ev = None

    def stop_scroll(self):
        self._stop_scroll()

    def _scroll_tick(self, dt):
        delta = self.scroll_speed * dt * 6.0
        step = -delta if self.scroll_direction == "down" else delta
        new_y = self.text.scroll_y + step / max(self.text.height, 1)
        self.text.scroll_y = max(0.0, min(1.0, new_y))
        self.bm_gutter.redraw()

    def _build_editor_row(self):
        row = BoxLayout(orientation="horizontal")
        self.bm_gutter = BookmarkGutter(self)
        row.add_widget(self.bm_gutter)

        self.line_numbers = Label(text="1", size_hint_x=None, width=dp(34),
                                   color=GUTTER_FG, valign="top", halign="right",
                                   font_size=sp(self.font_size_pt))
        self.line_numbers.bind(texture_size=lambda i, v: None)
        with self.line_numbers.canvas.before:
            Color(*GUTTER_BG)
            self.line_numbers._bg = Rectangle(pos=self.line_numbers.pos, size=self.line_numbers.size)
        self.line_numbers.bind(
            pos=lambda i, v: setattr(self.line_numbers._bg, "pos", v),
            size=lambda i, v: setattr(self.line_numbers._bg, "size", v))
        row.add_widget(self.line_numbers)

        self.text = LatexTextInput(
            pane=self, multiline=True, font_size=sp(self.font_size_pt),
            background_color=self.bg_color, foreground_color=self.fg_color,
            cursor_color=(1, 1, 1, 1), selection_color=SEL_BG,
            padding=[dp(6), dp(6), dp(6), dp(6)],
            do_wrap=self.word_wrap,
        )
        self.text.bind(text=self._on_text_changed)
        self.text.bind(scroll_y=lambda i, v: self._sync_gutters())
        self.text.bind(cursor=lambda i, v: self._schedule_statusbar())
        row.add_widget(self.text)
        self.add_widget(row)

    def _sync_gutters(self):
        self._update_line_numbers_scroll()
        self.bm_gutter.redraw()

    def _update_line_numbers_scroll(self):
        # Approximate: keep line-number label's y-scroll matched by rebuilding
        # text each time (label has no scroll of its own; instead we crop
        # based on the same visible window estimate used in the gutter).
        pass

    def _build_statusbar(self):
        bar = BoxLayout(size_hint_y=None, height=dp(26), padding=(dp(6), 0))
        self.status_label = Label(text="Ln 1, Col 0", color=(0.6, 0.6, 0.6, 1),
                                   halign="left", font_size=sp(11))
        self.status_label.bind(size=lambda i, v: setattr(i, "text_size", v))
        bar.add_widget(self.status_label)
        with bar.canvas.before:
            Color(0.09, 0.09, 0.09, 1)
            bar._bg = Rectangle(pos=bar.pos, size=bar.size)
        bar.bind(pos=lambda i, v: setattr(bar._bg, "pos", v),
                 size=lambda i, v: setattr(bar._bg, "size", v))
        self.add_widget(bar)

    # ── Text change handling ────────────────────────────────────────────
    def _on_text_changed(self, instance, value):
        if self._suspend_modified:
            return
        self.is_modified = True
        if self.large_file_mode:
            self._window_dirty = True
        self.app.refresh_tab_label(self.tab_id)
        self._schedule_linenum_update()
        self._schedule_statusbar()
        self._schedule_session_save()

    def _schedule_linenum_update(self):
        if self._linenum_ev:
            self._linenum_ev.cancel()
        self._linenum_ev = Clock.schedule_once(lambda dt: self._update_line_numbers(), LINENUM_DEBOUNCE_S)

    def _update_line_numbers(self):
        if self.large_file_mode:
            n_lines = self.text.text.count("\n") + 1
            start = self._current_window_start + 1
            self.line_numbers.text = "\n".join(
                str(i) for i in range(start, start + n_lines))
            total = self.total_lines()
            self.line_numbers.width = dp(18) + dp(9) * len(str(total))
            self.bm_gutter.redraw()
            return
        total = self.total_lines()
        self.line_numbers.text = "\n".join(str(i) for i in range(1, total + 1))
        self.line_numbers.width = dp(18) + dp(9) * len(str(total))
        self.bm_gutter.redraw()

    def _schedule_statusbar(self):
        if self._session_ev is None:
            pass
        Clock.schedule_once(lambda dt: self._update_statusbar(), 0)

    def _update_statusbar(self):
        try:
            col, row = self.text.cursor
        except Exception:
            row, col = 0, 0
        total = self.total_lines()
        if self.large_file_mode:
            actual_line = self._current_window_start + row + 1
            self.status_label.text = (
                f"Ln {actual_line}, Col {col}   |   {total} lines (large file)")
        else:
            self.status_label.text = f"Ln {row + 1}, Col {col}   |   {total} lines"

    def _schedule_session_save(self):
        if self._session_ev:
            self._session_ev.cancel()
        self._session_ev = Clock.schedule_once(lambda dt: self.app.schedule_session_save(),
                                                SESSION_SAVE_DEBOUNCE_S)

    # ── Helpers ──────────────────────────────────────────────────────────
    def total_lines(self):
        if self.large_file_mode:
            # Accurate once indexing completes; a safe lower-bound estimate
            # while indexing is still in progress (rare to query then).
            return max(1, self.large_buffer.index.line_count)
        return self.text.text.count("\n") + 1

    def set_text_preserve_view(self, new_text):
        self._suspend_modified = True
        self.text.text = new_text
        self._suspend_modified = False
        self.is_modified = True
        if self.large_file_mode:
            self._window_dirty = True
        self.app.refresh_tab_label(self.tab_id)
        self._update_line_numbers()
        self._schedule_session_save()

    def goto_line(self, line_no, toggle_bookmark=False):
        if self.large_file_mode:
            total = self.total_lines()
            line_no = max(1, min(line_no, total))
            target_idx = line_no - 1  # 0-based
            window_end = self._current_window_start + WINDOW_LINES
            if self._current_window_start <= target_idx < window_end:
                # Already loaded: just move the cursor within this window.
                rel = target_idx - self._current_window_start
                lines = self.text.text.split("\n")
                rel = max(0, min(rel, len(lines) - 1))
                idx = sum(len(l) + 1 for l in lines[:rel])
                self.text.cursor = self.text.get_cursor_from_index(idx)
                self.text.focus = True
                if toggle_bookmark:
                    self.bm_toggle_line(line_no)
                self._update_statusbar()
                return
            # Outside the loaded window: jump the window to centre on the
            # target line, then place the cursor once it's loaded.
            new_start = max(0, target_idx - WINDOW_LINES // 2)
            self._commit_window_edits()

            def _after_shift(dt):
                rel = target_idx - new_start
                lines = self.text.text.split("\n")
                rel = max(0, min(rel, len(lines) - 1))
                idx = sum(len(l) + 1 for l in lines[:rel])
                self.text.cursor = self.text.get_cursor_from_index(idx)
                self.text.focus = True
                if toggle_bookmark:
                    self.bm_toggle_line(line_no)
                self._update_statusbar()

            self._apply_window_shift(new_start)
            Clock.schedule_once(_after_shift, 0)
            return
        lines = self.text.text.split("\n")
        line_no = max(1, min(line_no, len(lines)))
        idx = sum(len(l) + 1 for l in lines[:line_no - 1])
        self.text.cursor = self.text.get_cursor_from_index(idx)
        self.text.focus = True
        if toggle_bookmark:
            self.bm_toggle_line(line_no)
        self._update_statusbar()

    def select_lines(self, s, e):
        offset = self._current_window_start if self.large_file_mode else 0
        lines = self.text.text.split("\n")
        start_idx = sum(len(l) + 1 for l in lines[:s - 1 - offset])
        end_idx = sum(len(l) + 1 for l in lines[:e - offset]) - 1
        end_idx = max(end_idx, start_idx)
        self.text.select_text(start_idx, end_idx)
        self.text.cursor = self.text.get_cursor_from_index(start_idx)
        self.text.focus = True

    def tab_label_text(self):
        name = getattr(self, "_display_name", None) or (
            os.path.basename(self.current_file) if self.current_file else "Untitled"
        )
        return ("* " if self.is_modified else "") + name

    # ── Bookmarks ────────────────────────────────────────────────────────
    def bm_toggle_line(self, line_no):
        existing = next((b for b in self.bookmarks if b["line"] == line_no), None)
        if existing:
            self.bookmarks.remove(existing)
        else:
            if self.large_file_mode:
                rel = line_no - 1 - self._current_window_start
                lines = self.text.text.split("\n")
                preview = lines[rel][:28].strip() if 0 <= rel < len(lines) else ""
            else:
                preview = self.text.text.split("\n")[line_no - 1][:28].strip()
            name = preview if preview else f"Line {line_no}"
            self.bookmarks.append({"line": line_no, "name": name})
        self.bookmarks.sort(key=lambda b: b["line"])
        self.bm_gutter.redraw()
        self._schedule_session_save()
        self.app.refresh_bookmark_panel()

    def bm_toggle_current(self):
        _, row = self.text.cursor
        line_no = row + 1 + (self._current_window_start if self.large_file_mode else 0)
        self.bm_toggle_line(line_no)

    def bm_next(self, forward=True):
        if not self.bookmarks:
            return
        lines = sorted(b["line"] for b in self.bookmarks)
        _, row = self.text.cursor
        cur = row + 1 + (self._current_window_start if self.large_file_mode else 0)
        if forward:
            nxt = next((l for l in lines if l > cur), lines[0])
        else:
            nxt = next((l for l in reversed(lines) if l < cur), lines[-1])
        self.goto_line(nxt)

    def bm_remove(self, line_no):
        self.bookmarks = [b for b in self.bookmarks if b["line"] != line_no]
        self.bm_gutter.redraw()
        self._schedule_session_save()
        self.app.refresh_bookmark_panel()

    def bm_clear_all(self):
        self.bookmarks = []
        self.bm_gutter.redraw()
        self._schedule_session_save()
        self.app.refresh_bookmark_panel()

    # ── Find & Replace (used by the shared dialog) ──────────────────────
    def find_next(self, pattern, forward=True, from_index=None):
        if self.large_file_mode:
            return self._find_next_large(pattern, forward)
        content = self.text.text
        start_idx = self.text.cursor_index() if from_index is None else from_index
        if forward:
            m = pattern.search(content, start_idx)
            if not m:
                m = pattern.search(content, 0)
        else:
            matches = list(pattern.finditer(content, 0, start_idx))
            m = matches[-1] if matches else None
            if not m:
                matches = list(pattern.finditer(content))
                m = matches[-1] if matches else None
        if not m:
            return False
        self.text.select_text(m.start(), m.end())
        self.text.cursor = self.text.get_cursor_from_index(m.end() if forward else m.start())
        return True

    def _find_next_large(self, pattern, forward=True):
        """Search within the loaded window first (instant, common case);
        if not found there, scan the file on disk in bounded chunks so a
        1GB file doesn't need to be loaded into memory just to search it."""
        content = self.text.text
        start_idx = self.text.cursor_index()
        if forward:
            m = pattern.search(content, start_idx)
        else:
            matches = list(pattern.finditer(content, 0, start_idx))
            m = matches[-1] if matches else None
        if m:
            self.text.select_text(m.start(), m.end())
            self.text.cursor = self.text.get_cursor_from_index(m.end() if forward else m.start())
            return True
        # Not in the current window — scan the file itself starting just
        # past (or before) the window, wrapping around if needed.
        found_line = self._scan_file_for_pattern(pattern, forward)
        if found_line is None:
            info_popup("Not found", "No more matches in this file.")
            return False
        self.goto_line(found_line + 1)
        return True

    def _scan_file_for_pattern(self, pattern, forward=True):
        """Streams the file in chunks (with overlap so a match spanning a
        chunk boundary isn't missed) and returns the 0-based line number of
        the first/last match outside the currently loaded window, or None."""
        path = self.large_buffer.path
        window_start_line = self._current_window_start
        window_end_line = window_start_line + WINDOW_LINES
        overlap = 4096
        try:
            size = os.path.getsize(path)
        except Exception:
            return None
        candidates = []
        with open(path, "rb") as f:
            pos = 0
            carry = ""
            carry_line_no = 0
            while pos < size:
                f.seek(pos)
                chunk = f.read(INDEX_CHUNK)
                if not chunk:
                    break
                text = carry + chunk.decode("utf-8", errors="replace")
                base_line = carry_line_no
                for m in pattern.finditer(text):
                    line_no = base_line + text.count("\n", 0, m.start())
                    if line_no < window_start_line or line_no >= window_end_line:
                        candidates.append(line_no)
                # Carry the tail (last `overlap` chars) into the next chunk
                # so matches spanning the boundary aren't lost, adjusting
                # the line-number base by how many newlines we kept.
                if len(text) > overlap:
                    tail = text[-overlap:]
                    carry_line_no = base_line + text.count("\n", 0, len(text) - overlap)
                    carry = tail
                else:
                    carry = text
                    carry_line_no = base_line
                pos += len(chunk)
        if not candidates:
            return None
        candidates.sort()
        if forward:
            after = [c for c in candidates if c > window_start_line]
            return after[0] if after else candidates[0]
        else:
            before = [c for c in candidates if c < window_start_line]
            return before[-1] if before else candidates[-1]

    def replace_all(self, pattern, repl):
        if self.large_file_mode:
            return self._replace_all_large(pattern, repl)
        content = self.text.text
        new_content, n = pattern.subn(repl, content)
        if n:
            self.set_text_preserve_view(new_content)
        return n

    def _replace_all_large(self, pattern, repl):
        """Streams the whole file, applying the substitution chunk by
        chunk, and writes the result to a temp file — never holding the
        full file in memory. Line-window overlap is used so a match
        spanning a chunk boundary is still caught."""
        self._commit_window_edits()
        path = self.large_buffer.path
        tmp_path = path + ".replacetmp"
        overlap = 4096
        total_n = 0
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as src, open(tmp_path, "wb") as out:
                pos = 0
                carry = ""
                while pos < size or carry:
                    chunk = src.read(INDEX_CHUNK) if pos < size else b""
                    pos += len(chunk)
                    text = carry + chunk.decode("utf-8", errors="replace")
                    if pos < size:
                        # Hold back the tail (up to the last newline within
                        # the overlap window) so we don't split a match
                        # that straddles this chunk boundary.
                        split_at = max(0, len(text) - overlap)
                        nl = text.rfind("\n", 0, len(text))
                        safe_split = min(split_at, nl + 1) if nl != -1 else split_at
                        emit, carry = text[:safe_split], text[safe_split:]
                    else:
                        emit, carry = text, ""
                    new_emit, n = pattern.subn(repl, emit)
                    total_n += n
                    out.write(new_emit.encode("utf-8", errors="replace"))
            os.replace(tmp_path, path)
        except Exception as e:
            info_popup("Error", f"Replace failed:\n{e}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return 0
        # File on disk has changed shape — rebuild the index and reload
        # the current window fresh so what's displayed matches the file.
        self.large_buffer.index = LineIndex(path)
        self.large_buffer.dirty_ranges = {}

        def _reindex_step(dt):
            more = self.large_buffer.index.build_step()
            if not more:
                self._apply_window_shift(self._current_window_start)
                return False
            return True
        Clock.schedule_interval(_reindex_step, 0)
        return total_n

    # ── Font / colour ────────────────────────────────────────────────────
    def set_font_size(self, size):
        self.font_size_pt = size
        self.text.font_size = sp(size)
        self.line_numbers.font_size = sp(size)
        self._update_line_numbers()

    def set_fg_color(self, color):
        self.fg_color = color
        self.text.foreground_color = color

    def set_bg_color(self, color):
        self.bg_color = color
        self.text.background_color = color

    def toggle_wordwrap(self):
        self.word_wrap = not self.word_wrap
        self.text.do_wrap = self.word_wrap

    def zoom_in(self):
        self.set_font_size(min(self.font_size_pt + 2, 60))

    def zoom_out(self):
        self.set_font_size(max(self.font_size_pt - 2, 6))

    def zoom_reset(self):
        self.set_font_size(14)

    # ── File I/O ─────────────────────────────────────────────────────────
    def load_file(self, path):
        if isinstance(path, tuple) and path and path[0] == "content_local":
            # Native Android picker already streamed the content:// file to
            # a local cache path (see _android_open_document) — from here
            # it behaves exactly like a normal file path, including the
            # size check below, except we remember the original URI as
            # where "Save" (not "Save As") should write back to.
            _, uri, display_name, local_path = path
            self._load_local_path(local_path, display_name=display_name,
                                   save_target=uri)
            return
        if isinstance(path, tuple) and path and path[0] == "content":
            # Small-file legacy path (kept for anything still producing
            # this shape): content already read fully into memory.
            _, uri, display_name, content = path
            self._enter_small_file_mode(content)
            self.current_file = uri
            self._display_name = display_name
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)
            self._update_line_numbers()
            self._update_statusbar()
            return
        self._load_local_path(path, display_name=os.path.basename(path), save_target=path)

    def _load_local_path(self, path, display_name, save_target):
        try:
            size = os.path.getsize(path)
        except Exception as e:
            info_popup("Error", f"Could not open file:\n{e}")
            return
        if size <= LARGE_FILE_THRESHOLD:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                info_popup("Error", f"Could not open file:\n{e}")
                return
            self._enter_small_file_mode(content)
            self.current_file = save_target
            self._display_name = display_name
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)
            self._update_line_numbers()
            self._update_statusbar()
        else:
            self.current_file = save_target
            self._display_name = display_name
            self._enter_large_file_mode(path)

    def _enter_small_file_mode(self, content):
        self.large_file_mode = False
        self.large_buffer = None
        if self._index_ev:
            self._index_ev.cancel()
            self._index_ev = None
        self._suspend_modified = True
        self.text.text = content
        self._suspend_modified = False

    # ── Large-file (windowed) mode ──────────────────────────────────────
    def _enter_large_file_mode(self, path):
        self.large_file_mode = True
        index = LineIndex(path)
        self.large_buffer = LargeFileBuffer(path, index)
        self._window_dirty = False
        self._current_window_start = 0

        self._loading_popup = info_popup(
            "Opening large file",
            "Indexing a large file for smooth scrolling — this happens once.\n"
            "The app will stay responsive; please wait a moment...",
            size_hint=(0.8, 0.35))
        # Remove the OK button while indexing so the popup reads as
        # progress rather than an error the user might dismiss early.
        try:
            self._loading_popup.content.children[0].disabled = True
            self._loading_popup.content.children[0].opacity = 0.4
        except Exception:
            pass

        def _step(dt):
            more = index.build_step()
            if not more:
                self._index_ev = None
                if self._loading_popup:
                    self._loading_popup.dismiss()
                    self._loading_popup = None
                self._finish_large_file_load()
                return False
            return True

        # Index in small time-boxed steps on the Kivy clock so the UI
        # thread never blocks for long, even though the file is huge.
        self._index_ev = Clock.schedule_interval(_step, 0)

    def _finish_large_file_load(self):
        self._suspend_modified = True
        self.text.text = self.large_buffer.read_window(0)
        self._suspend_modified = False
        self.is_modified = False
        self.app.refresh_tab_label(self.tab_id)
        self._update_line_numbers()
        self._update_statusbar()
        self.text.bind(scroll_y=self._check_window_scroll)

    def _check_window_scroll(self, instance, value):
        """Near the top or bottom of the loaded window, swap in the
        previous/next window. This is what lets scrolling through a
        multi-GB file feel continuous without ever loading it all."""
        if not self.large_file_mode:
            return
        # Kivy TextInput: scroll_y == 1 means scrolled to the very top of
        # the loaded content, 0 means scrolled to the very bottom.
        if value > 0.92:
            self._shift_window(-WINDOW_LINES // 2)
        elif value < 0.08:
            self._shift_window(WINDOW_LINES // 2)

    def _shift_window(self, delta_lines):
        if self._pending_window_start is not None:
            return  # a shift is already in flight
        new_start = max(0, self._current_window_start + delta_lines)
        if new_start == self._current_window_start:
            return
        self._commit_window_edits()
        self._pending_window_start = new_start
        Clock.schedule_once(lambda dt: self._apply_window_shift(new_start), 0)

    def _apply_window_shift(self, new_start):
        text = self.large_buffer.read_window(new_start)
        self._current_window_start = new_start
        self._window_dirty = False
        self._suspend_modified = True
        self.text.text = text
        # Land the cursor/scroll roughly mid-window rather than snapping to
        # an edge, so continued scrolling in the same direction keeps working.
        self.text.scroll_y = 0.5
        self._suspend_modified = False
        self._pending_window_start = None
        self._update_line_numbers()
        self._update_statusbar()

    def _commit_window_edits(self):
        """Record any edits made in the currently-loaded window into the
        buffer's dirty-range overlay before swapping windows or saving."""
        if not self.large_file_mode or not self._window_dirty:
            return
        self.large_buffer.mark_window_dirty(self._current_window_start, self.text.text)
        self._window_dirty = False

    def save_file(self, path=None):
        if self.large_file_mode:
            return self._save_large_file(path)
        return self._save_small_file(path)

    def _save_large_file(self, path=None):
        target = path if path is not None else self.current_file
        if not target:
            return False
        self._commit_window_edits()
        if isinstance(target, tuple):
            info_popup("Error", "Choose a location on this device to save a large "
                                 "file (Save As is not yet supported to a new "
                                 "cloud/content location for large files).")
            return False
        if isinstance(target, str) and target.startswith("content://"):
            info_popup("Error", "This large file was opened from outside the app's "
                                 "storage. Use Save As to save a copy to device "
                                 "storage first.")
            return False
        if not self.large_buffer.has_unsaved_changes():
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)
            return True

        progress_popup = info_popup(
            "Saving...",
            "Writing changes back to the file. This can take a moment for "
            "very large files — please don't close the app.",
            size_hint=(0.8, 0.3))
        try:
            progress_popup.content.children[0].disabled = True
            progress_popup.content.children[0].opacity = 0.4
        except Exception:
            pass

        def _do_save(dt):
            try:
                self.large_buffer.save_to(target)
            except Exception as e:
                progress_popup.dismiss()
                info_popup("Error", f"Could not save file:\n{e}")
                return
            progress_popup.dismiss()
            self.current_file = target
            self._display_name = os.path.basename(target)
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)

        # Give the popup a frame to actually render before the (blocking,
        # but bounded-memory) streaming save runs.
        Clock.schedule_once(_do_save, 0.1)
        return True

    def _save_small_file(self, path=None):
        target = path if path is not None else self.current_file
        if not target:
            return False
        if _is_android() and (
            (isinstance(target, str) and target.startswith("content://"))
            or target is None
        ):
            # Re-saving to a previously opened/created content:// URI:
            # write directly back through ContentResolver, no picker.
            if isinstance(target, str) and target.startswith("content://"):
                try:
                    from jnius import autoclass
                    PythonActivity = autoclass('org.kivy.android.PythonActivity')
                    Uri = autoclass('android.net.Uri')
                    resolver = PythonActivity.mActivity.getContentResolver()
                    uri = Uri.parse(target)
                    stream = resolver.openOutputStream(uri, "wt")
                    OutputStreamWriter = autoclass('java.io.OutputStreamWriter')
                    writer = OutputStreamWriter(stream, "UTF-8")
                    writer.write(self.text.text)
                    writer.flush()
                    writer.close()
                except Exception as e:
                    info_popup("Error", f"Could not save file:\n{e}")
                    return False
                self.is_modified = False
                self.app.refresh_tab_label(self.tab_id)
                return True
        if isinstance(target, tuple) and target and target[0] == "content":
            # Freshly picked save-as target from _android_create_document.
            _, uri, display_name = target
            self.current_file = uri
            self._display_name = display_name
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)
            return True
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self.text.text)
        except Exception as e:
            info_popup("Error", f"Could not save file:\n{e}")
            return False
        self.current_file = target
        self._display_name = os.path.basename(target)
        self.is_modified = False
        self.app.refresh_tab_label(self.tab_id)
        return True

    def to_state(self):
        if self.large_file_mode:
            # Never serialize a huge file's content into the session JSON.
            # On restore, large files re-open from their path (see
            # from_state) and re-index rather than round-tripping content.
            return {
                "current_file": self.current_file,
                "large_file": True,
                "bookmarks": self.bookmarks,
                "font_size": self.font_size_pt,
            }
        return {
            "current_file": self.current_file,
            "text": self.text.text,
            "bookmarks": self.bookmarks,
            "font_size": self.font_size_pt,
        }

    def from_state(self, state):
        self.bookmarks = state.get("bookmarks", []) or []
        self.set_font_size(state.get("font_size", 14))
        if state.get("large_file"):
            path = state.get("current_file", "") or ""
            if path and os.path.exists(path):
                self._display_name = os.path.basename(path)
                self._enter_large_file_mode(path)
            else:
                # Original file is gone (moved/deleted since last session);
                # fail gracefully into an empty tab rather than crashing.
                self._suspend_modified = True
                self.text.text = ""
                self._suspend_modified = False
                self.current_file = ""
                info_popup("File not found",
                           f"Could not restore large file:\n{path}\n"
                           "It may have been moved or deleted.")
            self.is_modified = False
            self.bm_gutter.redraw()
            return
        self._suspend_modified = True
        self.text.text = state.get("text", "")
        self._suspend_modified = False
        self.current_file = state.get("current_file", "") or ""
        self.is_modified = False
        self._update_line_numbers()
        self.bm_gutter.redraw()


def _has_mono():
    return False


# ══════════════════════════════════════════════════════════════════════════
#  Find & Replace popup (shared across tabs, like the Tk single dialog)
# ══════════════════════════════════════════════════════════════════════════

class FindReplaceContent(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(8), **kw)
        self.app = app
        self.match_case = False
        self.use_regex = False
        self.whole_word = False
        self.scope_all_tabs = False

        self.find_entry = TextInput(hint_text="Find...", multiline=False, size_hint_y=None, height=dp(40))
        self.repl_entry = TextInput(hint_text="Replace with...", multiline=False, size_hint_y=None, height=dp(40))
        self.add_widget(Label(text="Find:", size_hint_y=None, height=dp(18), color=FG, halign="left"))
        self.add_widget(self.find_entry)
        self.add_widget(Label(text="Replace:", size_hint_y=None, height=dp(18), color=FG, halign="left"))
        self.add_widget(self.repl_entry)

        opts = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(4))
        self.case_cb = self._labeled_checkbox(opts, "Match case", "match_case")
        self.regex_cb = self._labeled_checkbox(opts, "Regex", "use_regex")
        self.word_cb = self._labeled_checkbox(opts, "Whole word", "whole_word")
        self.add_widget(opts)

        scope_row = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(6))
        scope_row.add_widget(Label(text="Replace All scope:", color=(0.63, 0.78, 1, 1), size_hint_x=None, width=dp(130)))
        self.scope_cur_btn = ToggleButton(text="Current Tab", group="scope", state="down")
        self.scope_all_btn = ToggleButton(text="All Tabs", group="scope")
        self.scope_cur_btn.bind(on_release=lambda *_: setattr(self, "scope_all_tabs", False))
        self.scope_all_btn.bind(on_release=lambda *_: setattr(self, "scope_all_tabs", True))
        scope_row.add_widget(self.scope_cur_btn)
        scope_row.add_widget(self.scope_all_btn)
        self.add_widget(scope_row)

        btn_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        find_next_btn = Button(text="Find Next", background_color=(0.16, 0.50, 0.73, 1))
        find_prev_btn = Button(text="Find Prev", background_color=(0.12, 0.38, 0.55, 1))
        repl_btn = Button(text="Replace", background_color=(0.15, 0.68, 0.38, 1))
        repl_all_btn = Button(text="Replace All", background_color=(0.12, 0.52, 0.29, 1))
        find_next_btn.bind(on_release=lambda *_: self._find(True))
        find_prev_btn.bind(on_release=lambda *_: self._find(False))
        repl_btn.bind(on_release=lambda *_: self._replace_one())
        repl_all_btn.bind(on_release=lambda *_: self._replace_all())
        for b in (find_next_btn, find_prev_btn, repl_btn, repl_all_btn):
            btn_row.add_widget(b)
        self.add_widget(btn_row)

        self.status = Label(text="", size_hint_y=None, height=dp(22), color=(0.95, 0.6, 0.07, 1))
        self.add_widget(self.status)

    def _labeled_checkbox(self, parent, text, attr):
        box = BoxLayout(size_hint_x=None, width=dp(120))
        cb = CheckBox(size_hint_x=None, width=dp(28))
        cb.bind(active=lambda i, v: setattr(self, attr, v))
        box.add_widget(cb)
        box.add_widget(Label(text=text, color=FG, font_size=sp(12)))
        parent.add_widget(box)
        return cb

    def _pattern(self):
        term = self.find_entry.text
        if not term:
            return None
        flags = 0 if self.match_case else re.IGNORECASE
        if self.use_regex:
            pat = term
        else:
            pat = re.escape(term)
        if self.whole_word:
            pat = r'\b' + pat + r'\b'
        return re.compile(pat, flags)

    def _current_pane(self):
        return self.app.current_pane()

    def _find(self, forward):
        pane = self._current_pane()
        if not pane:
            return
        try:
            pat = self._pattern()
        except re.error as e:
            self.status.text = f"Regex error: {e}"
            return
        if pat is None:
            self.status.text = "Enter a search term."
            return
        found = pane.find_next(pat, forward=forward)
        self.status.text = "Found." if found else "Not found."

    def _replace_one(self):
        pane = self._current_pane()
        if not pane:
            return
        if pane.text.selection_text:
            s = pane.text.selection_from
            e = pane.text.selection_to
            s, e = min(s, e), max(s, e)
            content = pane.text.text
            new_content = content[:s] + self.repl_entry.text + content[e:]
            pane.set_text_preserve_view(new_content)
            pane.text.cursor = pane.text.get_cursor_from_index(s + len(self.repl_entry.text))
        self._find(True)

    def _replace_all(self):
        try:
            pat = self._pattern()
        except re.error as e:
            self.status.text = f"Regex error: {e}"
            return
        if pat is None:
            self.status.text = "Enter a search term."
            return
        repl = self.repl_entry.text
        if self.scope_all_tabs:
            def _do():
                total, tabs = 0, 0
                for pane in self.app.all_panes():
                    n = pane.replace_all(pat, repl)
                    if n:
                        total += n
                        tabs += 1
                self.status.text = f"Replaced {total} occurrence(s) across {tabs} tab(s)."
            confirm_popup("Replace All - All Tabs",
                          f"Replace all occurrences across {len(self.app.all_panes())} open tab(s)?",
                          _do)
        else:
            pane = self._current_pane()
            if not pane:
                return
            n = pane.replace_all(pat, repl)
            self.status.text = f"Replaced {n} occurrence(s) (current tab)."


# ══════════════════════════════════════════════════════════════════════════
#  Colour / Font picker popup (mobile substitute for native colour chooser)
# ══════════════════════════════════════════════════════════════════════════

def open_color_picker(title, on_pick):
    grid = GridLayout(cols=4, spacing=dp(6), padding=dp(10), size_hint_y=None)
    grid.bind(minimum_height=grid.setter("height"))
    popup = Popup(title=title, size_hint=(0.85, 0.6), background_color=(0.1, 0.1, 0.1, 1))
    scroller = ScrollView()
    scroller.add_widget(grid)
    popup.content = scroller
    for name, color in PRESET_COLOURS:
        btn = Button(text=name, background_color=color, size_hint_y=None, height=dp(56),
                     color=(0, 0, 0, 1) if sum(color[:3]) > 1.5 else (1, 1, 1, 1))

        def _pick(instance, c=color):
            popup.dismiss()
            on_pick(c)

        btn.bind(on_release=_pick)
        grid.add_widget(btn)
    popup.open()


# ══════════════════════════════════════════════════════════════════════════
#  Bookmark panel (list of bookmarks for the current tab)
# ══════════════════════════════════════════════════════════════════════════

class BookmarkPanel(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", size_hint_x=None, width=dp(220), **kw)
        self.app = app
        with self.canvas.before:
            Color(0.09, 0.09, 0.09, 1)
            self._bg = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=lambda i, v: setattr(self._bg, "pos", v),
                  size=lambda i, v: setattr(self._bg, "size", v))
        header = BoxLayout(size_hint_y=None, height=dp(40), padding=(dp(8), 0))
        header.add_widget(Label(text="Bookmarks", bold=True, color=FG))
        clear_btn = Button(text="Clear All", size_hint_x=None, width=dp(90),
                            background_color=(0.35, 0.2, 0.2, 1))
        clear_btn.bind(on_release=lambda *_: self._clear_all())
        header.add_widget(clear_btn)
        self.add_widget(header)
        self.scroller = ScrollView()
        self.list_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroller.add_widget(self.list_box)
        self.add_widget(self.scroller)

    def _clear_all(self):
        pane = self.app.current_pane()
        if pane:
            pane.bm_clear_all()

    def refresh(self):
        self.list_box.clear_widgets()
        pane = self.app.current_pane()
        if not pane or not pane.bookmarks:
            self.list_box.add_widget(Label(text="No bookmarks", size_hint_y=None, height=dp(36),
                                            color=(0.5, 0.5, 0.5, 1)))
            return
        for b in sorted(pane.bookmarks, key=lambda x: x["line"]):
            row = BoxLayout(size_hint_y=None, height=dp(40), padding=(dp(6), 0), spacing=dp(4))
            lbl = Button(text=f"L{b['line']}: {b['name']}", halign="left", valign="middle",
                         background_normal="", background_color=(0.15, 0.15, 0.15, 1),
                         color=FG)
            lbl.bind(size=lambda i, v: setattr(i, "text_size", (v[0] - dp(10), None)))
            ln = b["line"]
            lbl.bind(on_release=lambda i, l=ln: self.app.current_pane().goto_line(l))
            rm = Button(text="x", size_hint_x=None, width=dp(32),
                        background_color=(0.5, 0.2, 0.2, 1))
            rm.bind(on_release=lambda i, l=ln: self.app.current_pane().bm_remove(l))
            row.add_widget(lbl)
            row.add_widget(rm)
            self.list_box.add_widget(row)


def open_font_size_picker(current, on_pick):
    grid = GridLayout(cols=4, spacing=dp(6), padding=dp(10), size_hint_y=None)
    grid.bind(minimum_height=grid.setter("height"))
    popup = Popup(title="Font Size", size_hint=(0.7, 0.55), background_color=(0.1, 0.1, 0.1, 1))
    scroller = ScrollView()
    scroller.add_widget(grid)
    popup.content = scroller
    for size in FONT_SIZES:
        btn = Button(text=str(size), size_hint_y=None, height=dp(48))
        if size == current:
            btn.background_color = (0.15, 0.68, 0.38, 1)

        def _pick(instance, s=size):
            popup.dismiss()
            on_pick(s)

        btn.bind(on_release=_pick)
        grid.add_widget(btn)
    popup.open()


# ══════════════════════════════════════════════════════════════════════════
#  Tab bar (horizontal scrollable strip of tab buttons + "+" button)
# ══════════════════════════════════════════════════════════════════════════

class TabBar(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None, height=dp(42), **kw)
        self.app = app
        with self.canvas.before:
            Color(0.08, 0.08, 0.08, 1)
            self._bg = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=lambda i, v: setattr(self._bg, "pos", v),
                  size=lambda i, v: setattr(self._bg, "size", v))
        self.scroller = ScrollView(do_scroll_y=False, size_hint_x=1)
        self.tabs_box = BoxLayout(orientation="horizontal", size_hint_x=None, spacing=dp(2))
        self.tabs_box.bind(minimum_width=self.tabs_box.setter("width"))
        self.scroller.add_widget(self.tabs_box)
        self.add_widget(self.scroller)
        add_btn = Button(text="+", size_hint_x=None, width=dp(44),
                          background_color=(0.15, 0.68, 0.38, 1), bold=True)
        add_btn.bind(on_release=lambda *_: self.app.new_tab())
        self.add_widget(add_btn)
        self._buttons = {}

    def add_tab(self, tab_id, label):
        row = BoxLayout(size_hint_x=None, width=dp(150))
        btn = ToggleButton(text=label, group="tabs", size_hint_x=1,
                            background_color=(0.2, 0.2, 0.2, 1), shorten=True,
                            shorten_from="left")
        btn.bind(on_release=lambda *_: self.app.select_tab(tab_id))
        close_btn = Button(text="x", size_hint_x=None, width=dp(30),
                            background_color=(0.3, 0.15, 0.15, 1))
        close_btn.bind(on_release=lambda *_: self.app.close_tab(tab_id))
        row.add_widget(btn)
        row.add_widget(close_btn)
        self.tabs_box.add_widget(row)
        self._buttons[tab_id] = (row, btn)

    def set_label(self, tab_id, label):
        if tab_id in self._buttons:
            self._buttons[tab_id][1].text = label

    def set_active(self, tab_id):
        for tid, (row, btn) in self._buttons.items():
            btn.state = "down" if tid == tab_id else "normal"

    def remove_tab(self, tab_id):
        if tab_id in self._buttons:
            row, btn = self._buttons.pop(tab_id)
            self.tabs_box.remove_widget(row)


# ══════════════════════════════════════════════════════════════════════════
#  Native Android file picker (SAF) — replaces tkinter.filedialog properly.
#  Sidesteps scoped-storage / runtime-permission issues entirely, since the
#  OS grants per-file access itself. Falls back to the Kivy chooser when
#  not running on Android (e.g. desktop testing).
# ══════════════════════════════════════════════════════════════════════════

def _android_open_document(on_choose):
    """Launch ACTION_OPEN_DOCUMENT and copy the picked file to a local cache
    path via ContentResolver, streaming in fixed-size chunks. content://
    URIs give sequential-stream access only (no cheap random seek/size), so
    for large-file windowed mode to work at all we need a real local path
    with known byte offsets — copying once, streaming, is the only way to
    get that without ever holding the whole file in memory."""
    from jnius import autoclass
    from android import activity

    Intent = autoclass('android.content.Intent')
    PythonActivity = autoclass('org.kivy.android.PythonActivity')

    intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
    intent.addCategory(Intent.CATEGORY_OPENABLE)
    intent.setType("*/*")
    mime_types = ["text/plain", "application/x-tex", "text/x-tex"]
    StringArray = autoclass('java.lang.String')
    arr = autoclass('java.lang.reflect.Array').newInstance(StringArray, len(mime_types))
    for i, m in enumerate(mime_types):
        autoclass('java.lang.reflect.Array').set(arr, i, m)
    intent.putExtra(Intent.EXTRA_MIME_TYPES, arr)

    REQUEST_CODE = 9101

    def _on_activity_result(request_code, result_code, data):
        if request_code != REQUEST_CODE:
            return
        activity.unbind(on_activity_result=_on_activity_result)
        RESULT_OK = -1
        if result_code != RESULT_OK or data is None:
            return
        uri = data.getData()
        # Fix: this callback fires directly on Android's main UI thread
        # (it's a Java->Python JNI callback, not something Kivy schedules).
        # The old code did the whole file copy right here, synchronously.
        # For a multi-MB file that's easily long enough to trip Android's
        # ANR ("app not responding") watchdog on the UI thread, which the
        # system handles by killing/blacking out the window — with no
        # Python exception ever raised, so no error popup and no
        # traceback: exactly the silent "screen goes black" symptom.
        # Fix: do the actual copy on a background thread instead, and only
        # touch Kivy/UI state via Clock.schedule_once from that thread.
        import threading

        def _do_copy():
            try:
                display_name = _android_uri_display_name(uri) or "untitled.tex"
                local_path = _unique_cache_path(display_name)
                resolver = PythonActivity.mActivity.getContentResolver()
                java_stream = resolver.openInputStream(uri)
                BufferedInputStream = autoclass('java.io.BufferedInputStream')
                buffered = BufferedInputStream(java_stream, 1 << 16)
                # Wrap the Java InputStream's read(byte[]) in a small Python
                # loop so we copy in bounded chunks, never buffering the
                # whole file — this is what makes even a 1GB pick safe.
                # NOTE: no byte-at-a-time fallback here anymore — that path
                # made millions of individual JNI calls for a multi-MB file
                # and was almost certainly the real cause of the freeze/ANR.
                # pyjnius's read(bytearray) reliably returns the read count
                # and fills the buffer in modern versions; if it fails we
                # surface the real error instead of silently degrading to
                # a pathologically slow per-byte loop.
                buf = bytearray(1 << 20)  # 1 MiB buffer
                with open(local_path, "wb") as out:
                    while True:
                        n = buffered.read(buf)
                        if n is None or n == -1:
                            break
                        out.write(bytes(buf[:n]))
                buffered.close()
            except Exception as e:
                Clock.schedule_once(lambda dt: info_popup("Error", f"Could not open file:\n{e}"))
                return
            Clock.schedule_once(lambda dt: on_choose(("content_local", str(uri), display_name, local_path)))

        threading.Thread(target=_do_copy, daemon=True).start()

    activity.bind(on_activity_result=_on_activity_result)
    PythonActivity.mActivity.startActivityForResult(intent, REQUEST_CODE)


def _unique_cache_path(display_name):
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', display_name) or "picked_file"
    return os.path.join(STORAGE_DIR, f".picked_{int(time.time())}_{safe}")


def _android_create_document(default_name, content, on_saved):
    """Launch ACTION_CREATE_DOCUMENT and write bytes via ContentResolver."""
    from jnius import autoclass
    from android import activity

    Intent = autoclass('android.content.Intent')
    PythonActivity = autoclass('org.kivy.android.PythonActivity')

    intent = Intent(Intent.ACTION_CREATE_DOCUMENT)
    intent.addCategory(Intent.CATEGORY_OPENABLE)
    intent.setType("text/plain")
    intent.putExtra(Intent.EXTRA_TITLE, default_name)

    REQUEST_CODE = 9102

    def _on_activity_result(request_code, result_code, data):
        if request_code != REQUEST_CODE:
            return
        activity.unbind(on_activity_result=_on_activity_result)
        RESULT_OK = -1
        if result_code != RESULT_OK or data is None:
            return
        uri = data.getData()
        try:
            resolver = PythonActivity.mActivity.getContentResolver()
            stream = resolver.openOutputStream(uri)
            OutputStreamWriter = autoclass('java.io.OutputStreamWriter')
            writer = OutputStreamWriter(stream, "UTF-8")
            writer.write(content)
            writer.flush()
            writer.close()
            display_name = _android_uri_display_name(uri) or default_name
        except Exception as e:
            Clock.schedule_once(lambda dt: info_popup("Error", f"Could not save file:\n{e}"))
            return
        Clock.schedule_once(lambda dt: on_saved(("content", str(uri), display_name)))

    activity.bind(on_activity_result=_on_activity_result)
    PythonActivity.mActivity.startActivityForResult(intent, REQUEST_CODE)


def _android_uri_display_name(uri):
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        resolver = PythonActivity.mActivity.getContentResolver()
        OpenableColumns = autoclass('android.provider.OpenableColumns')
        cursor = resolver.query(uri, None, None, None, None)
        if cursor is None:
            return None
        cursor.moveToFirst()
        idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
        name = cursor.getString(idx) if idx >= 0 else None
        cursor.close()
        return name
    except Exception:
        return None


def _is_android():
    try:
        import jnius  # noqa
        from android import activity  # noqa
        return True
    except Exception:
        return False


def open_file_browser(title, on_choose, save_mode=False, default_name="untitled.tex"):
    if _is_android():
        if save_mode:
            _android_create_document(default_name, "", lambda result: on_choose(result))
        else:
            _android_open_document(lambda result: on_choose(result))
        return
    # Desktop fallback (Kivy-native chooser), for testing off-device.
    content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(8))
    start_path = STORAGE_DIR
    chooser = FileChooserListView(path=start_path, filters=["*.tex", "*.txt", "*.latex", "*"])
    content.add_widget(chooser)
    name_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
    name_input = TextInput(text=default_name if save_mode else "", multiline=False)
    if save_mode:
        name_row.add_widget(Label(text="Name:", size_hint_x=None, width=dp(60), color=FG))
        name_row.add_widget(name_input)
        content.add_widget(name_row)
    btn_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
    ok_btn = Button(text="Save" if save_mode else "Open", background_color=(0.15, 0.68, 0.38, 1))
    cancel_btn = Button(text="Cancel")
    btn_row.add_widget(cancel_btn)
    btn_row.add_widget(ok_btn)
    content.add_widget(btn_row)
    popup = Popup(title=title, content=content, size_hint=(0.95, 0.9))

    def _choose_dir(instance, selection):
        if selection:
            name_input.text = os.path.basename(selection[0])

    if save_mode:
        chooser.bind(selection=_choose_dir)

    def _ok(*_):
        if save_mode:
            folder = chooser.path
            fname = name_input.text.strip()
            if not fname:
                return
            path = os.path.join(folder, fname)
        else:
            if not chooser.selection:
                return
            path = chooser.selection[0]
        popup.dismiss()
        on_choose(path)

    ok_btn.bind(on_release=_ok)
    cancel_btn.bind(on_release=popup.dismiss)
    chooser.bind(on_submit=lambda *_: _ok())
    popup.open()


# ══════════════════════════════════════════════════════════════════════════
#  Root widget
# ══════════════════════════════════════════════════════════════════════════

class NotepadRoot(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", **kw)
        self.app = app
        self._build_top_bar()
        body = BoxLayout(orientation="horizontal")
        self.tab_area = BoxLayout(orientation="vertical")
        body.add_widget(self.tab_area)
        self.bookmark_panel = BookmarkPanel(app)
        self.bookmark_panel.width = 0
        self.bookmark_panel.opacity = 0
        body.add_widget(self.bookmark_panel)
        self.add_widget(body)

        self.tab_bar = TabBar(app)
        self.tab_area.add_widget(self.tab_bar)
        self.pane_holder = FloatLayout()
        self.tab_area.add_widget(self.pane_holder)

    def _build_top_bar(self):
        # Two rows instead of one: 9 fixed-width buttons + a title never
        # fit on a phone-width screen in a single row, so several ended up
        # pushed off-screen (looked like "invisible" buttons). Splitting
        # across two rows, with buttons that flex to fill the row width
        # (size_hint_x=1 instead of a fixed dp), keeps every button
        # reachable and consistently tap-sized regardless of screen width.
        row1 = BoxLayout(size_hint_y=None, height=dp(48), padding=(dp(4), 0), spacing=dp(2))
        row2 = BoxLayout(size_hint_y=None, height=dp(48), padding=(dp(4), 0), spacing=dp(2))
        for row in (row1, row2):
            with row.canvas.before:
                Color(0.05, 0.05, 0.05, 1)
                row._bg = Rectangle(pos=row.pos, size=row.size)
            row.bind(pos=lambda i, v: setattr(i._bg, "pos", v),
                     size=lambda i, v: setattr(i._bg, "size", v))

        title = Label(text="LaTeX Notepad", bold=True, size_hint_x=None, width=dp(100),
                      color=FG, font_size=sp(13))
        row1.add_widget(title)

        def mk(row, text, cb):
            b = Button(text=text, size_hint_x=1, background_color=(0.15, 0.15, 0.15, 1))
            b.bind(on_release=cb)
            row.add_widget(b)
            return b

        mk(row1, "Open", lambda *_: self.app.action_open())
        mk(row1, "Save", lambda *_: self.app.action_save())
        mk(row1, "Find", lambda *_: self.app.open_find_replace())
        mk(row1, "Menu", lambda *_: self.app.open_menu())

        mk(row2, "Goto", lambda *_: self.app.toggle_strip("goto_strip"))
        mk(row2, "Range", lambda *_: self.app.toggle_strip("copy_strip"))
        mk(row2, "Replc", lambda *_: self.app.toggle_strip("replace_strip"))
        mk(row2, "Scroll", lambda *_: self.app.toggle_strip("scroll_strip"))
        mk(row2, "Bkmk", lambda *_: self.app.toggle_bookmark_panel())

        self.add_widget(row1)
        self.add_widget(row2)


# ══════════════════════════════════════════════════════════════════════════
#  Main application
# ══════════════════════════════════════════════════════════════════════════

class LatexNotepadApp(App):
    def build(self):
        _request_android_permissions()
        Window.clearcolor = (0.06, 0.06, 0.06, 1)
        self.title = "LaTeX Notepad"
        self.panes = {}          # tab_id -> EditorPane
        self._tab_counter = 0
        self._active_tab_id = None
        self._session_ev = None
        self.find_replace_popup = None
        self.find_replace_content = None

        self.root_widget = NotepadRoot(self)
        self._restore_session()
        if not self.panes:
            self.new_tab()
        Clock.schedule_interval(lambda dt: self._auto_save_loop(), 30)
        return self.root_widget

    # ── Tab management ──────────────────────────────────────────────────
    def new_tab(self, restore_state=None):
        self._tab_counter += 1
        tab_id = f"tab{self._tab_counter}"
        pane = EditorPane(self, tab_id)
        self.panes[tab_id] = pane
        if restore_state:
            pane.from_state(restore_state)
        label = pane.tab_label_text()
        self.root_widget.tab_bar.add_tab(tab_id, label)
        self.select_tab(tab_id)
        return pane

    def select_tab(self, tab_id):
        if tab_id not in self.panes:
            return
        self._active_tab_id = tab_id
        holder = self.root_widget.pane_holder
        holder.clear_widgets()
        pane = self.panes[tab_id]
        pane.size_hint = (1, 1)
        pane.pos_hint = {"x": 0, "y": 0}
        holder.add_widget(pane)
        self.root_widget.tab_bar.set_active(tab_id)
        self.refresh_bookmark_panel()
        if self.find_replace_content:
            fname = getattr(pane, "_display_name", None) or (
                os.path.basename(pane.current_file) if pane.current_file else "Untitled"
            )
            self.find_replace_content.status.text = f"Now targeting: {fname}"

    def close_tab(self, tab_id):
        if tab_id not in self.panes:
            return
        pane = self.panes[tab_id]
        if pane.is_modified:
            def _discard():
                self._do_close_tab(tab_id)
            confirm_popup("Unsaved changes",
                          "This tab has unsaved changes. Close without saving?",
                          _discard)
        else:
            self._do_close_tab(tab_id)

    def _do_close_tab(self, tab_id):
        pane = self.panes.pop(tab_id, None)
        if pane:
            pane.stop_scroll()
        self.root_widget.tab_bar.remove_tab(tab_id)
        if self._active_tab_id == tab_id:
            self._active_tab_id = None
            if self.panes:
                self.select_tab(next(iter(self.panes)))
            else:
                self.new_tab()
        self.schedule_session_save()

    def current_pane(self):
        return self.panes.get(self._active_tab_id)

    def all_panes(self):
        return list(self.panes.values())

    def refresh_tab_label(self, tab_id):
        pane = self.panes.get(tab_id)
        if pane:
            self.root_widget.tab_bar.set_label(tab_id, pane.tab_label_text())

    # ── Toolbar actions ──────────────────────────────────────────────────
    def toggle_strip(self, strip_name):
        pane = self.current_pane()
        if not pane:
            return
        strip = getattr(pane, strip_name)
        pane._toggle_strip(strip)

    def toggle_bookmark_panel(self):
        panel = self.root_widget.bookmark_panel
        if panel.width > 0:
            panel.width = 0
            panel.opacity = 0
        else:
            panel.width = dp(220)
            panel.opacity = 1
            self.refresh_bookmark_panel()

    def refresh_bookmark_panel(self):
        self.root_widget.bookmark_panel.refresh()

    def action_open(self):
        def _chosen(path):
            pane = self.new_tab()
            pane.load_file(path)
        open_file_browser("Open File", _chosen, save_mode=False)

    def action_save(self):
        pane = self.current_pane()
        if not pane:
            return
        if pane.current_file:
            pane.save_file()
        else:
            self.action_save_as()

    def action_save_as(self):
        pane = self.current_pane()
        if not pane:
            return

        def _chosen(path):
            pane.save_file(path)

        default_name = getattr(pane, "_display_name", None) or (
            os.path.basename(pane.current_file) if pane.current_file else "untitled.tex"
        )
        open_file_browser("Save File As", _chosen, save_mode=True,
                           default_name=default_name or "untitled.tex")

    def open_find_replace(self):
        if self.find_replace_popup is None:
            self.find_replace_content = FindReplaceContent(self)
            self.find_replace_popup = Popup(title="Find & Replace",
                                             content=self.find_replace_content,
                                             size_hint=(0.92, 0.85))
        self.find_replace_popup.open()

    def open_menu(self):
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))
        scroller = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        inner.bind(minimum_height=inner.setter("height"))
        scroller.add_widget(inner)
        popup = Popup(title="Menu", content=scroller, size_hint=(0.8, 0.9))

        def row_btn(text, cb):
            b = Button(text=text, size_hint_y=None, height=dp(44))
            b.bind(on_release=lambda *_: (popup.dismiss(), cb()))
            inner.add_widget(b)

        row_btn("New Tab", self.new_tab)
        row_btn("Open File...", self.action_open)
        row_btn("Save", self.action_save)
        row_btn("Save As...", self.action_save_as)
        row_btn("Close Tab", lambda: self.close_tab(self._active_tab_id))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("Toggle Bookmark on Line", lambda: self._delegate("bm_toggle_current"))
        row_btn("Next Bookmark", lambda: self._delegate("bm_next", True))
        row_btn("Previous Bookmark", lambda: self._delegate("bm_next", False))
        row_btn("Clear Bookmarks in Tab", lambda: self._delegate("bm_clear_all"))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("Toggle Word Wrap", lambda: self._delegate("toggle_wordwrap"))
        row_btn("Toggle Syntax Highlight", self._toggle_highlight)
        row_btn("Font Size...", self._pick_font_size)
        row_btn("Text Colour...", self._pick_fg_color)
        row_btn("Background Colour...", self._pick_bg_color)
        row_btn("Zoom In", lambda: self._delegate("zoom_in"))
        row_btn("Zoom Out", lambda: self._delegate("zoom_out"))
        row_btn("Reset Zoom", lambda: self._delegate("zoom_reset"))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("About", self._show_about)
        popup.open()

    def _delegate(self, method, *args):
        pane = self.current_pane()
        if pane and hasattr(pane, method):
            getattr(pane, method)(*args)

    def _toggle_highlight(self):
        pane = self.current_pane()
        if pane:
            pane.highlight_enabled = not pane.highlight_enabled
            pane.text._trigger_update_graphics()

    def _pick_font_size(self):
        pane = self.current_pane()
        if not pane:
            return
        open_font_size_picker(pane.font_size_pt, lambda s: pane.set_font_size(s))

    def _pick_fg_color(self):
        pane = self.current_pane()
        if not pane:
            return
        open_color_picker("Text Colour", lambda c: pane.set_fg_color(c))

    def _pick_bg_color(self):
        pane = self.current_pane()
        if not pane:
            return
        open_color_picker("Background Colour", lambda c: pane.set_bg_color(c))

    def _show_about(self):
        info_popup(
            "LaTeX Notepad",
            "LaTeX Notepad - Android edition\n\n"
            "Ported from a desktop Tkinter app to Kivy for Android.\n\n"
            "Features:\n"
            "- Multi-tab plain text / LaTeX editing\n"
            "- Syntax highlighting (commands, comments, image paths)\n"
            "- Find & Replace, with Replace All across all tabs\n"
            "- Bookmarks with a jump panel\n"
            "- Go to Line\n"
            "- Copy / Replace line ranges\n"
            "- Auto-scroll with adjustable speed\n"
            "- Font size and colour presets\n"
            "- Session auto-save and restore\n\n"
            "(The LaTeX minipage code-placer tool from the desktop version\n"
            "has been removed in this edition.)"
        )

    # ── Session persistence ──────────────────────────────────────────────
    def schedule_session_save(self):
        if self._session_ev:
            self._session_ev.cancel()
        self._session_ev = Clock.schedule_once(lambda dt: self._save_session(), 0.5)

    def _save_session(self):
        data = {
            "tabs": [pane.to_state() for pane in self.panes.values()],
            "active": self._active_tab_id,
        }
        save_session(data)

    def _restore_session(self):
        data = load_session()
        if not data:
            return
        for state in data.get("tabs", []):
            self.new_tab(restore_state=state)

    def _auto_save_loop(self):
        self._save_session()

    def on_stop(self):
        for pane in self.panes.values():
            pane.stop_scroll()
        self._save_session()


if __name__ == "__main__":
    LatexNotepadApp().run()
