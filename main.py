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
import bisect
import codecs
import shutil
import threading
from urllib.parse import unquote
from array import array
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

# ─── Universal font (fixes "▯" boxes for emoji / Hindi / Arabic / math) ─────
# Kivy's default font (Roboto) only has Latin/Greek/Cyrillic, so any other
# character is drawn as a missing-glyph box. UniversalSans.ttf is a merged
# font (DejaVu Sans + FreeSans + Unifont emoji: ~10,600 characters). Setting
# it as Kivy's DEFAULT_FONT makes every Label/Button/TextInput/Popup use it.
from kivy.core.text import LabelBase, DEFAULT_FONT

UNIVERSAL_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "UniversalSans.ttf")
FONT_NAME = DEFAULT_FONT
try:
    if os.path.exists(UNIVERSAL_FONT):
        LabelBase.register(name="UniversalSans", fn_regular=UNIVERSAL_FONT,
                           fn_bold=UNIVERSAL_FONT, fn_italic=UNIVERSAL_FONT,
                           fn_bolditalic=UNIVERSAL_FONT)
        LabelBase.register(name=DEFAULT_FONT, fn_regular=UNIVERSAL_FONT,
                           fn_bold=UNIVERSAL_FONT, fn_italic=UNIVERSAL_FONT,
                           fn_bolditalic=UNIVERSAL_FONT)
        FONT_NAME = "UniversalSans"
except Exception as _font_err:  # never crash the app over a font
    print("Font registration failed, using default font:", _font_err)

# ─── Encoding detection (fixes "�" boxes from non-UTF-8 files) ──────────────
def detect_encoding(path, sample_bytes=262144):
    """Best-effort text encoding for `path`. Files that aren't UTF-8 (Windows
    Notepad's ANSI/cp1252, UTF-16, legacy Indic encodings...) used to be
    force-decoded as UTF-8, turning every unreadable byte into a U+FFFD box
    that no font can fix. Returns a Python codec name."""
    try:
        with open(path, "rb") as f:
            head = f.read(sample_bytes)
    except Exception:
        return "utf-8"
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if not head:
        return "utf-8"
    # BOM-less UTF-16: text is full of NUL bytes in a regular even/odd pattern.
    if b"\x00" in head[:4096]:
        chunk = head[:4096]
        even_nul = chunk[0::2].count(b"\x00")
        odd_nul = chunk[1::2].count(b"\x00")
        half = max(1, len(chunk) // 2)
        if odd_nul > half * 0.3 and even_nul < half * 0.05:
            return "utf-16-le"
        if even_nul > half * 0.3 and odd_nul < half * 0.05:
            return "utf-16-be"
    # Plain UTF-8 is by far the most common; accept it if it decodes cleanly.
    # (A read cut mid-character at the sample edge is tolerated.)
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as e:
        if e.start >= len(head) - 4:
            return "utf-8"
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(head).best()
        if best is not None and best.encoding:
            enc = best.encoding.lower().replace("_", "-")
            # Single-byte Latin guesses are a coin-flip between cp1250/
            # cp1252/latin-1/etc. on short text; cp1252 (Windows "ANSI") is
            # what real-world Western files almost always are.
            if enc in ("cp1250", "cp1252", "cp1254", "cp1257", "iso8859-1",
                       "iso8859-2", "iso8859-15", "latin-1", "latin1",
                       "mac-roman", "mac-latin2", "cp850", "cp437"):
                return "cp1252"
            return best.encoding
    except Exception:
        pass
    return "cp1252"


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


# Android's system clipboard travels over Binder (~1 MB limit shared by the
# whole process, text is UTF-16 on the wire). Bigger strings fail silently, so
# anything above this goes to the in-app clipboard instead.
CLIPBOARD_SAFE_CHARS = 200000


def clean_display_name(name):
    """Strip the internal cache prefix ('.picked_<ts>_') so tabs always show
    the ORIGINAL file name, never the local cache copy's name."""
    if not name:
        return name
    return re.sub(r"^\.picked_\d+_", "", name)


def name_from_uri(uri):
    try:
        t = unquote(uri)
        t = t.rstrip("/").split("/")[-1]
        return t.split(":")[-1] or "file"
    except Exception:
        return "file"


def copy_to_system_clipboard(text):
    try:
        from kivy.core.clipboard import Clipboard
        Clipboard.copy(text)
        return True
    except Exception:
        return False


def merge_ranges(pairs, total):
    """[(a,b),...] -> sorted, clamped to 1..total, overlapping/adjacent merged."""
    rs = []
    for a, b in pairs:
        s, e = min(a, b), max(a, b)
        if e < 1 or s > total:
            continue
        rs.append((max(1, s), min(e, total)))
    rs.sort()
    merged = []
    for s, e in rs:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _uri_out_stream(uri_str, mode):
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Uri = autoclass('android.net.Uri')
    resolver = PythonActivity.mActivity.getContentResolver()
    return resolver.openOutputStream(Uri.parse(uri_str), mode)


def write_text_to_uri(uri_str, text, mode="wt"):
    """Returns None on success, else an error string."""
    try:
        from jnius import autoclass
        stream = _uri_out_stream(uri_str, mode)
        OutputStreamWriter = autoclass('java.io.OutputStreamWriter')
        writer = OutputStreamWriter(stream, "UTF-8")
        writer.write(text)
        writer.flush()
        writer.close()
        return None
    except Exception as e:
        return str(e)


def copy_file_to_uri(local_path, uri_str):
    """Stream a local file into a content:// URI (bounded memory)."""
    try:
        stream = _uri_out_stream(uri_str, "wt")
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                stream.write(bytearray(chunk), 0, len(chunk))
        stream.flush()
        stream.close()
        return None
    except Exception as e:
        return str(e)


def append_text_to_target(target, text):
    """target = filesystem path or content:// URI. Returns error string or None."""
    try:
        if isinstance(target, str) and target.startswith("content://"):
            return write_text_to_uri(target, text, mode="wa")
        with open(target, "a", encoding="utf-8") as f:
            f.write(text)
        return None
    except Exception as e:
        return str(e)


# ══════════════════════════════════════════════════════════════════════════
#  Shared collapse/expand helper for any hide-on-demand panel or strip
# ══════════════════════════════════════════════════════════════════════════
#
# Root-cause note (see the Replace-strip and bookmark-panel bugs this
# fixes): a container's width/height shrinking to 0 does NOT shrink any
# descendant that has its own fixed size (size_hint_x/y=None with a literal
# dp() value). Kivy still lays those descendants out at full size,
# positioned relative to the container's own edge — which, once that edge
# has moved to a 0-size position, can land the descendant's real screen
# rectangle anywhere, including on top of unrelated real UI (the toolbar
# row, in both bugs this fixed). Kivy's base Widget.on_touch_down doesn't
# check its own collide_point before recursing into children, so any touch
# landing on one of these still-full-size, still-enabled leftover widgets
# is silently swallowed before it ever reaches the intended target —
# exactly the "button doesn't respond" / "app looks frozen" symptom,
# because the tap IS being handled, just by an invisible dead widget no one
# can see.
#
# Fix: walk every descendant of the panel/strip and force ITS size to zero
# too (remembering the original values), not just the outer container's.
def collapse_widget(widget):
    if not hasattr(widget, "_saved_sizes"):
        widget._saved_sizes = {}

    def collapse(w):
        if w not in widget._saved_sizes:
            widget._saved_sizes[w] = (
                w.size_hint_y, w.height, w.size_hint_x, w.width,
            )
        w.size_hint_y = None
        w.height = 0
        w.size_hint_x = None
        w.width = 0
        w.disabled = True
        w.opacity = 0
        for c in w.children:
            collapse(c)

    collapse(widget)


def expand_widget(widget, restore_outer_size=True):
    widget.disabled = False
    widget.opacity = 1
    saved = getattr(widget, "_saved_sizes", {})
    for w, (shy, h, shx, wd) in saved.items():
        if w is widget and not restore_outer_size:
            # Caller sets the outer container's own target size explicitly
            # (e.g. a fixed panel width, or a strip's natural_height) —
            # don't clobber it with whatever it happened to be at
            # construction time.
            w.disabled = False
            w.opacity = 1
            continue
        w.size_hint_y = shy
        if shy is None:
            w.height = h
        w.size_hint_x = shx
        if shx is None:
            w.width = wd
        w.disabled = False
        w.opacity = 1


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
#  Tappable status-bar label (copy current line number on tap)
# ══════════════════════════════════════════════════════════════════════════

class _TappableLabel(ButtonBehavior, Label):
    """A Label that behaves like a button. Used for the bottom status bar
    (e.g. "Ln 460, Col 0 | 136921 lines") so tapping it can copy the
    current line number to the clipboard, without changing how it looks
    (ButtonBehavior adds no visuals of its own)."""
    pass


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
        # Narrowed from dp(18): it only ever holds a dp(10) dot, the extra
        # width was dead space next to the line numbers (see below).
        self.width = dp(14)
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
        pane = self.pane
        ti = pane.text
        sv = pane.text_scroll
        bm_lines = {b["line"] for b in pane.bookmarks}
        if not bm_lines:
            return
        window_offset = pane._current_window_start if pane.large_file_mode else 0
        # Determine the visible line range from the ScrollView's own
        # scroll_y (0 = scrolled to bottom, 1 = scrolled to top, matching
        # Kivy's convention) against the TextInput's full content height —
        # this replaces the old approach of reading the TextInput's own
        # scroll_y, which no longer reflects anything once the TextInput
        # is sized to its full content and scrolled via an outer
        # ScrollView instead of internally.
        try:
            total_lines = len(ti._lines) if ti._lines else 1
            line_h = ti.line_height + ti.line_spacing
            content_h = max(ti.height, sv.height)
            hidden = max(0, content_h - sv.height)
            scrolled_from_top = (1 - min(max(sv.scroll_y, 0), 1)) * hidden
            first_visible = int(scrolled_from_top / line_h) if line_h else 0
            visible_lines = max(1, int(sv.height / line_h) + 2)
            first_visible = max(0, min(first_visible, max(0, total_lines - 1)))
        except Exception:
            return
        # Anchor drawing to the gutter's own on-screen position, which the
        # BoxLayout keeps aligned with the ScrollView's viewport (both are
        # plain rows in the same parent, so their tops line up).
        top = self.top
        starts = pane.line_start_rows()
        with self.canvas.after:
            for ln in bm_lines:
                idx0 = ln - 1 - window_offset  # window-relative logical index
                if starts is not None:
                    if idx0 < 0 or idx0 >= len(starts):
                        continue
                    idx0 = starts[idx0]        # -> visual row (wrap on)
                if idx0 < first_visible - 2 or idx0 > first_visible + visible_lines + 2:
                    continue
                rel = idx0 - first_visible
                y = top - (rel + 0.5) * line_h
                if y < self.y - line_h or y > top + line_h:
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


class LineNumberGutter(Widget):
    """Draws line numbers next to the text area.

    Root-cause fix: the previous implementation built ONE Label containing
    every line number in the loaded window (up to WINDOW_LINES = 2000 lines)
    joined with "\\n", sized to the label's full texture height, and lived in
    a ScrollView. Two things fell out of that:

      1. A single Kivy Label is a single GPU texture. A window's worth of
         numbers at normal font size is tens of thousands of pixels tall —
         far past what real devices allow for one texture (commonly capped
         around 4096-8192px). Past that limit the texture comes back blank
         or garbled, which is exactly the "gutter is wide but no numbers are
         visible" bug in the screenshot — and it kicks in for ordinary files
         too, not just huge ones, since only a few hundred lines is already
         enough to blow the limit.
      2. That same giant label+texture was rebuilt from scratch on *every*
         scroll_y/scroll_x change (see EditorPane._sync_gutters), including
         every pixel of a drag and every tick of auto-scroll (30x/sec). Full
         texture rebuilds at that rate pin the UI thread, which is what made
         scrolling crawl and made the toolbar buttons feel dead — touches
         were arriving, they just weren't getting processed because the main
         loop was busy re-rendering thousands of lines of numbers no one
         had scrolled past yet.

    Fix: never render more numbers than are actually on screen. This mirrors
    BookmarkGutter above — a small recycled pool of tiny Label widgets, one
    per visible text row, repositioned each redraw. Redraw is now cheap
    enough to call on every scroll frame.
    """

    MAX_POOL = 160  # generous headroom for tall/small-font screens

    def __init__(self, pane, **kw):
        super().__init__(**kw)
        self.pane = pane
        self.size_hint_x = None
        self.width = dp(40)
        with self.canvas.before:
            self._bg_color = Color(*GUTTER_BG)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._update_bg, size=self._update_bg)
        self._labels = []
        for _ in range(self.MAX_POOL):
            lbl = Label(text="", color=GUTTER_FG, halign="left", valign="middle",
                        size_hint=(None, None), opacity=0)
            self.add_widget(lbl)
            self._labels.append(lbl)

    def _update_bg(self, *_):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size

    def update_width(self, total_lines):
        """Only needs to change when the digit count of the highest line
        number changes (e.g. 999 -> 1000) — not on every scroll/keystroke."""
        # Size from the ACTUAL rendered width of the widest possible number
        # at the current font size, not a fixed dp-per-digit guess. A fixed
        # guess is too wide at small fonts (the "gutter is too broad"
        # complaint) and clips the numbers at large fonts. Digits "0" are
        # the widest glyphs in most fonts, so measure a string of them.
        digits = len(str(max(total_lines, 1)))
        self._last_total = total_lines
        try:
            from kivy.core.text import Label as CoreLabel
            fs = self.pane.text.font_size
            lab = CoreLabel(text="0" * digits, font_size=fs)
            lab.refresh()
            text_w = lab.texture.width if lab.texture else dp(9) * digits
        except Exception:
            text_w = dp(9) * digits
        # +7dp = the 3dp left inset + 4dp row margin used in redraw(), so
        # the number sits fully inside with a hair of breathing room.
        new_w = text_w + dp(10)
        if abs(new_w - self.width) > 0.5:
            self.width = new_w

    def redraw(self):
        pane = self.pane
        ti = pane.text
        sv = pane.text_scroll
        window_offset = pane._current_window_start if pane.large_file_mode else 0
        try:
            total_in_window = len(ti._lines) if ti._lines else 1
            line_h = ti.line_height + ti.line_spacing
            content_h = max(ti.height, sv.height)
            hidden = max(0, content_h - sv.height)
            scrolled_from_top = (1 - min(max(sv.scroll_y, 0), 1)) * hidden
            first_visible = int(scrolled_from_top / line_h) if line_h else 0
            first_visible = max(0, min(first_visible, max(0, total_in_window - 1)))
            visible_count = max(1, int(sv.height / line_h) + 2)
        except Exception:
            return
        top = self.top
        row_w = self.width - dp(4)
        used = 0
        wrapped = pane._wrapped()
        flags = ti._lines_flags if wrapped else None
        cur_logical = pane.logical_at_row(first_visible) if wrapped else 0
        for i in range(min(visible_count, self.MAX_POOL)):
            idx = first_visible + i
            if idx >= total_in_window:
                break
            lbl = self._labels[used]
            if wrapped:
                is_start = idx == 0 or (idx < len(flags) and flags[idx] & 1)
                if i > 0 and is_start:
                    cur_logical += 1
                lbl.text = str(cur_logical + 1 + window_offset) if is_start else ""
            else:
                lbl.text = str(idx + 1 + window_offset)
            lbl.font_size = ti.font_size
            lbl.size = (row_w, line_h)
            lbl.text_size = (row_w, line_h)
            lbl.pos = (self.x + dp(3), top - (i + 1) * line_h)
            lbl.opacity = 1
            used += 1
        for j in range(used, self.MAX_POOL):
            self._labels[j].opacity = 0
            self._labels[j].text = ""


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
        # NOTE: Kivy's TextInput renders line labels WITHOUT markup, so
        # injecting [color=...] / &bl; here made those literal characters
        # show up in lines containing backslash-commands, %, [, ] or &.
        # Plain rendering keeps the text exactly as typed.
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


# ══════════════════════════════════════════════════════════════════════════
#  Time-boxed background job runner (keeps the UI alive during huge scans)
# ══════════════════════════════════════════════════════════════════════════
class StepJob:
    """Drives a generator a few milliseconds per frame on the Kivy clock.

    Why: Find / Replace All over a multi-hundred-MB file (10,000,000+ lines)
    takes seconds to minutes. Doing it in one blocking call freezes the UI
    thread, and on Android a frozen UI thread means the system shows
    "app not responding" and can kill the app. Instead the work is written
    as a generator that yields a progress fraction (0..1) after each small
    unit of work; this runner gives it ~20 ms per frame (the screen keeps
    redrawing and taps keep working), and shows a progress popup with a
    Cancel button if the job runs longer than a fraction of a second.

    The generator's `return value` is passed to on_done(result, error).
    Cancelling closes the generator (running its `finally` cleanup).
    """

    # Kivy's Clock holds only a WEAK reference to a bound method, so a job
    # nobody else references gets garbage-collected and silently never runs.
    # Keep every live job in this set until it finishes or is cancelled.
    _active = set()

    def __init__(self, title, gen, on_done, budget=0.02, show_after=0.3):
        StepJob._active.add(self)
        self.title = title
        self.gen = gen
        self.on_done = on_done
        self.budget = budget
        self.show_after = show_after
        self.t0 = time.perf_counter()
        self.progress = 0.0
        self.popup = None
        self.label = None
        self.finished = False
        self.ev = Clock.schedule_interval(self._tick, 0)

    def _open_popup(self):
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        self.label = Label(text="Working... 0%", color=FG)
        content.add_widget(self.label)
        btn = Button(text="Cancel", size_hint_y=None, height=dp(44))
        btn.bind(on_release=lambda *_: self.cancel())
        content.add_widget(btn)
        self.popup = Popup(title=self.title, content=content, size_hint=(0.8, 0.3),
                           auto_dismiss=False, background_color=(0.1, 0.1, 0.1, 1))
        self.popup.open()

    def _close_popup(self):
        if self.popup is not None:
            try:
                self.popup.dismiss()
            except Exception:
                pass
            self.popup = None

    def _tick(self, dt):
        if self.finished:
            return False
        end = time.perf_counter() + self.budget
        try:
            while time.perf_counter() < end:
                self.progress = next(self.gen)
        except StopIteration as stop:
            self._finish(stop.value, None)
            return False
        except Exception as exc:  # never let a job error jam the UI
            self._finish(None, exc)
            return False
        if self.popup is None and time.perf_counter() - self.t0 > self.show_after:
            self._open_popup()
        if self.label is not None:
            try:
                self.label.text = f"Working... {int(min(max(self.progress, 0), 1) * 100)}%"
            except Exception:
                pass
        return True

    def _finish(self, result, error):
        self.finished = True
        StepJob._active.discard(self)
        self._close_popup()
        try:
            self.on_done(result, error)
        except Exception:
            import traceback
            traceback.print_exc()

    def cancel(self):
        if self.finished:
            return
        self.finished = True
        StepJob._active.discard(self)
        try:
            self.ev.cancel()
        except Exception:
            pass
        try:
            self.gen.close()      # runs the generator's finally: cleanup
        except Exception:
            pass
        self._close_popup()
        try:
            self.on_done(None, "cancelled")
        except Exception:
            import traceback
            traceback.print_exc()



_NL_RE = re.compile(b"\n")


class LineIndex:
    """Maps line number -> byte offset for a file, built incrementally off
    the UI thread so indexing a huge file doesn't freeze the app."""

    def __init__(self, path):
        self.path = path
        # offsets[i] = byte offset where line i (0-based) starts.
        # array('Q') stores each offset as a raw unsigned 64-bit int (8
        # bytes) instead of a Python list of int objects (~40 bytes each).
        # For 10,000,000 lines that is ~80 MB instead of ~400 MB, which is
        # the difference between working and Android killing the app for
        # using too much memory. 'Q' also has no 4 GB file-size ceiling.
        self.offsets = array("Q", [0])
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
        base = self._build_pos + 1
        # One regex pass in C finds every newline in the chunk; far faster
        # than a Python-level find() loop, which matters at 10M+ lines.
        self.offsets.extend([base + m.start() for m in _NL_RE.finditer(chunk)])
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
        # Where a *large* file actually lives on local disk for re-indexing
        # on restore. Kept separate from current_file because current_file
        # may be a content:// URI (the original picked location, used for
        # "Save"), while large-file mode always reads from a local cache
        # copy of it (see _android_open_document) — the two are NOT
        # interchangeable, and os.path.exists() on a content:// URI is
        # always False. See from_state()/to_state() for why this matters.
        self._large_local_path = None
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
        self.scroll_speed = 3.0   # lines per second (user-typed in the Scroll strip)
        self._scroll_ev = None
        self._highlight_ev = None
        self._linenum_ev = None
        self._session_ev = None
        self._suspend_modified = False

        # Large-file (windowed) mode state
        self.large_file_mode = False
        self.large_buffer = None       # LargeFileBuffer, set when large_file_mode is True
        self._index_ev = None          # Clock event driving incremental indexing
        self._search_job = None        # running big-file Find scan (StepJob), if any
        self._loading_popup = None
        self._pending_window_start = None
        self._current_window_start = 0
        self._window_dirty = False     # has the currently-loaded window been edited?
        self._pending_view = None      # {'top','row','col'} waiting to be applied
        self._last_view = None
        self._scroll_lock = False      # True while WE scroll programmatically
        self._user_moved = False
        self._restore_try = 0
        self._large_needs_writeback = False   # cache file changed, original not yet updated

        # Coalesces gutter redraws: a diagonal/momentum scroll fires both
        # scroll_x and scroll_y in the same frame, which used to mean two
        # full redraws for one visual update. A Kivy trigger collapses any
        # number of calls within a frame down to a single redraw next frame.
        self._gutter_trigger = Clock.create_trigger(self._do_sync_gutters, 0)


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
            self.strips.add_widget(s)
            # Start fully collapsed (see _collapse_strip): must zero every
            # fixed-height descendant too, not just the strip's own outer
            # container, or a strip that has never even been opened yet
            # still sits fully sized at its default layout position and
            # can intercept taps meant for the real toolbar above/below it.
            self._collapse_strip(s)

    def _collapse_strip(self, strip):
        """Fully collapse a strip so it can neither be seen nor tapped.

        Root-cause fix (this is the actual cause of the dead toolbar
        buttons): zeroing a *container's* height does not shrink any
        descendant that has its own fixed height (size_hint_y=None, a
        literal height in dp). Kivy still lays those descendants out at
        their normal size, positioned relative to the container's own
        position — which, once the container's height is 0, can be
        anywhere, including on top of the real toolbar row above/below it.
        The Replace strip's inner row ("Replace lines: ... [Preview]
        [Replace]") is built exactly this way, with a fixed-height inner
        BoxLayout — so even though the strip LOOKS collapsed (0 height,
        opacity 0 on the outer container), its buttons stayed fully sized
        and fully interactive at their last layout position, which happens
        to sit on top of the main "Goto / Range / Replc / Scroll / Bkmk /
        Wrap" row. Kivy's base Widget.on_touch_down doesn't check
        collide_point on itself before recursing into children, so any
        touch landing there is silently swallowed by these invisible
        leftover buttons before it ever reaches the real toolbar — which
        is exactly the "buttons don't work" symptom, and it only shows up
        once the app has been laid out (they aren't there from frame zero,
        they're the true resting position of an always-collapsed strip).

        Fix: walk every descendant and force size to zero too (not just
        the outer container's), remembering original values so
        _expand_strip can restore them. A widget with height 0 cannot
        collide with a touch, wherever it happens to sit.
        """
        if not hasattr(strip, "_saved_sizes"):
            strip._saved_sizes = {}

        def collapse(w):
            if w not in strip._saved_sizes:
                strip._saved_sizes[w] = (
                    w.size_hint_y, w.height, w.size_hint_x, w.width,
                )
            w.size_hint_y = None
            w.height = 0
            w.disabled = True
            w.opacity = 0
            for c in w.children:
                collapse(c)

        collapse(strip)
        strip.height = 0
        strip.opacity = 0
        strip.disabled = True

    def _expand_strip(self, strip):
        strip.disabled = False
        strip.opacity = 1
        saved = getattr(strip, "_saved_sizes", {})
        for w, (shy, h, shx, wd) in saved.items():
            w.size_hint_y = shy
            if shy is None:
                w.height = h
            w.size_hint_x = shx
            if shx is None:
                w.width = wd
            w.disabled = False
            w.opacity = 1
        strip.height = strip.natural_height

    def _toggle_strip(self, strip):
        # Kivy note: a BoxLayout only relayouts children when their size
        # *properties* change; toggling opacity/disabled alone doesn't
        # trigger that. Changing height, opacity and disabled together
        # fixes the strip's OWN internal layout (self.strips.do_layout()
        # below) — but self.strips is only one BoxLayout deep. Its own
        # height change then has to ripple UP into this EditorPane (self),
        # which is what actually decides how much vertical room strips vs.
        # the editor_row/text area get. That second relayout was left to
        # happen "automatically" on the next frame, which is exactly the
        # gap that made a freshly-opened strip's buttons sit one frame
        # behind where they're drawn — the strip is painted, but the row
        # underneath it hasn't shrunk out of the way yet, so the first tap
        # can land on the wrong widget. Force both levels synchronously.
        showing = strip.height > 0
        # Collapse any other strip first so only one is open at a time —
        # several stacked open strips were pushing each other around and
        # making it easy to tap into the wrong (still-animating) one.
        for other in (self.goto_strip, self.copy_strip, self.replace_strip, self.scroll_strip):
            if other is not strip and other.height > 0:
                self._collapse_strip(other)
        if showing:
            self._collapse_strip(strip)
        else:
            self._expand_strip(strip)
        self.strips.do_layout()
        self.do_layout()

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
        outer = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(92),
                           padding=dp(4), spacing=dp(4))
        outer.natural_height = dp(92)
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(4))
        row.add_widget(Label(text="Lines", size_hint_x=None, width=dp(42), color=FG))
        self.copy_from = TextInput(multiline=False, input_filter="int", size_hint_x=None, width=dp(72))
        self.copy_to = TextInput(multiline=False, input_filter="int", size_hint_x=None, width=dp(72))
        row.add_widget(self.copy_from)
        row.add_widget(Label(text="to", size_hint_x=None, width=dp(20), color=FG))
        row.add_widget(self.copy_to)
        copy_btn = Button(text="Copy", background_color=(0.16, 0.50, 0.73, 1))
        copy_btn.bind(on_release=lambda *_: self._copy_line_range())
        hl_btn = Button(text="Select", background_color=(0.56, 0.27, 0.68, 1))
        hl_btn.bind(on_release=lambda *_: self._highlight_line_range())
        row.add_widget(copy_btn)
        row.add_widget(hl_btn)
        outer.add_widget(row)
        row2 = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(4))
        paste_btn = Button(text="Paste", size_hint_x=None, width=dp(64),
                            background_color=(0.15, 0.68, 0.38, 1))
        paste_btn.bind(on_release=lambda *_: self.paste_app_clip())
        tab_btn = Button(text="To Tab", size_hint_x=None, width=dp(72),
                          background_color=(0.9, 0.49, 0.13, 1))
        tab_btn.bind(on_release=lambda *_: self._range_to_tab())
        row2.add_widget(paste_btn)
        row2.add_widget(tab_btn)
        self.copy_status = Label(text="", color=(0.95, 0.6, 0.07, 1), halign="left", font_size=sp(11))
        self.copy_status.bind(size=lambda i, v: setattr(i, "text_size", v))
        row2.add_widget(self.copy_status)
        outer.add_widget(row2)
        with outer.canvas.before:
            Color(0.106, 0.227, 0.294, 1)
            outer._bg = Rectangle(pos=outer.pos, size=outer.size)
        outer.bind(pos=lambda i, v: setattr(outer._bg, "pos", v),
                    size=lambda i, v: setattr(outer._bg, "size", v))
        return outer

    def _parse_range(self, from_ti, to_ti, status_lbl, need_window=True):
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
        if need_window and self.large_file_mode:
            window_end = self._current_window_start + WINDOW_LINES
            if s - 1 < self._current_window_start or e > window_end:
                status_lbl.text = (
                    f"In a large file, use Goto to bring L{s}-L{e} into view first")
                return None
        return s, e

    def get_lines_text(self, s, e):
        """Text of absolute lines s..e (1-based, inclusive) -> (text, error).
        Large files are read straight from disk through the line index, so
        ANY range works (not just the 2000-line window on screen)."""
        if not self.large_file_mode:
            lines = self.text.text.split("\n")
            return "\n".join(lines[s - 1:e]), None
        buf = self.large_buffer
        ws = self._current_window_start
        self._commit_window_edits()
        if buf.has_unsaved_changes():
            if s - 1 >= ws and e <= ws + WINDOW_LINES:
                lines = self.text.text.split("\n")
                return "\n".join(lines[s - 1 - ws:e - ws]), None
            return None, "Unsaved edits in this large file - Save first"
        idx = buf.index
        if not idx.complete:
            return None, "Still indexing this file - try again in a moment"
        a, b = idx.line_range_bytes(s - 1, e)
        try:
            with open(buf.path, "rb") as f:
                f.seek(a)
                raw = f.read(max(0, b - a))
        except Exception as ex:
            return None, f"Read failed: {ex}"
        txt = raw.decode("utf-8", errors="replace")
        if txt.endswith("\n"):
            txt = txt[:-1]
        if txt.endswith("\r"):
            txt = txt[:-1]
        return txt, None

    def _copy_line_range(self):
        r = self._parse_range(self.copy_from, self.copy_to, self.copy_status, need_window=False)
        if not r:
            return
        s, e = r
        content, err = self.get_lines_text(s, e)
        if err:
            self.copy_status.text = err
            return
        n = e - s + 1
        self.app.range_clip = content       # in-app clipboard: always works, any size
        if len(content) <= CLIPBOARD_SAFE_CHARS:
            copy_to_system_clipboard(content)
            self.copy_status.text = f"Copied {n} line(s) (L{s}-L{e})"
        else:
            self.copy_status.text = (
                f"Copied {n} lines to APP clipboard (too big for Android's). "
                "Use Paste / To Tab / Save Range to File")
            info_popup("Large copy",
                       f"{n} lines ({len(content):,} characters) were copied to the "
                       "app's own clipboard because Android's system clipboard cannot "
                       "hold that much.\n\nUse 'Paste' in any tab of this app, 'To Tab' "
                       "to open the range in a new tab, or Menu > Save Range to File.",
                       size_hint=(0.85, 0.5))
        Clock.schedule_once(lambda dt: setattr(self.copy_status, "text", ""), 6)

    def paste_app_clip(self):
        clip = getattr(self.app, "range_clip", "")
        if not clip:
            self.copy_status.text = "App clipboard is empty"
            return
        self.text.insert_text(clip)
        self.copy_status.text = f"Pasted {clip.count(chr(10)) + 1} line(s)"
        Clock.schedule_once(lambda dt: setattr(self.copy_status, "text", ""), 4)

    def _range_to_tab(self):
        r = self._parse_range(self.copy_from, self.copy_to, self.copy_status, need_window=False)
        if not r:
            return
        s, e = r
        content, err = self.get_lines_text(s, e)
        if err:
            self.copy_status.text = err
            return
        src_name = self.tab_label_text().lstrip("* ")
        pane = self.app.new_tab()
        pane._enter_small_file_mode(content)
        pane._display_name = f"L{s}-{e} of {src_name}"
        pane.is_modified = True
        self.app.refresh_tab_label(pane.tab_id)
        pane._update_line_numbers()

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
        row.add_widget(Label(text="Lines/sec", size_hint_x=None, width=dp(70), color=FG))
        # Free-typed speed in lines per second (decimals OK, e.g. 0.5 or 12).
        self.scroll_speed_input = TextInput(
            text="3", multiline=False, input_filter="float",
            size_hint_x=1, halign="center",
            write_tab=False, padding=[dp(6), dp(10), dp(6), dp(6)],
        )

        def _apply_speed(*_):
            try:
                v = float(self.scroll_speed_input.text)
            except ValueError:
                return
            # Clamp to something sane so a stray "0" or "99999" can't wedge the UI.
            self.scroll_speed = max(0.1, min(v, 500.0))

        self.scroll_speed_input.bind(text=_apply_speed)
        row.add_widget(self.scroll_speed_input)
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
        # scroll_speed is in LINES per second. Convert to pixels with the
        # text's real rendered line height, then to ScrollView's 0..1 range
        # using how far the content can actually scroll (content - viewport),
        # so "5 lines/sec" really is ~5 lines/sec at any font size.
        line_h = max(getattr(self.text, "line_height", 0) or 0, 1)
        line_h += getattr(self.text, "line_spacing", 0) or 0
        pixels = self.scroll_speed * line_h * dt
        scrollable = self.text.height - self.text_scroll.height
        if scrollable <= 0:
            return
        frac = pixels / scrollable
        cur = self.text_scroll.scroll_y
        # scroll_y: 1.0 = top, 0.0 = bottom, so "down" decreases it.
        new_y = cur - frac if self.scroll_direction == "down" else cur + frac
        self.text_scroll.scroll_y = max(0.0, min(1.0, new_y))
        self._sync_gutters()

    def _build_editor_row(self):
        # ── Rebuilt editor row ──────────────────────────────────────────
        # The old layout put a static Label (line numbers) and a bare
        # TextInput side by side with no shared scroll container. That
        # caused three of the reported bugs at once:
        #   1. "Wrong line numbers" — the Label always printed 1..N top to
        #      bottom regardless of scroll position; it never tracked what
        #      was actually visible in the TextInput.
        #   2. The stray "black line" seam — the gutter's background
        #      Rectangle and the TextInput scrolled independently, so their
        #      edges drifted out of sync.
        #   3. Jittery scrolling — nothing kept the gutter's paint in step
        #      with the TextInput's own (already slightly stepped) internal
        #      scroll, so they visibly fought each other.
        #
        # Fix: wrap the TextInput in one ScrollView that we control
        # directly (do_scroll_x for the horizontal scrollbar, do_scroll_y
        # for vertical), and use LineNumberGutter (a small recycled pool of
        # row labels, see above) to redraw only the numbers actually on
        # screen to match the TextInput's *actual* rendered line layout
        # every time it scrolls or its content changes. This keeps
        # everything driven by one source of truth (the ScrollView's
        # scroll_y) instead of two widgets guessing at each other's state.
        row = BoxLayout(orientation="horizontal")
        self.bm_gutter = BookmarkGutter(self)
        row.add_widget(self.bm_gutter)

        # See LineNumberGutter above for why this replaced a single giant
        # Label living in its own ScrollView.
        self.line_number_gutter = LineNumberGutter(self)
        row.add_widget(self.line_number_gutter)

        self.text = LatexTextInput(
            pane=self, multiline=True, font_size=sp(self.font_size_pt),
            font_name=FONT_NAME,
            background_color=self.bg_color, foreground_color=self.fg_color,
            cursor_color=(1, 1, 1, 1), selection_color=SEL_BG,
            padding=[dp(6), dp(6), dp(6), dp(6)],
            do_wrap=self.word_wrap,
            size_hint=(None, None),
            scroll_distance=dp(20),
        )
        # The TextInput sizes itself to its content; the ScrollView around
        # it clips to the visible area and provides the actual scrollbars.
        # NOTE: TextInput only exposes minimum_height (from its own line
        # layout), not minimum_width — binding to a non-existent
        # "minimum_width" property crashed on startup with a KeyError.
        # Width is instead driven explicitly by _apply_word_wrap /
        # _on_scroll_view_resize below (viewport width when wrapping,
        # natural content width otherwise).
        self.text.bind(minimum_height=self.text.setter("height"))

        self.text_scroll = ScrollView(
            do_scroll_x=not self.word_wrap, do_scroll_y=True,
            bar_width=dp(8), scroll_type=["bars", "content"],
            bar_color=(0.55, 0.55, 0.55, 0.9), bar_inactive_color=(0.45, 0.45, 0.45, 0.4),
            effect_cls="ScrollEffect",  # non-bouncy: avoids the "jitter"
                                        # feel of Kivy's default overscroll
                                        # effect, which is what made
                                        # dragging feel inaccurate/rubbery.
        )
        self.text_scroll.add_widget(self.text)
        self.text_scroll.bind(on_touch_down=self._mark_user_moved)
        self.text_scroll.bind(scroll_y=lambda i, v: self._sync_gutters(),
                               scroll_x=lambda i, v: self._sync_gutters())
        self.text_scroll.bind(size=lambda i, v: self._on_scroll_view_resize())
        row.add_widget(self.text_scroll)
        self.add_widget(row)

        self.text.bind(text=self._on_text_changed)
        self.text.bind(cursor=lambda i, v: self._schedule_statusbar())
        self._apply_word_wrap()

    def _mark_user_moved(self, inst, touch):
        if inst.collide_point(*touch.pos):
            self._user_moved = True

    def _on_scroll_view_resize(self):
        if self.word_wrap:
            self.text.width = self.text_scroll.width
        self._sync_gutters()

    def _apply_word_wrap(self):
        """Re-applies word-wrap mode to both the TextInput and its
        ScrollView. Word wrap on: text reflows to the visible width, no
        horizontal scrollbar is needed. Word wrap off: text lays out at
        its natural (longest-line) width and a horizontal scrollbar
        appears so the rest can be panned into view."""
        self.text.do_wrap = self.word_wrap
        self.text_scroll.do_scroll_x = not self.word_wrap
        if self.word_wrap:
            # Constrain to the viewport width so wrapping actually kicks in,
            # instead of the TextInput sizing itself to its longest line.
            self.text.size_hint_x = 1
            self.text.width = self.text_scroll.width
        else:
            self.text.size_hint_x = None
            self._update_natural_width()
        self.text._trigger_update_graphics()
        self._sync_gutters()

    def _update_natural_width(self):
        """With word-wrap off, size the TextInput to its longest rendered
        line so there's real content to horizontally scroll through.
        TextInput has no public minimum_width (unlike minimum_height), so
        this is computed from the widths Kivy already calculated for each
        rendered row.

        Fix: the old version added a small fixed pad (dp(24)) on top of
        the raw label widths. Those label widths are just the glyph
        advance of each rendered row and don't account for the
        TextInput's own right-side padding, the cursor's own width drawn
        past the last glyph, or sub-pixel rounding in Kivy's text layout —
        so at full-right scroll the last few characters of the longest
        line sat right at (or past) the ScrollView's clip edge and looked
        clipped. Padding is now derived from the TextInput's actual
        left+right padding plus the cursor width, with a small safety
        margin, so the true end of the longest line always has room to
        scroll fully into view."""
        try:
            labels = self.text._lines_labels
            widest = max((lbl.width for lbl in labels), default=0)
        except Exception:
            widest = 0
        try:
            pad_left, pad_right = self.text.padding[0], self.text.padding[2]
        except Exception:
            pad_left, pad_right = dp(6), dp(6)
        try:
            cursor_w = self.text.cursor_width
        except Exception:
            cursor_w = dp(2)
        extra = pad_left + pad_right + cursor_w + dp(12)  # safety margin
        min_w = self.text_scroll.width
        self.text.width = max(widest + extra, min_w)

    def _sync_gutters(self, *_):
        self._gutter_trigger()

    def _do_sync_gutters(self, *_):
        # Called on every scroll_y/scroll_x change, including every pixel of
        # a drag and every auto-scroll tick — must stay cheap. Both gutters
        # now only touch the handful of on-screen rows, not the whole
        # window's worth of content (see LineNumberGutter for why the old
        # version of this was the actual cause of the slow/frozen scrolling
        # and unresponsive toolbar).
        self.line_number_gutter.redraw()
        self.bm_gutter.redraw()
        if self.get_root_window() is not None and self._pending_view is None:
            self.app.schedule_session_save()

    def _build_statusbar(self):
        bar = BoxLayout(size_hint_y=None, height=dp(26), padding=(dp(6), 0))
        # Tappable status bar: lets the user copy the current line number
        # (as shown, e.g. "Ln 460, Col 0 | 136921 lines") without needing
        # Goto or a selection first.
        self.status_label = _TappableLabel(
            text="Ln 1, Col 0", color=(0.6, 0.6, 0.6, 1),
            halign="left", font_size=sp(11),
            on_release=lambda *_a: self._copy_current_line_number())
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
        self._linenum_ev = Clock.schedule_once(lambda dt: self._on_linenum_tick(), LINENUM_DEBOUNCE_S)

    def _on_linenum_tick(self):
        self._update_line_numbers()
        if not self.word_wrap:
            self._update_natural_width()

    def _update_line_numbers(self):
        # Called when content/window/font actually changes (not on plain
        # scrolling — that goes through the cheap _sync_gutters path above).
        # Only the gutter's *width* depends on the total line count (it
        # needs to widen from e.g. 3 digits to 6), so that's all that's
        # recomputed here; the visible numbers themselves are drawn by
        # LineNumberGutter.redraw() from only the on-screen rows.
        total = self.total_lines()
        self.line_number_gutter.update_width(total)
        self.line_number_gutter.redraw()

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
        lrow = self.logical_at_row(row)
        col = self.logical_col(row, col)
        if self.large_file_mode:
            actual_line = self._current_window_start + lrow + 1
            self.status_label.text = (
                f"Ln {actual_line}, Col {col}   |   {total} lines (large file)")
        else:
            self.status_label.text = f"Ln {lrow + 1}, Col {col}   |   {total} lines"

    def _copy_current_line_number(self):
        """Tapping the status bar copies just the current line number
        (the number after "Ln " in "Ln 460, Col 0 | ... lines") to the
        system clipboard — a quick way to grab a line number for Goto,
        bookmarks, etc. without selecting text first."""
        try:
            col, row = self.text.cursor
        except Exception:
            row = 0
        lrow = self.logical_at_row(row)
        if self.large_file_mode:
            line_no = self._current_window_start + lrow + 1
        else:
            line_no = lrow + 1
        copy_to_system_clipboard(str(line_no))
        # Brief flash so the tap feels acknowledged, then restore the
        # normal status text.
        prev_text, prev_color = self.status_label.text, self.status_label.color
        self.status_label.text = f"Copied line {line_no}"
        self.status_label.color = (0.15, 0.68, 0.38, 1)

        def _restore(dt):
            self.status_label.color = prev_color
            self._update_statusbar()
        Clock.schedule_once(_restore, 0.8)

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

    # ── Wrap-aware line mapping ──────────────────────────────────────────
    # With word wrap ON one logical line spans several visual rows. Line
    # numbers, bookmarks, the status bar and saved positions must always use
    # the LOGICAL line so they match wrap-off mode.
    def _wrapped(self):
        ti = self.text
        return bool(getattr(ti, "do_wrap", False)) and bool(ti._lines_flags)

    def logical_at_row(self, row):
        """0-based logical line (within the loaded text) containing visual row."""
        if not self._wrapped():
            return max(0, row)
        flags = self.text._lines_flags
        row = min(max(0, row), len(flags) - 1)
        return sum(1 for f in flags[:row + 1] if f & 1)

    def line_start_rows(self):
        """Visual row where each logical line starts (None when wrap is off)."""
        if not self._wrapped():
            return None
        return [0] + [i for i, f in enumerate(self.text._lines_flags) if i and f & 1]

    def row_of_logical(self, l, starts=None):
        if l <= 0:
            return 0
        if starts is None:
            starts = self.line_start_rows()
        if starts is None:
            return l
        return starts[min(l, len(starts) - 1)]

    def logical_col(self, row, col):
        if not self._wrapped():
            return col
        lines, flags = self.text._lines, self.text._lines_flags
        r = min(row, len(lines) - 1)
        while r > 0 and r < len(flags) and not (flags[r] & 1):
            r -= 1
            col += len(lines[r])
        return col

    def visual_pos(self, logical, col):
        """(col, row) in visual terms for a logical line/col."""
        row = self.row_of_logical(logical)
        lines, flags = self.text._lines, self.text._lines_flags
        if self._wrapped() and lines:
            row = min(row, len(lines) - 1)
            while (col > len(lines[row]) and row + 1 < len(lines)
                   and row + 1 < len(flags) and not (flags[row + 1] & 1)):
                col -= len(lines[row])
                row += 1
        return col, row

    def _top_logical_rel(self):
        return self.logical_at_row(self._first_visible_row())

    def _top_abs_line(self):
        off = self._current_window_start if self.large_file_mode else 0
        return off + self._top_logical_rel()

    def _scroll_abs_line_later(self, abs_line):
        off = self._current_window_start if self.large_file_mode else 0
        self._scroll_rows_later(lambda: self.row_of_logical(abs_line - off))

    # ── View helpers (scroll position <-> line) ─────────────────────────
    def _line_h(self):
        return (self.text.line_height + self.text.line_spacing) or 1

    def _first_visible_row(self):
        ti, sv = self.text, self.text_scroll
        lh = self._line_h()
        hidden = max(0, max(ti.height, sv.height) - sv.height)
        frac = 1 - min(max(sv.scroll_y, 0), 1)
        return max(0, int(frac * hidden / lh + 1e-3))

    def scroll_to_row(self, row):
        ti, sv = self.text, self.text_scroll
        lh = self._line_h()
        hidden = max(0, max(ti.height, sv.height) - sv.height)
        if hidden <= 0:
            sv.scroll_y = 1
            return
        sv.scroll_y = 1 - min(1.0, max(0.0, row * lh / hidden))

    def _scroll_rows_later(self, get_row):
        """Scroll after the TextInput has finished laying out (retries)."""
        self._scroll_lock = True

        def _do(dt):
            try:
                self.scroll_to_row(max(0, int(get_row())))
            except Exception:
                pass
            self._sync_gutters()

        for d in (0, 0.05, 0.15, 0.35):
            Clock.schedule_once(_do, d)
        Clock.schedule_once(lambda dt: setattr(self, "_scroll_lock", False), 0.5)

    def current_view(self):
        """Absolute top row + cursor, for the session file."""
        if self._pending_view:
            return dict(self._pending_view)
        if self.parent is not None and self.get_root_window() is not None:
            try:
                off = self._current_window_start if self.large_file_mode else 0
                col, row = self.text.cursor
                self._last_view = {"top": off + self._top_logical_rel(),
                                   "row": off + self.logical_at_row(row),
                                   "col": self.logical_col(row, col)}
            except Exception:
                pass
        return dict(self._last_view) if self._last_view else None

    def request_view_restore(self, view):
        if not view:
            return
        self._pending_view = dict(view)
        self._user_moved = False
        self.kick_view_restore()

    def kick_view_restore(self):
        if self._pending_view:
            self._restore_try = 0
            Clock.schedule_once(self._try_apply_view, 0.25)

    def _try_apply_view(self, dt):
        pv = self._pending_view
        if not pv:
            return
        if self.large_file_mode and getattr(self, "_index_ev", None) is not None:
            Clock.schedule_once(self._try_apply_view, 0.3)    # still indexing
            return
        visible = self.parent is not None and self.get_root_window() is not None
        if not visible:
            self._scroll_lock = False
            return        # applied later, when this tab is selected
        if self._user_moved:
            self._pending_view = None
            self._scroll_lock = False
            return
        self._restore_try += 1
        off = self._current_window_start if self.large_file_mode else 0
        self._scroll_lock = True
        try:
            self.scroll_to_row(self.row_of_logical(max(0, pv["top"] - off)))
            if not pv.get("cursor_done"):
                lines = self.text._lines or []
                if lines:
                    c, r = self.visual_pos(max(0, pv["row"] - off), max(0, pv["col"]))
                    r = max(0, min(r, len(lines) - 1))
                    c = max(0, min(c, len(lines[r])))
                    self.text.cursor = (c, r)
                    pv["cursor_done"] = True
        except Exception:
            pass
        self._sync_gutters()
        self._update_statusbar()
        if self._restore_try < 4:
            Clock.schedule_once(self._try_apply_view, 0.35)
        else:
            self._pending_view = None
            self._scroll_lock = False
        Clock.schedule_once(lambda d: setattr(self, "_scroll_lock", False)
                            if self._pending_view is None else None, 0.5)

    def set_text_preserve_view(self, new_text):
        row = self._top_logical_rel()
        self._suspend_modified = True
        self.text.text = new_text
        self._suspend_modified = False
        self.is_modified = True
        if self.large_file_mode:
            self._window_dirty = True
        self.app.refresh_tab_label(self.tab_id)
        self._update_line_numbers()
        self._schedule_session_save()
        self._scroll_rows_later(lambda: self.row_of_logical(row))

    def _flush_window_edits_sync(self):
        """Commit the on-screen window AND write every edited window into the
        local file right now, so windows never overlap as separate edits."""
        if not self.large_file_mode:
            return
        self._commit_window_edits()
        buf = self.large_buffer
        if buf.has_unsaved_changes():
            buf.save_to(buf.path)
            idx = buf.index
            while idx.build_step():
                pass
            self._large_needs_writeback = True

    def goto_line(self, line_no, toggle_bookmark=False):
        scroll = not toggle_bookmark
        if self.large_file_mode:
            total = self.total_lines()
            line_no = max(1, min(line_no, total))
            target_idx = line_no - 1
            ws = self._current_window_start
            if ws <= target_idx < ws + WINDOW_LINES:
                self._place_cursor(line_no, toggle_bookmark, scroll)
                return
            self._flush_window_edits_sync()
            total = self.total_lines()
            new_start = max(0, min(target_idx - WINDOW_LINES // 2, max(0, total - WINDOW_LINES)))
            self._apply_window_shift(new_start)
            Clock.schedule_once(
                lambda dt: self._place_cursor(line_no, toggle_bookmark, scroll), 0.1)
            return
        self._place_cursor(line_no, toggle_bookmark, scroll)

    def _place_cursor(self, line_no, toggle_bookmark, scroll=True):
        off = self._current_window_start if self.large_file_mode else 0
        lines = self.text.text.split("\n")
        rel = max(0, min(line_no - 1 - off, len(lines) - 1))
        idx = sum(len(l) + 1 for l in lines[:rel])
        self.text.cursor = self.text.get_cursor_from_index(idx)
        if scroll:
            self._scroll_rows_later(lambda: self.text.cursor_row - 2)
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
        self._scroll_rows_later(lambda: self.text.cursor_row - 2)

    def tab_label_text(self):
        name = getattr(self, "_display_name", None)
        if not name:
            cf = self.current_file
            if cf and cf.startswith("content://"):
                name = name_from_uri(cf)
            elif cf:
                name = os.path.basename(cf)
            else:
                name = "Untitled"
        name = clean_display_name(name)
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

    def bm_rename(self, line_no, new_name):
        for b in self.bookmarks:
            if b["line"] == line_no:
                b["name"] = new_name.strip() or b["name"]
        self._schedule_session_save()
        self.app.refresh_bookmark_panel()

    def _adjust_bookmarks_for_delete(self, merged):
        new = []
        for b in self.bookmarks:
            ln = b["line"]
            if any(s <= ln <= e for s, e in merged):
                continue
            shift = sum(e - s + 1 for s, e in merged if e < ln)
            nb = dict(b)
            nb["line"] = ln - shift
            new.append(nb)
        self.bookmarks = new
        self.bm_gutter.redraw()
        self.app.refresh_bookmark_panel()

    # ── Edit helpers ─────────────────────────────────────────────────────
    def undo(self):
        try:
            self.text.do_undo()
        except Exception:
            pass

    def redo(self):
        try:
            self.text.do_redo()
        except Exception:
            pass

    def select_all(self):
        self.text.select_all()

    def _cur_line_bounds(self):
        content = self.text.text
        idx = self.text.cursor_index()
        ls = content.rfind("\n", 0, idx) + 1
        le = content.find("\n", idx)
        if le == -1:
            le = len(content)
        return content, ls, le

    def copy_current_line(self):
        content, ls, le = self._cur_line_bounds()
        copy_to_system_clipboard(content[ls:le])
        self.app.range_clip = content[ls:le]
        self.status_label.text = "Line copied"

    def cut_current_line(self):
        content, ls, le = self._cur_line_bounds()
        copy_to_system_clipboard(content[ls:le])
        self.app.range_clip = content[ls:le]
        end = le + 1 if le < len(content) else le
        self.set_text_preserve_view(content[:ls] + content[end:])

    def copy_line_number(self):
        content, ls, le = self._cur_line_bounds()
        off = self._current_window_start if self.large_file_mode else 0
        n = content.count("\n", 0, ls) + 1 + off
        copy_to_system_clipboard(str(n))
        self.status_label.text = f"Copied line number {n}"

    # ── Delete multiple line ranges ──────────────────────────────────────
    def delete_line_ranges(self, pairs, on_done=None):
        def _fin(n):
            if on_done:
                on_done(n)
            return n

        if self.large_file_mode:
            if not self.large_buffer.index.complete:
                info_popup("Please wait", "This file is still being indexed.")
                return _fin(0)
            self._commit_window_edits()
        total = self.total_lines()
        merged = merge_ranges(pairs, total)
        if not merged:
            return _fin(0)
        n = sum(e - s + 1 for s, e in merged)
        if not self.large_file_mode:
            lines = self.text.text.split("\n")
            out, pos = [], 0
            for s, e in merged:
                out.extend(lines[pos:s - 1])
                pos = e
            out.extend(lines[pos:])
            self.set_text_preserve_view("\n".join(out))
            self._adjust_bookmarks_for_delete(merged)
            return _fin(n)
        top_abs = self._top_abs_line()

        def _finished(result, error):
            if error == "cancelled":
                _fin(0)
                return
            if error:
                info_popup("Error", f"Delete failed:\n{error}")
                _fin(0)
                return
            self._adjust_bookmarks_for_delete(merged)
            self._reindex_after_replace(keep_top=top_abs)
            _fin(n)

        StepJob("Deleting lines...", self._delete_ranges_gen(merged), _finished)
        return None

    def _flush_dirty_gen(self):
        buf = self.large_buffer
        if buf.has_unsaved_changes():
            buf.save_to(buf.path)
            idx = buf.index
            while idx.build_step():
                yield 0.0
        return

    @staticmethod
    def _copy_span_gen(src, out, a, b, total):
        if b <= a:
            return
        src.seek(a)
        remaining = b - a
        while remaining > 0:
            chunk = src.read(min(INDEX_CHUNK, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
            yield min(1.0, (b - remaining) / max(1, total))

    def _delete_ranges_gen(self, merged):
        yield from self._flush_dirty_gen()
        buf = self.large_buffer
        idx = buf.index
        offsets = idx.offsets
        total_size = idx.total_size
        n = idx.line_count
        path = buf.path
        tmp = path + ".deltmp"
        committed = False
        try:
            with open(path, "rb") as src, open(tmp, "wb") as out:
                pos = 0
                for s, e in merged:
                    if s > n:
                        break
                    e = min(e, n)
                    start_b = offsets[s - 1]
                    end_b = offsets[e] if e < len(offsets) else total_size
                    yield from self._copy_span_gen(src, out, pos, start_b, total_size)
                    pos = end_b
                yield from self._copy_span_gen(src, out, pos, total_size, total_size)
            os.replace(tmp, path)
            committed = True
        finally:
            if not committed:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return True

    # ── Remove blank lines between two given lines ───────────────────────
    @staticmethod
    def _count_removable(lines, a, b):
        count = 0
        i = 0
        while i < len(lines):
            if lines[i].rstrip("\n") == a:
                j = i + 1
                blanks = 0
                while j < len(lines) and lines[j].strip() == "":
                    blanks += 1
                    j += 1
                if j < len(lines) and lines[j].rstrip("\n") == b and blanks > 0:
                    count += blanks
                    i = j + 1
                    continue
            i += 1
        return count

    @staticmethod
    def _remove_blanks_from_content(content, a, b):
        lines = content.split("\n")
        new_lines = []
        i = 0
        removed = 0
        while i < len(lines):
            if lines[i].rstrip("\n") == a:
                j = i + 1
                blanks = []
                while j < len(lines) and lines[j].strip() == "":
                    blanks.append(j)
                    j += 1
                if j < len(lines) and lines[j].rstrip("\n") == b and blanks:
                    new_lines.append(lines[i])
                    removed += len(blanks)
                    i = j
                    continue
            new_lines.append(lines[i])
            i += 1
        return "\n".join(new_lines), removed

    def remove_blanks_between(self, la, lb, apply, on_done):
        if not self.large_file_mode:
            content = self.text.text
            if apply:
                new, removed = self._remove_blanks_from_content(content, la, lb)
                if removed:
                    self.set_text_preserve_view(new)
                on_done(removed)
            else:
                on_done(self._count_removable(content.split("\n"), la, lb))
            return
        if not self.large_buffer.index.complete:
            info_popup("Please wait", "This file is still being indexed.")
            on_done(0)
            return
        self._commit_window_edits()

        def _finished(result, error):
            if error == "cancelled":
                on_done(0)
                return
            if error:
                info_popup("Error", f"Failed:\n{error}")
                on_done(0)
                return
            n = result or 0
            if apply and n:
                self._reindex_after_replace()
            on_done(n)

        StepJob("Scanning..." if not apply else "Removing blanks...",
                self._blank_gen(la, lb, apply), _finished)

    def _blank_gen(self, la, lb, apply):
        yield from self._flush_dirty_gen()
        path = self.large_buffer.path
        tmp = path + ".blanktmp"
        size = max(1, os.path.getsize(path))
        removed = 0
        pending = None
        committed = False
        out = open(tmp, "wb") if apply else None
        try:
            with open(path, "rb") as src:
                read = 0
                count = 0
                for raw in src:
                    read += len(raw)
                    count += 1
                    if count % 20000 == 0:
                        yield read / size
                    line = raw.decode("utf-8", errors="replace")
                    stripped = line.rstrip("\r\n")
                    if pending is not None:
                        if line.strip() == "":
                            pending.append(raw)
                            continue
                        if stripped == lb and pending:
                            removed += len(pending)
                        elif out:
                            for p in pending:
                                out.write(p)
                        pending = None
                    if out:
                        out.write(raw)
                    if stripped == la:
                        pending = []
                if pending and out:
                    for p in pending:
                        out.write(p)
            if out:
                out.close()
                out = None
                if removed:
                    os.replace(tmp, path)
                    committed = True
        finally:
            if out:
                out.close()
            if not committed:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return removed

    # ── Mark all (bookmarks every matching line) ─────────────────────────
    MAX_MARK_LINES = 500

    def mark_all(self, pattern, term):
        content = self.text.text
        off = self._current_window_start if self.large_file_mode else 0
        existing = {b["line"] for b in self.bookmarks}
        lines_all = []
        seen = set()
        count = 0
        cur_line, cur_pos = 1, 0
        for m in pattern.finditer(content):
            count += 1
            cur_line += content.count("\n", cur_pos, m.start())
            cur_pos = m.start()
            if cur_line not in seen:
                seen.add(cur_line)
                lines_all.append(cur_line)
        added = 0
        rows = content.split("\n") if lines_all else []
        for ln in lines_all[:self.MAX_MARK_LINES]:
            absl = ln + off
            if absl in existing:
                continue
            prev = rows[ln - 1][:28].strip() if ln - 1 < len(rows) else ""
            self.bookmarks.append({"line": absl, "mark": True,
                                   "name": f"[{term[:20]}] {prev}" if prev else f"[{term[:20]}] L{absl}"})
            added += 1
        self.bookmarks.sort(key=lambda b: b["line"])
        self.bm_gutter.redraw()
        self._schedule_session_save()
        self.app.refresh_bookmark_panel()
        return count, len(lines_all), added

    def clear_marks(self):
        n = len([b for b in self.bookmarks if b.get("mark")])
        self.bookmarks = [b for b in self.bookmarks if not b.get("mark")]
        self.bm_gutter.redraw()
        self._schedule_session_save()
        self.app.refresh_bookmark_panel()
        return n

    # ── Find & Replace (used by the shared dialog) ──────────────────────
    def find_next(self, pattern, forward=True, from_index=None, on_done=None):
        """Find the next match.

        Returns True/False when the answer is known immediately, or None if
        a big-file scan was started in the background. If on_done is given
        it is called exactly once with True (found), False (not found) or
        None (cancelled) -- immediately, or later when a scan finishes."""
        if self.large_file_mode:
            return self._find_next_large(pattern, forward, on_done)
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
        found = bool(m)
        if m:
            self.text.select_text(m.start(), m.end())
            self.text.cursor = self.text.get_cursor_from_index(m.end() if forward else m.start())
        if on_done:
            on_done(found)
        return found

    def _find_next_large(self, pattern, forward, on_done):
        """Search the loaded window first (instant, common case). If not
        found there, scan the rest of the file on disk in small time-boxed
        steps (see StepJob) so even a 10,000,000-line file never freezes
        the UI, and the user can cancel."""
        def _done(v):
            if on_done:
                on_done(v)
            return v

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
            return _done(True)

        if not self.large_buffer.index.complete:
            info_popup("Please wait", "This file is still being indexed. "
                                      "Try Find again in a moment.")
            return _done(False)

        # Only one scan at a time: a new request replaces a running one.
        if self._search_job is not None and not self._search_job.finished:
            self._search_job.cancel()

        def _finished(result, error):
            self._search_job = None
            if error == "cancelled":
                _done(None)
                return
            if error:
                info_popup("Error", f"Search failed:\n{error}")
                _done(False)
                return
            if result is None:
                info_popup("Not found", "No more matches in this file.")
                _done(False)
                return
            self.goto_line(result + 1)
            # Once the window has settled, highlight the match itself.
            Clock.schedule_once(lambda dt: self._select_match_at_cursor_line(pattern), 0.15)
            _done(True)

        self._search_job = StepJob("Searching...", self._scan_gen(pattern, forward), _finished)
        return None

    def _select_match_at_cursor_line(self, pattern):
        try:
            content = self.text.text
            idx = self.text.cursor_index()
            line_start = content.rfind("\n", 0, idx) + 1
            line_end = content.find("\n", idx)
            if line_end == -1:
                line_end = len(content)
            m = pattern.search(content, line_start, line_end)
            if m:
                self.text.select_text(m.start(), m.end())
        except Exception:
            pass

    def _scan_gen(self, pattern, forward=True):
        """Generator: scans the file on disk in ~1 MB, line-aligned chunks
        (using the line index), stops at the FIRST match, and returns its
        0-based line number (or None). Yields a progress fraction.

        Old version collected every match in the whole file and re-counted
        newlines from the start of the chunk for each one -- for a common
        word in a 10M-line file that is minutes of work on the UI thread.
        This one does one search per chunk and one newline count in total.
        Chunks start/end on line boundaries, so multi-byte characters are
        never split, and a few lines of look-ahead catch matches that run
        across a chunk edge."""
        buf = self.large_buffer
        index = buf.index
        offsets = index.offsets
        total_size = index.total_size
        total = index.line_count
        CHUNK = 1 << 20
        LOOK = 8

        def off(i):
            return offsets[i] if i < len(offsets) else total_size

        ws = self._current_window_start
        we = min(ws + WINDOW_LINES, total)
        if forward:
            spans = [(we, total), (0, we)]
        else:
            spans = [(0, ws), (ws, total)]
        work_total = max(1, sum(b - a for a, b in spans))
        work_done = 0

        with open(buf.path, "rb") as f:
            for a, b in spans:
                if b <= a:
                    continue
                if forward:
                    cur = a
                    while cur < b:
                        hi = min(b, len(offsets))
                        end = bisect.bisect_left(offsets, off(cur) + CHUNK, cur + 1, hi)
                        end = min(max(end, cur + 1), b)
                        hit = self._scan_chunk(f, pattern, cur, end, total, off, LOOK, True)
                        if hit is not None:
                            return hit
                        work_done += end - cur
                        cur = end
                        yield work_done / work_total
                else:
                    cur_end = b
                    while cur_end > a:
                        beg = bisect.bisect_left(offsets, off(cur_end) - CHUNK, a, cur_end)
                        beg = min(beg, cur_end - 1)
                        hit = self._scan_chunk(f, pattern, beg, cur_end, total, off, LOOK, False)
                        if hit is not None:
                            return hit
                        work_done += cur_end - beg
                        cur_end = beg
                        yield work_done / work_total
        return None

    @staticmethod
    def _scan_chunk(f, pattern, first, last, total, off, look, forward):
        """Search lines [first, last) (plus `look` lines of look-ahead that
        only serve to complete a match starting inside the range). Returns
        the 0-based line of the first (forward) / last (backward) match
        starting inside the range, else None."""
        la_last = min(last + look, total)
        start_b = off(first)
        owned_end_b = off(last)
        f.seek(start_b)
        raw = f.read(max(0, off(la_last) - start_b))
        cut = owned_end_b - start_b
        owned = raw[:cut].decode("utf-8", errors="replace")
        text = owned + raw[cut:].decode("utf-8", errors="replace")
        owned_len = len(owned)
        if forward:
            m = pattern.search(text)
            if m is None or m.start() >= owned_len:
                return None
        else:
            m = None
            for mm in pattern.finditer(text):
                if mm.start() >= owned_len:
                    break
                m = mm
            if m is None:
                return None
        return first + owned.count("\n", 0, m.start())

    def replace_all(self, pattern, repl, on_done=None):
        """Replace every match. Small files: immediate, returns the count.
        Large files: runs in small time-boxed steps with a Cancel button,
        returns None, and reports the count through on_done(n) (called
        exactly once either way)."""
        if self.large_file_mode:
            return self._replace_all_large(pattern, repl, on_done)
        content = self.text.text
        new_content, n = pattern.subn(repl, content)
        if n:
            self.set_text_preserve_view(new_content)
        if on_done:
            on_done(n)
        return n

    def _replace_all_large(self, pattern, repl, on_done):
        def _fin(n):
            if on_done:
                on_done(n)
            return n

        self._commit_window_edits()
        if self.large_buffer.has_unsaved_changes():
            # The replace rewrites the file from its on-disk bytes; doing it
            # now would silently throw away the edits you haven't saved.
            info_popup("Save first", "This large file has unsaved edits. Save "
                                     "(or Save As) first, then run Replace All.")
            return _fin(0)
        if not self.large_buffer.index.complete:
            info_popup("Please wait", "This file is still being indexed. "
                                      "Try again in a moment.")
            return _fin(0)

        def _finished(result, error):
            if error == "cancelled":
                _fin(0)          # temp file already removed; file untouched
                return
            if error:
                info_popup("Error", f"Replace failed:\n{error}")
                _fin(0)
                return
            n = result or 0
            if n:
                self._reindex_after_replace()
            _fin(n)

        StepJob("Replacing...", self._replace_gen(pattern, repl), _finished)
        return None

    def _replace_gen(self, pattern, repl):
        """Generator: streams the file through the substitution in ~4 MB
        pieces cut on line boundaries (so a single-line match is never
        split), writing a temp file, and only swaps it in if something
        actually changed. Uses an incremental UTF-8 decoder so a multi-byte
        character (Hindi, accented letters...) straddling a chunk edge is
        decoded correctly instead of being corrupted."""
        path = self.large_buffer.path
        tmp_path = path + ".replacetmp"
        size = max(1, os.path.getsize(path))
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        total_n = 0
        carry = ""
        committed = False
        try:
            with open(path, "rb") as src, open(tmp_path, "wb") as out:
                pos = 0
                while True:
                    chunk = src.read(INDEX_CHUNK)
                    final = not chunk
                    text = carry + decoder.decode(chunk, final)
                    pos += len(chunk)
                    if final:
                        emit, carry = text, ""
                    else:
                        nl = text.rfind("\n")
                        if nl != -1:
                            emit, carry = text[:nl + 1], text[nl + 1:]
                        else:
                            emit, carry = "", text   # one enormous line: keep reading
                    if emit:
                        new_emit, n = pattern.subn(repl, emit)
                        total_n += n
                        out.write(new_emit.encode("utf-8", errors="replace"))
                    if final:
                        break
                    yield pos / size
            if total_n:
                os.replace(tmp_path, path)
                committed = True
        finally:
            if not committed:
                try:
                    os.remove(tmp_path)   # cancelled / error / nothing to change
                except OSError:
                    pass
        return total_n

    def _reindex_after_replace(self, keep_top=None, mark_modified=True):
        """File on disk changed shape: rebuild the line index (incrementally,
        on the clock) and reload the window so the screen matches. The local
        cache differs from the original now, so flag it for write-back."""
        buf = self.large_buffer
        path = buf.path
        if keep_top is None:
            keep_top = self._top_abs_line()
        buf.index = LineIndex(path)
        buf.dirty_ranges = {}
        self._window_dirty = False
        if mark_modified:
            self.is_modified = True
            self._large_needs_writeback = True
            self.app.refresh_tab_label(self.tab_id)
            self._schedule_session_save()
        idx = buf.index

        def _reindex_step(dt):
            more = idx.build_step()
            if more:
                return True
            total = max(1, idx.line_count)
            keep = max(0, min(keep_top, total - 1))
            start = max(0, min(keep - WINDOW_LINES // 2, total - WINDOW_LINES))
            self._apply_window_shift(start, top_abs=keep)
            return False
        Clock.schedule_interval(_reindex_step, 0)

    # ── Font / colour ────────────────────────────────────────────────────
    def set_font_size(self, size):
        self.font_size_pt = size
        self.text.font_size = sp(size)
        self._update_line_numbers()
        if not self.word_wrap:
            # Line widths change with font size; recompute once Kivy has
            # relaid the text at the new size, or wrap-off mode keeps the
            # old (now wrong) scroll width and clips/under-scrolls.
            Clock.schedule_once(lambda dt: self._update_natural_width(), 0)

    def set_fg_color(self, color):
        self.fg_color = color
        self.text.foreground_color = color

    def set_bg_color(self, color):
        self.bg_color = color
        self.text.background_color = color

    def toggle_wordwrap(self):
        self.word_wrap = not self.word_wrap
        self._apply_word_wrap()

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
                with open(path, "r", encoding=detect_encoding(path),
                          errors="replace", newline=None) as f:
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
            # Remember the real local path separately from current_file:
            # current_file may be a content:// URI (save_target, for "Save"
            # to write back to the original location) while `path` here is
            # always a real filesystem path we can reopen next launch.
            self._large_local_path = path
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
        # LineIndex splits on the raw byte b"\n", which is only valid for
        # ASCII-compatible encodings (not UTF-16/32), so those fall back
        # to a single-byte codec that at least never shows U+FFFD boxes.
        _enc = detect_encoding(path)
        if _enc.lower().replace("_", "-").startswith(("utf-16", "utf-32")):
            _enc = "cp1252"
        self.large_buffer.encoding = _enc
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
        pv = self._pending_view
        start = 0
        if pv:
            total = max(1, self.large_buffer.index.line_count)
            start = max(0, min(pv["top"] - WINDOW_LINES // 2, total - WINDOW_LINES))
        self._current_window_start = start
        self._suspend_modified = True
        self.text.text = self.large_buffer.read_window(start)
        self._suspend_modified = False
        self.is_modified = False
        self.app.refresh_tab_label(self.tab_id)
        self._update_line_numbers()
        self._update_statusbar()
        self.text_scroll.bind(scroll_y=self._check_window_scroll)
        if pv:
            self.kick_view_restore()

    def _check_window_scroll(self, instance, value):
        """Near the top or bottom of the loaded window, swap in the
        previous/next window (keeping the same line at the top of the screen)."""
        if not self.large_file_mode or self._scroll_lock:
            return
        if value > 0.92:
            self._shift_window(-WINDOW_LINES // 2)
        elif value < 0.08:
            self._shift_window(WINDOW_LINES // 2)

    def _shift_window(self, delta_lines):
        if self._pending_window_start is not None or self._scroll_lock:
            return
        if not self.large_buffer.index.complete:
            return
        total = self.total_lines()
        max_start = max(0, total - WINDOW_LINES)
        new_start = max(0, min(self._current_window_start + delta_lines, max_start))
        if new_start == self._current_window_start:
            return
        top_abs = self._top_abs_line()
        self._flush_window_edits_sync()
        self._pending_window_start = new_start
        Clock.schedule_once(lambda dt: self._apply_window_shift(new_start, top_abs), 0)

    def _apply_window_shift(self, new_start, top_abs=None):
        text = self.large_buffer.read_window(new_start)
        self._current_window_start = new_start
        self._window_dirty = False
        self._suspend_modified = True
        self.text.text = text
        self._suspend_modified = False
        self._pending_window_start = None
        self._update_line_numbers()
        self._update_statusbar()
        if top_abs is not None:
            self._scroll_abs_line_later(top_abs)
        else:
            self.text_scroll.scroll_y = 0.5

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
        self._commit_window_edits()
        buf = self.large_buffer
        local = self._large_local_path or buf.path
        tgt = path if path is not None else self.current_file
        if not tgt:
            return False
        dest_uri = dest_path = new_name = None
        if isinstance(tgt, tuple):
            dest_uri, new_name = tgt[1], tgt[2]
        elif tgt.startswith("content://"):
            dest_uri = tgt
        else:
            dest_path = tgt
        need_local = buf.has_unsaved_changes()
        if (not need_local and not self._large_needs_writeback and path is None):
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)
            return True
        old_total = self.total_lines()
        popup = info_popup(
            "Saving...",
            "Writing changes back to the file. This can take a moment for "
            "very large files - please don't close the app.",
            size_hint=(0.8, 0.3))
        try:
            popup.content.children[0].disabled = True
            popup.content.children[0].opacity = 0.4
        except Exception:
            pass

        def done(err):
            popup.dismiss()
            if err:
                info_popup("Error", f"Could not save file:\n{err}")
                return
            if dest_uri:
                self.current_file = dest_uri
            elif dest_path:
                self.current_file = dest_path
                new_name_l = os.path.basename(dest_path)
                self._display_name = new_name_l
            if new_name:
                self._display_name = new_name
            self._large_needs_writeback = False
            self.is_modified = False
            self.app.refresh_tab_label(self.tab_id)
            if need_local:
                self._reindex_after_save(old_total)

        def work():
            err = None
            try:
                if need_local:
                    buf.save_to(local)
                if dest_uri:
                    err = copy_file_to_uri(local, dest_uri)
                elif dest_path and os.path.abspath(dest_path) != os.path.abspath(local):
                    shutil.copyfile(local, dest_path)
            except Exception as e:
                err = str(e)
            Clock.schedule_once(lambda dt: done(err))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _reindex_after_save(self, old_total):
        idx = self.large_buffer.index
        top = self._top_abs_line()

        def _step(dt):
            if idx.build_step():
                return True
            if idx.line_count != old_total:
                self._apply_window_shift(self._current_window_start, top_abs=top)
            return False
        Clock.schedule_interval(_step, 0)

    def _save_small_file(self, path=None):
        target = path if path is not None else self.current_file
        if not target:
            return False
        text = self.text.text
        if isinstance(target, tuple) and target and target[0] == "content":
            _, uri, display_name = target
            err = write_text_to_uri(uri, text)
            if err:
                info_popup("Error", f"Could not save file:\n{err}")
                return False
            self.current_file = uri
            self._display_name = display_name
        elif isinstance(target, str) and target.startswith("content://"):
            err = write_text_to_uri(target, text)
            if err:
                info_popup("Error", f"Could not save file:\n{err}")
                return False
        else:
            try:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                info_popup("Error", f"Could not save file:\n{e}")
                return False
            self.current_file = target
            self._display_name = os.path.basename(target)
        self.is_modified = False
        self.app.refresh_tab_label(self.tab_id)
        return True

    def to_state(self):
        base = {
            "current_file": self.current_file,
            "display_name": clean_display_name(getattr(self, "_display_name", None) or ""),
            "bookmarks": self.bookmarks,
            "font_size": self.font_size_pt,
            "wrap": self.word_wrap,
            "view": self.current_view(),
        }
        if self.large_file_mode:
            # Never serialize a huge file's content into the session JSON; it
            # is re-opened from its local cache path and re-indexed instead.
            base["local_path"] = self._large_local_path or self.current_file
            base["large_file"] = True
            base["needs_writeback"] = self._large_needs_writeback
            return base
        base["text"] = self.text.text
        return base

    def from_state(self, state):
        self.bookmarks = state.get("bookmarks", []) or []
        self.set_font_size(state.get("font_size", 14))
        wrap = state.get("wrap", True)
        if wrap != self.word_wrap:
            self.word_wrap = wrap
            self._apply_word_wrap()
        dn = clean_display_name(state.get("display_name") or "")
        view = state.get("view")
        if state.get("large_file"):
            local_path = state.get("local_path") or state.get("current_file", "") or ""
            current_file = state.get("current_file", "") or local_path
            if local_path and os.path.exists(local_path):
                self.current_file = current_file
                self._large_local_path = local_path
                self._large_needs_writeback = bool(state.get("needs_writeback"))
                self._display_name = dn or (
                    name_from_uri(current_file) if current_file.startswith("content://")
                    else clean_display_name(os.path.basename(current_file or local_path)))
                if view:
                    self._pending_view = dict(view)
                self._enter_large_file_mode(local_path)
                if self._large_needs_writeback:
                    self.is_modified = True
            else:
                self._suspend_modified = True
                self.text.text = ""
                self._suspend_modified = False
                self.current_file = ""
                info_popup("File not found",
                           f"Could not restore large file:\n{dn or local_path}\n"
                           "It may have been moved or deleted.")
            self.bm_gutter.redraw()
            return
        self._suspend_modified = True
        self.text.text = state.get("text", "")
        self._suspend_modified = False
        self.current_file = state.get("current_file", "") or ""
        if dn:
            self._display_name = dn
        self.is_modified = False
        self._update_line_numbers()
        self.bm_gutter.redraw()
        self.request_view_restore(view)


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

        mark_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        mark_btn = Button(text="Mark All (bookmark lines)", background_color=(0.56, 0.27, 0.68, 1))
        clear_btn = Button(text="Clear Marks", size_hint_x=None, width=dp(110),
                            background_color=(0.4, 0.4, 0.4, 1))
        mark_btn.bind(on_release=lambda *_: self._mark_all())
        clear_btn.bind(on_release=lambda *_: self._clear_marks())
        mark_row.add_widget(mark_btn)
        mark_row.add_widget(clear_btn)
        self.add_widget(mark_row)

        self.status = Label(text="", size_hint_y=None, height=dp(40), color=(0.95, 0.6, 0.07, 1),
                            halign="center")
        self.status.bind(width=lambda i, v: setattr(i, "text_size", (v, None)))
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
        def _report(found):
            self.status.text = ("Cancelled." if found is None
                                else "Found." if found else "Not found.")

        if pane.find_next(pat, forward=forward, on_done=_report) is None:
            self.status.text = "Searching..."

    def _mark_all(self):
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
        count, nlines, added = pane.mark_all(pat, self.find_entry.text)
        if not count:
            self.status.text = "No matches found."
            return
        note = " (loaded part of large file only)" if pane.large_file_mode else ""
        self.status.text = (f"Marked {count} match(es) on {nlines} line(s); "
                            f"{added} bookmark(s) added{note}.")

    def _clear_marks(self):
        pane = self._current_pane()
        if pane:
            self.status.text = f"Cleared {pane.clear_marks()} mark(s)."

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
                panes = list(self.app.all_panes())
                state = {"total": 0, "tabs": 0}

                def _next(i=0):
                    if i >= len(panes):
                        self.status.text = (f"Replaced {state['total']} occurrence(s) "
                                            f"across {state['tabs']} tab(s).")
                        return

                    def _cb(n):
                        if n:
                            state["total"] += n
                            state["tabs"] += 1
                        _next(i + 1)
                    panes[i].replace_all(pat, repl, on_done=_cb)
                _next()
            confirm_popup("Replace All - All Tabs",
                          f"Replace all occurrences across {len(self.app.all_panes())} open tab(s)?",
                          _do)
        else:
            pane = self._current_pane()
            if not pane:
                return
            def _fin(n):
                self.status.text = f"Replaced {n} occurrence(s) (current tab)."
            if pane.replace_all(pat, repl, on_done=_fin) is None:
                self.status.text = "Replacing..."


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
        super().__init__(orientation="vertical", size_hint_x=None, width=dp(250), **kw)
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

    def collapse(self):
        self.is_open = False
        collapse_widget(self)
        self.width = 0

    def expand(self):
        self.is_open = True
        expand_widget(self, restore_outer_size=False)
        self.width = dp(250)
        self.refresh()

    def _clear_all(self):
        pane = self.app.current_pane()
        if pane:
            pane.bm_clear_all()

    def refresh(self):
        if not getattr(self, "is_open", False):
            return  # hidden: don't build widgets that would sit un-collapsed
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
            rn = Button(text="Ab", size_hint_x=None, width=dp(34),
                        background_color=(0.2, 0.35, 0.5, 1))
            rn.bind(on_release=lambda i, l=ln, nm=b["name"]: prompt_popup(
                "Rename bookmark", f"Name for line {l}:", nm,
                on_ok=lambda v, l=l: self.app.current_pane().bm_rename(l, v)))
            rm = Button(text="x", size_hint_x=None, width=dp(32),
                        background_color=(0.5, 0.2, 0.2, 1))
            rm.bind(on_release=lambda i, l=ln: self.app.current_pane().bm_remove(l))
            row.add_widget(lbl)
            row.add_widget(rn)
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
        row = BoxLayout(size_hint_x=None, width=dp(170))
        btn = ToggleButton(text=label, group="tabs", size_hint_x=1,
                            background_color=(0.2, 0.2, 0.2, 1), shorten=True,
                            shorten_from="right", halign="center", valign="middle")
        btn.bind(size=lambda i, v: setattr(i, "text_size", (max(0, v[0] - dp(10)), v[1])))
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
            # Bug fix: str(uri) on a raw pyjnius Java object does NOT call
            # Android's Uri.toString() — pyjnius has no __str__ override for
            # arbitrary autoclassed types, so str() silently falls back to
            # the debug repr "<android.net.Uri at 0x... jclass=... jself=...>".
            # That garbage string was what got written into current_file and
            # persisted to the session JSON, so every restart tried to
            # os.path.exists() that repr (always False) and showed "File not
            # found" even though the real file was untouched. Call
            # .toString() explicitly to get the real content:// URI string.
            uri_str = uri.toString()
            Clock.schedule_once(lambda dt: on_choose(("content_local", uri_str, display_name, local_path)))

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
        # See the matching fix/comment in _android_open_document above:
        # str(uri) is not Uri.toString() for a pyjnius object.
        uri_str = uri.toString()
        Clock.schedule_once(lambda dt: on_saved(("content", uri_str, display_name)))

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
#  Tools dialogs ported from the desktop app
#  (Delete Lines / Remove Blank Lines Between / Special Copy Mode)
# ══════════════════════════════════════════════════════════════════════════

def _wrap_label(text, height=dp(24), color=FG, **kw):
    lbl = Label(text=text, size_hint_y=None, height=height, color=color,
                halign="left", valign="middle", **kw)
    lbl.bind(width=lambda i, v: setattr(i, "text_size", (v, None)))
    return lbl


def open_delete_lines_dialog(app):
    pane = app.current_pane()
    if not pane:
        return
    content = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
    content.add_widget(_wrap_label(
        "Add line pairs (from / to). Press Delete All to remove every range at once.",
        height=dp(40), font_size=sp(12)))
    grid = GridLayout(cols=1, spacing=dp(4), size_hint_y=None)
    grid.bind(minimum_height=grid.setter("height"))
    sv = ScrollView(size_hint_y=0.4)
    sv.add_widget(grid)
    content.add_widget(sv)
    rows = []

    def add_row(*_):
        r = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(4))
        f = TextInput(multiline=False, input_filter="int", hint_text="from")
        t = TextInput(multiline=False, input_filter="int", hint_text="to")
        x = Button(text="x", size_hint_x=None, width=dp(38), background_color=(0.5, 0.2, 0.2, 1))
        r.add_widget(f)
        r.add_widget(Label(text="to", size_hint_x=None, width=dp(24), color=FG))
        r.add_widget(t)
        r.add_widget(x)
        entry = (f, t, r)
        rows.append(entry)
        grid.add_widget(r)

        def _rm(*_):
            if entry in rows:
                rows.remove(entry)
            grid.remove_widget(r)
        x.bind(on_release=_rm)

    add_row()
    add_btn = Button(text="+ Add pair", size_hint_y=None, height=dp(40),
                      background_color=(0.16, 0.50, 0.73, 1))
    add_btn.bind(on_release=add_row)
    content.add_widget(add_btn)
    content.add_widget(_wrap_label('Or paste pairs, one per line ("20 301", "503-3000", "12,15"):',
                                   height=dp(34), font_size=sp(12)))
    paste = TextInput(multiline=True, size_hint_y=0.3)
    content.add_widget(paste)
    summary = _wrap_label("", height=dp(48), color=(0.95, 0.6, 0.07, 1), font_size=sp(12))
    content.add_widget(summary)

    def gather():
        pairs, bad = [], 0
        for f, t, _ in rows:
            a, b = f.text.strip(), t.text.strip()
            if not a and not b:
                continue
            try:
                pairs.append((int(a), int(b)))
            except ValueError:
                bad += 1
        for line in paste.text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p for p in re.split(r"[,\-\s]+", line) if p]
            try:
                if len(parts) != 2:
                    raise ValueError
                pairs.append((int(parts[0]), int(parts[1])))
            except ValueError:
                bad += 1
        return pairs, bad

    def summarize():
        pairs, bad = gather()
        total = pane.total_lines()
        merged = merge_ranges(pairs, total)
        n = sum(e - s + 1 for s, e in merged)
        msg = f"Will delete {n} line(s) in {len(merged)} range(s) (file has {total} lines)."
        if bad:
            msg += f"  {bad} entry(ies) could not be read and are skipped."
        summary.text = msg
        return merged

    def preview(*_):
        merged = summarize()
        if merged:
            pane.goto_line(merged[0][0])

    def do_delete(*_):
        merged = summarize()
        if not merged:
            summary.text = "Nothing to delete - add at least one valid pair."
            return
        n = sum(e - s + 1 for s, e in merged)
        pairs, _ = gather()

        def _go():
            def _done(deleted):
                summary.text = f"Deleted {deleted} line(s)."
                if deleted:
                    popup.dismiss()
            pane.delete_line_ranges(pairs, on_done=_done)
        confirm_popup("Confirm delete",
                      f"Delete {n} line(s) across {len(merged)} range(s)?\n"
                      "(Use Menu > Undo for small files.)", _go)

    btns = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
    b1 = Button(text="Preview", background_color=(0.56, 0.27, 0.68, 1))
    b2 = Button(text="Delete All", background_color=(0.75, 0.22, 0.17, 1))
    b3 = Button(text="Close")
    b1.bind(on_release=preview)
    b2.bind(on_release=do_delete)
    for b in (b1, b2, b3):
        btns.add_widget(b)
    content.add_widget(btns)
    popup = Popup(title="Delete Lines - multiple ranges", content=content,
                  size_hint=(0.97, 0.95), background_color=(0.1, 0.1, 0.1, 1))
    b3.bind(on_release=popup.dismiss)
    popup.open()


def open_remove_blank_dialog(app):
    if not app.current_pane():
        return
    content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))
    content.add_widget(_wrap_label(
        "Type the exact text of Line A and Line B. Blank lines sitting between "
        "A and B are deleted.", height=dp(48), font_size=sp(12)))
    content.add_widget(_wrap_label("Line A (upper line):", height=dp(22)))
    ta = TextInput(multiline=False, size_hint_y=None, height=dp(42))
    content.add_widget(ta)
    content.add_widget(_wrap_label("Line B (lower line):", height=dp(22)))
    tb = TextInput(multiline=False, size_hint_y=None, height=dp(42))
    content.add_widget(tb)
    scope = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
    cur_btn = ToggleButton(text="Current Tab", group="blank_scope", state="down")
    all_btn = ToggleButton(text="All Tabs", group="blank_scope")
    scope.add_widget(cur_btn)
    scope.add_widget(all_btn)
    content.add_widget(scope)
    status = _wrap_label("", height=dp(48), color=(0.95, 0.6, 0.07, 1), font_size=sp(12))
    content.add_widget(status)

    def run(apply):
        la, lb = ta.text, tb.text
        if not la or not lb:
            status.text = "Both fields are required."
            return
        panes = app.all_panes() if all_btn.state == "down" else [app.current_pane()]
        st = {"total": 0, "tabs": 0}

        def nxt(i=0):
            if i >= len(panes):
                verb = "Removed" if apply else "Found"
                status.text = (f"{verb} {st['total']} blank line(s) in {st['tabs']} tab(s)."
                               if st["total"] else "No blank lines found between those lines.")
                return

            def cb(n):
                if n:
                    st["total"] += n
                    st["tabs"] += 1
                nxt(i + 1)
            panes[i].remove_blanks_between(la, lb, apply, cb)
        nxt()

    btns = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
    p_btn = Button(text="Preview", background_color=(0.56, 0.27, 0.68, 1))
    r_btn = Button(text="Remove Blanks", background_color=(0.15, 0.68, 0.38, 1))
    c_btn = Button(text="Close")
    p_btn.bind(on_release=lambda *_: run(False))
    r_btn.bind(on_release=lambda *_: run(True))
    for b in (p_btn, r_btn, c_btn):
        btns.add_widget(b)
    content.add_widget(btns)
    content.add_widget(Widget())
    popup = Popup(title="Remove blank lines between two lines", content=content,
                  size_hint=(0.95, 0.8), background_color=(0.1, 0.1, 0.1, 1))
    c_btn.bind(on_release=popup.dismiss)
    popup.open()


def open_scm_dialog(app):
    """Special Copy Mode, phone edition: append a line range to a chosen
    output file (each save adds the text plus a blank-line separator)."""
    pane = app.current_pane()
    if not pane:
        return
    content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))
    content.add_widget(_wrap_label(
        "Save line ranges into an output file (appended each time, works for "
        "ranges of any size).", height=dp(48), font_size=sp(12)))
    file_lbl = _wrap_label("", height=dp(34), color=(0.15, 0.68, 0.38, 1), font_size=sp(12))
    content.add_widget(file_lbl)

    def refresh_file_label():
        file_lbl.text = ("Output: " + app.scm_output_name) if app.scm_output else "No output file chosen"
    refresh_file_label()

    def choose(*_):
        def _got(res):
            if isinstance(res, tuple):
                app.scm_output, app.scm_output_name = res[1], res[2]
            else:
                app.scm_output, app.scm_output_name = res, os.path.basename(res)
            refresh_file_label()
            app.schedule_session_save()
        open_file_browser("Choose output file", _got, save_mode=True, default_name="extracted.txt")

    def clear(*_):
        app.scm_output = None
        app.scm_output_name = ""
        refresh_file_label()
        app.schedule_session_save()

    r0 = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
    cb_ = Button(text="Choose Output File", background_color=(0.18, 0.34, 0.49, 1))
    cl_ = Button(text="Clear", size_hint_x=None, width=dp(80))
    cb_.bind(on_release=choose)
    cl_.bind(on_release=clear)
    r0.add_widget(cb_)
    r0.add_widget(cl_)
    content.add_widget(r0)
    r1 = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
    f = TextInput(multiline=False, input_filter="int", hint_text="from line")
    t = TextInput(multiline=False, input_filter="int", hint_text="to line")
    r1.add_widget(f)
    r1.add_widget(t)
    content.add_widget(r1)
    status = _wrap_label("", height=dp(48), color=(0.95, 0.6, 0.07, 1), font_size=sp(12))
    content.add_widget(status)

    def save(*_):
        if not app.scm_output:
            status.text = "Choose an output file first."
            return
        r = pane._parse_range(f, t, status, need_window=False)
        if not r:
            return
        s, e = r
        text, err = pane.get_lines_text(s, e)
        if err:
            status.text = err
            return
        err = append_text_to_target(app.scm_output, text + "\n\n\n")
        status.text = (f"Error: {err}" if err else
                       f"Saved L{s}-L{e} ({e - s + 1} lines) -> {app.scm_output_name}")

    btns = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
    sv_btn = Button(text="Append Range to File", background_color=(0.15, 0.68, 0.38, 1))
    c_btn = Button(text="Close", size_hint_x=None, width=dp(90))
    sv_btn.bind(on_release=save)
    btns.add_widget(sv_btn)
    btns.add_widget(c_btn)
    content.add_widget(btns)
    content.add_widget(Widget())
    popup = Popup(title="Save Line Range to File", content=content,
                  size_hint=(0.95, 0.7), background_color=(0.1, 0.1, 0.1, 1))
    c_btn.bind(on_release=popup.dismiss)
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
        body.add_widget(self.bookmark_panel)
        # Start fully collapsed: zero every fixed-size descendant too (see
        # collapse_widget), or the hidden panel's "Clear All" button etc.
        # keep their full size and can swallow taps meant for other UI.
        self.bookmark_panel.collapse()
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
            # on_press (fires the instant a finger touches down) rather than
            # on_release (fires on touch-up, but only if the same touch is
            # still "grabbed" by this exact button when it lifts). Under any
            # frame lag — a slow phone, a big file still doing work in the
            # background — the up-half of a tap can arrive late enough that
            # Kivy no longer resolves it back to this button, so the tap is
            # silently swallowed and the button looks dead even though the
            # press itself landed fine. on_press has no such round trip.
            def _safe(*args, _cb=cb, _name=text):
                # A crash inside any toolbar action must never leave the UI
                # jammed: show the error and carry on.
                try:
                    return _cb(*args)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    try:
                        info_popup("Error", f"'{_name}' failed:\n{e}")
                    except Exception:
                        pass
            b.bind(on_press=_safe)
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
        self.wrap_btn = mk(row2, "Wrap: On", lambda *_: self.app.toggle_current_wordwrap())

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
        self.range_clip = ""             # in-app clipboard (any size)
        self.scm_output = None           # output file for "Save Line Range to File"
        self.scm_output_name = ""

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
        prev = self.panes.get(self._active_tab_id)
        if prev is not None and prev is not self.panes[tab_id]:
            prev.current_view()          # remember where it was before hiding it
        self._active_tab_id = tab_id
        holder = self.root_widget.pane_holder
        holder.clear_widgets()
        pane = self.panes[tab_id]
        pane.size_hint = (1, 1)
        pane.pos_hint = {"x": 0, "y": 0}
        holder.add_widget(pane)
        pane.kick_view_restore()
        self.schedule_session_save()
        self.root_widget.tab_bar.set_active(tab_id)
        self.refresh_bookmark_panel()
        try:
            self.root_widget.wrap_btn.text = "Wrap: On" if pane.word_wrap else "Wrap: Off"
        except Exception:
            pass
        if self.find_replace_content:
            self.find_replace_content.status.text = (
                f"Now targeting: {pane.tab_label_text().lstrip('* ')}")

    def _cycle_tab(self, step):
        ids = list(self.panes.keys())
        if len(ids) < 2 or self._active_tab_id not in ids:
            return
        self.select_tab(ids[(ids.index(self._active_tab_id) + step) % len(ids)])

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
        if getattr(panel, "is_open", False):
            panel.collapse()
        else:
            panel.expand()

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
        row_btn("Next Tab", lambda: self._cycle_tab(1))
        row_btn("Previous Tab", lambda: self._cycle_tab(-1))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("Undo", lambda: self._delegate("undo"))
        row_btn("Redo", lambda: self._delegate("redo"))
        row_btn("Select All", lambda: self._delegate("select_all"))
        row_btn("Copy Current Line", lambda: self._delegate("copy_current_line"))
        row_btn("Cut Current Line", lambda: self._delegate("cut_current_line"))
        row_btn("Paste from App Clipboard", lambda: self._delegate("paste_app_clip"))
        row_btn("Copy Current Line Number", lambda: self._delegate("copy_line_number"))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("Delete Lines (multiple ranges)...", lambda: open_delete_lines_dialog(self))
        row_btn("Remove Blank Lines Between Two Lines...", lambda: open_remove_blank_dialog(self))
        row_btn("Save Line Range to File...", lambda: open_scm_dialog(self))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("Toggle Bookmark on Line", lambda: self._delegate("bm_toggle_current"))
        row_btn("Next Bookmark", lambda: self._delegate("bm_next", True))
        row_btn("Previous Bookmark", lambda: self._delegate("bm_next", False))
        row_btn("Clear Bookmarks in Tab", lambda: self._delegate("bm_clear_all"))
        inner.add_widget(Widget(size_hint_y=None, height=dp(1)))
        row_btn("Toggle Word Wrap", lambda: self._delegate("toggle_wordwrap"))
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

    def toggle_current_wordwrap(self):
        pane = self.current_pane()
        if not pane:
            return
        pane.toggle_wordwrap()
        try:
            self.root_widget.wrap_btn.text = "Wrap: On" if pane.word_wrap else "Wrap: Off"
        except Exception:
            pass

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
            "- Find & Replace, with Replace All across all tabs\n"
            "- Bookmarks with a jump panel\n"
            "- Go to Line\n"
            "- Copy / Replace line ranges (copy works on huge files)\n"
            "- Delete multiple line ranges, remove blank lines between two lines\n"
            "- Save line ranges to an output file, Mark All, undo/redo\n"
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
        panes = list(self.panes.values())
        ids = list(self.panes.keys())
        data = {
            "tabs": [pane.to_state() for pane in panes],
            "active_index": ids.index(self._active_tab_id) if self._active_tab_id in ids else 0,
            "scm_output": self.scm_output,
            "scm_output_name": self.scm_output_name,
        }
        save_session(data)

    def _restore_session(self):
        data = load_session()
        if not data:
            return
        self.scm_output = data.get("scm_output")
        self.scm_output_name = data.get("scm_output_name", "") or ""
        for state in data.get("tabs", []):
            self.new_tab(restore_state=state)
        ids = list(self.panes.keys())
        idx = data.get("active_index", 0)
        if ids and 0 <= idx < len(ids):
            self.select_tab(ids[idx])       # come back on the SAME tab

    def _auto_save_loop(self):
        self._save_session()

    def on_pause(self):
        # Android may kill a backgrounded app without ever calling on_stop,
        # so persist tabs + scroll position + cursor right now.
        for pane in self.panes.values():
            pane.current_view()
        self._save_session()
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        for pane in self.panes.values():
            pane.stop_scroll()
            pane.current_view()
        self._save_session()


if __name__ == "__main__":
    LatexNotepadApp().run()
