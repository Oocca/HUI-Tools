#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║      HUI VFD Display  v1.2  — Dot-Matrix         ║
║  Renders the Mackie HUI 2×40 vacuum-fluorescent  ║
║  display as an accurate 5×7 dot-matrix pixel     ║
║  grid, scalable to any monitor resolution.       ║
║  Part of the HUI Tools suite.                    ║
╚══════════════════════════════════════════════════╝

© 2026 Richard Philip
Released under the GNU General Public License v3.0
https://www.gnu.org/licenses/gpl-3.0.html

Protocol reference: HUI_MIDI_protocol.pdf (theageman, 2010)
  - Main display command: 0x12
  - 8 zones (0-7), 10 characters each
  - Zones 0-3 -> top row, zones 4-7 -> bottom row

Shares config.hui with HUI-Display.
Use Menu > Configure to change settings.
"""

import sys
import os
import json
import threading
import webbrowser
import mido
import tkinter as tk
from tkinter import ttk, colorchooser


# ╔═══════════════════════════════════════════════════════════╗
# ║  CONFIGURATION                                            ║
# ╚═══════════════════════════════════════════════════════════╝

_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, 'config.hui')

_DEFAULTS = {
    'pt_sends_to':  'HUI In',
    'hui_out_port': 'HUI',
    'hui_in_port':  'HUI',
    'pt_recv_from': 'HUI Out',
    'fg_color':      '#d1e9e4',   # VFD phosphor teal  (H112 S85 L208 / R209 G233 B228)
    'bg_color':      '#060a08',   # Near-black background
    'dim_color':     '#003d2c',   # Status bar / labels
    'font_size':     15,          # Text-mode font size in points
    'pixel_size':    5,           # Dot-matrix: screen pixels per VFD dot
    'display_mode':  'dot_matrix',# 'dot_matrix' | 'text'
    'text_font':     'Consolas',  # Text-mode monospace font
}

# Monospace fonts available in text mode (user may also type any name)
_TEXT_FONTS = [
    'Consolas',
    'Courier New',
    'Lucida Console',
    'Cascadia Mono',
    'Cascadia Code',
    'Fixedsys Excelsior 3.01',
    'Terminal',
    'Fixedsys',
    'OCR A Extended',
]

# ── 5×7 Dot-Matrix Font ─────────────────────────────────────
#
# Each entry is a 7-tuple of 5-bit integers, one per display row (top → bottom).
# Bit 4 = leftmost pixel, bit 0 = rightmost pixel.
# e.g.  14 = 01110 = .XXX.
#        17 = 10001 = X...X
#        31 = 11111 = XXXXX
#
# Covers printable ASCII (32–126) plus selected HUI-2 control characters.

FONT_5X7 = {
    # ── Special HUI-2 characters ──────────────────────────────
    0x19: ( 3,  5,  4,  4, 12, 12,  0),  # ♪  music note
    0x1A: (12, 18, 12,  0,  0,  0,  0),  # °  degree sign
    0x1B: (12, 18, 12,  0,  0,  0,  0),  # °  degree sign (alt)
    0x1C: (31, 14,  4,  0,  0,  0,  0),  # ▼  down arrow
    0x1D: (16, 24, 30, 31, 30, 24, 16),  # ►  right arrow
    0x1E: ( 1,  3, 15, 31, 15,  3,  1),  # ◄  left arrow
    0x1F: ( 0,  0,  0,  4, 14, 31,  0),  # ▲  up arrow

    # ── ASCII 32–47 (punctuation and symbols) ─────────────────
    32: ( 0,  0,  0,  0,  0,  0,  0),   # (space)
    33: ( 4,  4,  4,  4,  0,  4,  0),   # !
    34: (10, 10,  0,  0,  0,  0,  0),   # "
    35: (10, 10, 31, 10, 31, 10, 10),   # #
    36: ( 4, 15, 20, 14,  5, 30,  4),   # $
    37: (24, 25,  2,  4,  8, 19,  3),   # %
    38: (12, 18, 20,  8, 21, 18, 13),   # &
    39: ( 4,  4,  0,  0,  0,  0,  0),   # '
    40: ( 2,  4,  8,  8,  8,  4,  2),   # (
    41: ( 8,  4,  2,  2,  2,  4,  8),   # )
    42: ( 0,  4, 21, 14, 21,  4,  0),   # *
    43: ( 0,  4,  4, 31,  4,  4,  0),   # +
    44: ( 0,  0,  0,  0,  6,  4,  8),   # ,
    45: ( 0,  0,  0, 31,  0,  0,  0),   # -
    46: ( 0,  0,  0,  0,  0,  6,  6),   # .
    47: ( 0,  1,  2,  4,  8, 16,  0),   # /

    # ── ASCII 48–57 (digits) ──────────────────────────────────
    48: (14, 17, 19, 21, 25, 17, 14),   # 0
    49: ( 4, 12,  4,  4,  4,  4, 14),   # 1
    50: (14, 17,  1,  2,  4,  8, 31),   # 2
    51: (14, 17,  1,  6,  1, 17, 14),   # 3
    52: ( 2,  6, 10, 18, 31,  2,  2),   # 4
    53: (31, 16, 30,  1,  1, 17, 14),   # 5
    54: ( 6,  8, 16, 30, 17, 17, 14),   # 6
    55: (31,  1,  2,  4,  8,  8,  8),   # 7
    56: (14, 17, 17, 14, 17, 17, 14),   # 8
    57: (14, 17, 17, 15,  1,  2, 12),   # 9

    # ── ASCII 58–64 (more punctuation) ────────────────────────
    58: ( 0,  6,  6,  0,  6,  6,  0),   # :
    59: ( 0,  6,  6,  0,  6,  4,  8),   # ;
    60: ( 2,  4,  8, 16,  8,  4,  2),   # <
    61: ( 0,  0, 31,  0, 31,  0,  0),   # =
    62: ( 8,  4,  2,  1,  2,  4,  8),   # >
    63: (14, 17,  1,  2,  4,  0,  4),   # ?
    64: (14, 17,  1, 13, 21, 21, 14),   # @

    # ── ASCII 65–90 (uppercase) ───────────────────────────────
    65: (14, 17, 17, 31, 17, 17, 17),   # A
    66: (30, 17, 17, 30, 17, 17, 30),   # B
    67: (14, 17, 16, 16, 16, 17, 14),   # C
    68: (28, 18, 17, 17, 17, 18, 28),   # D
    69: (31, 16, 16, 30, 16, 16, 31),   # E
    70: (31, 16, 16, 30, 16, 16, 16),   # F
    71: (14, 17, 16, 23, 17, 17, 15),   # G
    72: (17, 17, 17, 31, 17, 17, 17),   # H
    73: (14,  4,  4,  4,  4,  4, 14),   # I
    74: ( 3,  1,  1,  1,  1, 17, 14),   # J
    75: (17, 18, 20, 24, 20, 18, 17),   # K
    76: (16, 16, 16, 16, 16, 16, 31),   # L
    77: (17, 27, 21, 17, 17, 17, 17),   # M
    78: (17, 25, 21, 19, 17, 17, 17),   # N
    79: (14, 17, 17, 17, 17, 17, 14),   # O
    80: (30, 17, 17, 30, 16, 16, 16),   # P
    81: (14, 17, 17, 17, 21, 18, 13),   # Q
    82: (30, 17, 17, 30, 20, 18, 17),   # R
    83: (14, 17, 16, 14,  1, 17, 14),   # S
    84: (31,  4,  4,  4,  4,  4,  4),   # T
    85: (17, 17, 17, 17, 17, 17, 14),   # U
    86: (17, 17, 17, 17, 17, 10,  4),   # V
    87: (17, 17, 17, 21, 21, 27, 17),   # W
    88: (17, 17, 10,  4, 10, 17, 17),   # X
    89: (17, 17, 10,  4,  4,  4,  4),   # Y
    90: (31,  1,  2,  4,  8, 16, 31),   # Z

    # ── ASCII 91–96 (brackets, etc.) ──────────────────────────
    91: (14,  8,  8,  8,  8,  8, 14),   # [
    92: ( 0, 16,  8,  4,  2,  1,  0),   # \
    93: (14,  2,  2,  2,  2,  2, 14),   # ]
    94: ( 4, 10, 17,  0,  0,  0,  0),   # ^
    95: ( 0,  0,  0,  0,  0,  0, 31),   # _
    96: ( 8,  4,  0,  0,  0,  0,  0),   # `

    # ── ASCII 97–122 (lowercase) ──────────────────────────────
    97:  ( 0,  0, 14,  1, 15, 17, 15),  # a
    98:  (16, 16, 30, 17, 17, 17, 30),  # b
    99:  ( 0,  0, 14, 16, 16, 17, 14),  # c
    100: ( 1,  1, 15, 17, 17, 17, 15),  # d
    101: ( 0,  0, 14, 17, 31, 16, 14),  # e
    102: ( 6,  8,  8, 28,  8,  8,  8),  # f
    103: ( 0, 15, 17, 17, 15,  1, 14),  # g  (descender)
    104: (16, 16, 22, 25, 17, 17, 17),  # h
    105: ( 4,  0, 12,  4,  4,  4, 14),  # i
    106: ( 2,  0,  2,  2,  2, 18, 12),  # j  (descender)
    107: (16, 16, 18, 20, 24, 20, 18),  # k
    108: (12,  4,  4,  4,  4,  4, 14),  # l
    109: ( 0,  0, 26, 21, 21, 17, 17),  # m
    110: ( 0,  0, 22, 25, 17, 17, 17),  # n
    111: ( 0,  0, 14, 17, 17, 17, 14),  # o
    112: ( 0,  0, 30, 17, 17, 30, 16),  # p  (descender)
    113: ( 0,  0, 15, 17, 17, 15,  1),  # q  (descender)
    114: ( 0,  0, 22, 25, 16, 16, 16),  # r
    115: ( 0,  0, 14, 16, 14,  1, 14),  # s
    116: ( 8,  8, 30,  8,  8,  8,  6),  # t
    117: ( 0,  0, 17, 17, 17, 17, 14),  # u
    118: ( 0,  0, 17, 17, 17, 10,  4),  # v
    119: ( 0,  0, 17, 17, 21, 21, 10),  # w
    120: ( 0,  0, 17, 10,  4, 10, 17),  # x
    121: ( 0, 17, 17, 17, 15,  1, 14),  # y  (descender)
    122: ( 0,  0, 31,  2,  4,  8, 31),  # z

    # ── ASCII 123–126 (remaining) ─────────────────────────────
    123: ( 2,  4,  4,  8,  4,  4,  2),  # {
    124: ( 4,  4,  4,  4,  4,  4,  4),  # |
    125: ( 8,  4,  4,  2,  4,  4,  8),  # }
    126: ( 0,  0,  9, 22,  0,  0,  0),  # ~  tilde
}

# Fallback pattern for characters not in the font: a small box
_FALLBACK = (31, 17, 17, 17, 17, 17, 31)

# ── HUI-2 character translation (maps raw byte → font key) ──
_HUI2_MAP: dict = {
    0x10: 32,   0x11: 32,   0x12: 32,   0x13: 32,
    0x14: 32,   0x15: 32,   0x16: 32,   0x17: 32,
    0x18: 32,                            # plain spaces
    0x19: 0x19,                          # ♪
    0x1A: 0x1A, 0x1B: 0x1B,             # °
    0x1C: 0x1C, 0x1D: 0x1D,             # ▼ ►
    0x1E: 0x1E, 0x1F: 0x1F,             # ◄ ▲
}


# ╔═══════════════════════════════════════════════════════════╗
# ║  COLOUR HELPERS                                           ║
# ╚═══════════════════════════════════════════════════════════╝

def _hex_to_rgb(h: str):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _rgb_to_hex(r, g, b):
    return f'#{int(r):02x}{int(g):02x}{int(b):02x}'

def _dim_color(hex_color: str, factor: float = 0.08) -> str:
    """Return a very dim version of a colour for unlit VFD pixels."""
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex(r * factor, g * factor, b * factor)


# ╔═══════════════════════════════════════════════════════════╗
# ║  CONFIG                                                   ║
# ╚═══════════════════════════════════════════════════════════╝

class Config:
    def __init__(self):
        self._d = dict(_DEFAULTS)
        self._load()

    def __getattr__(self, key):
        if key.startswith('_'):
            raise AttributeError(key)
        if key in _DEFAULTS:
            return self._d.get(key, _DEFAULTS[key])
        raise AttributeError(key)

    def _load(self):
        try:
            with open(_CONFIG_FILE) as f:
                saved = json.load(f)
            for k in _DEFAULTS:
                if k in saved:
                    self._d[k] = saved[k]
        except FileNotFoundError:
            self._save()
        except Exception as e:
            print(f'Warning: could not read config.hui: {e}')

    def _save(self):
        try:
            with open(_CONFIG_FILE, 'w') as f:
                json.dump(self._d, f, indent=2)
        except Exception as e:
            print(f'Warning: could not save config.hui: {e}')

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if k in _DEFAULTS:
                self._d[k] = v
        self._save()


# ╔═══════════════════════════════════════════════════════════╗
# ║  HUI PROTOCOL                                             ║
# ╚═══════════════════════════════════════════════════════════╝

_HUI_HEADER    = bytes([0x00, 0x00, 0x66, 0x05, 0x00])
_CMD_MAIN_DISP = 0x12
_ROWS = 2
_COLS = 40


def _decode_byte(b: int) -> int:
    """Map a raw HUI-2 byte to a FONT_5X7 key."""
    if b in _HUI2_MAP:
        return _HUI2_MAP[b]
    if 0x20 <= b <= 0x7D:
        return b
    return 32   # unmapped → space


class HUIMainDisplay:
    """Thread-safe 2×40 integer buffer of font keys."""

    def __init__(self):
        self.lock           = threading.Lock()
        self._buf           = [32] * (_ROWS * _COLS)   # spaces
        self.sysex_received = 0

    def process_sysex(self, data) -> bool:
        d = bytes(data)
        if len(d) < 6 or d[:5] != _HUI_HEADER or d[5] != _CMD_MAIN_DISP:
            return False
        payload = d[6:]
        with self.lock:
            i = 0
            while i + 10 < len(payload):
                zone  = payload[i]
                chars = payload[i + 1 : i + 11]
                i    += 11
                if zone >= 8:
                    continue
                row = zone // 4
                col = (zone % 4) * 10
                for j, b in enumerate(chars):
                    self._buf[row * _COLS + col + j] = _decode_byte(b)
            self.sysex_received += 1
        return True

    def get_rows(self):
        with self.lock:
            return (
                list(self._buf[:_COLS]),
                list(self._buf[_COLS:]),
            )


# ╔═══════════════════════════════════════════════════════════╗
# ║  MIDI ROUTING                                             ║
# ╚═══════════════════════════════════════════════════════════╝

def _resolve(name: str, available: list) -> str:
    if name in available:
        return name
    matches = [p for p in available if p.startswith(name)]
    return sorted(matches)[0] if matches else name


class MIDIRouter:
    def __init__(self, cfg: Config, display: HUIMainDisplay, on_status):
        self.cfg       = cfg
        self.display   = display
        self.on_status = on_status
        self._stop     = threading.Event()
        self._ports    = []

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        for p in self._ports:
            try: p.close()
            except Exception: pass
        self._ports.clear()

    def _run(self):
        cfg = self.cfg
        try:
            ins  = mido.get_input_names()
            outs = mido.get_output_names()
            pti = mido.open_input(_resolve(cfg.pt_sends_to,  ins))
            huo = mido.open_output(_resolve(cfg.hui_out_port, outs))
            hui = mido.open_input(_resolve(cfg.hui_in_port,  ins))
            pto = mido.open_output(_resolve(cfg.pt_recv_from, outs))
            self._ports = [pti, huo, hui, pto]
        except Exception as e:
            self.on_status(f'MIDI ERROR: {e}', error=True)
            return

        self.on_status(f'Connected  —  routing active')

        def _hw_to_pt():
            try:
                for msg in hui:
                    if self._stop.is_set(): break
                    try: pto.send(msg)
                    except Exception: pass
            except Exception: pass

        threading.Thread(target=_hw_to_pt, daemon=True).start()

        try:
            n = 0
            for msg in pti:
                if self._stop.is_set(): break
                try: huo.send(msg)
                except Exception: pass
                if msg.type == 'sysex':
                    self.display.process_sysex(msg.data)
                n += 1
                if n % 50 == 0:
                    sx = self.display.sysex_received
                    self.on_status(
                        f'Running  —  {n:,} MIDI msgs  —  {sx} display SysEx'
                    )
        except Exception: pass


# ╔═══════════════════════════════════════════════════════════╗
# ║  DOT-MATRIX RENDERER                                      ║
# ╚═══════════════════════════════════════════════════════════╝

class VFDDotMatrix:
    """
    Pre-creates all 2 × 40 × 35 = 2,800 pixel items on a tk.Canvas,
    then updates them in-place by colour change only — no create/delete
    during animation, so redraws are fast.

    Physical dot dimensions are computed from pixel_size:
        dot_w  = round(pixel_size × 0.85)  (narrower than tall — authentic VFD look)
        dot_h  = round(pixel_size × 1.4)   (slightly taller)
        dot_gap = max(1, pixel_size // 4)  (gap between dots within a character)
        char_gap = dot_w × 2               (two full dot-widths between characters)
        row_gap  = dot_h + dot_gap × 3    (extra vertical gap between text rows)
        pad      = max(6, pixel_size × 2) (outer border padding)
    """

    CHAR_ROWS = 2
    CHAR_COLS = 40
    DOT_ROWS  = 7
    DOT_COLS  = 5

    def __init__(self, canvas: tk.Canvas, cfg: Config):
        self.cv  = canvas
        self.cfg = cfg

        ps = max(1, int(cfg.pixel_size))
        if ps == 1:
            # Pixel-perfect: 1 screen pixel per VFD dot, no intra-character gaps
            self.dot_w    = 1
            self.dot_h    = 1
            self.dot_gap  = 0
            self.char_gap = 1
            self.row_gap  = 1
            self.pad      = 1
        else:
            self.dot_w    = max(1, round(ps * 0.75))
            self.dot_h    = max(1, int(self.dot_w * 1.75))  # floor of 1.75× width; dot_h tracks dot_w so both scale together
            self.dot_gap  = max(0, (ps + 1) // 4)
            self.char_gap = max(1, self.dot_w * 2)
            self.row_gap  = self.dot_h + self.dot_gap * 3
            self.pad      = max(4, ps * 2)
        self.rx = max(0, ps // 5)

        self.lit_color = cfg.fg_color
        self.dim_color = _dim_color(cfg.fg_color, 0.07)

        # _items[(char_row, char_col, dot_row, dot_col)] = canvas item id
        self._items: dict = {}
        self._build()

    # ── Layout geometry ───────────────────────────────────────

    def cell_w(self) -> int:
        """Width of one character cell in screen pixels (including inter-char gap)."""
        return self.DOT_COLS * self.dot_w + (self.DOT_COLS - 1) * self.dot_gap + self.char_gap

    def cell_h(self) -> int:
        """Height of one character cell in screen pixels."""
        return self.DOT_ROWS * self.dot_h + (self.DOT_ROWS - 1) * self.dot_gap

    def canvas_width(self) -> int:
        return 2 * self.pad + self.CHAR_COLS * self.cell_w() - self.char_gap

    def canvas_height(self) -> int:
        return 2 * self.pad + self.CHAR_ROWS * self.cell_h() + (self.CHAR_ROWS - 1) * self.row_gap

    def char_origin(self, cr: int, cc: int):
        """Top-left pixel coordinate of a character at (row cr, col cc)."""
        x = self.pad + cc * self.cell_w()
        y = self.pad + cr * (self.cell_h() + self.row_gap)
        return x, y

    # ── Canvas construction (called once) ─────────────────────

    def _build(self):
        self.cv.delete('all')
        for cr in range(self.CHAR_ROWS):
            for cc in range(self.CHAR_COLS):
                ox, oy = self.char_origin(cr, cc)
                for dr in range(self.DOT_ROWS):
                    for dc in range(self.DOT_COLS):
                        x1 = ox + dc * (self.dot_w + self.dot_gap)
                        y1 = oy + dr * (self.dot_h + self.dot_gap)
                        x2 = x1 + self.dot_w
                        y2 = y1 + self.dot_h
                        iid = self.cv.create_rectangle(
                            x1, y1, x2, y2,
                            fill=self.dim_color, outline='',
                        )
                        self._items[(cr, cc, dr, dc)] = iid

    # ── Per-character update ──────────────────────────────────

    def update_char(self, cr: int, cc: int, font_key: int):
        """Update one character cell from a FONT_5X7 lookup key."""
        pattern = FONT_5X7.get(font_key, _FALLBACK)
        for dr in range(self.DOT_ROWS):
            row_bits = pattern[dr]
            for dc in range(self.DOT_COLS):
                bit = (row_bits >> (4 - dc)) & 1
                self.cv.itemconfig(
                    self._items[(cr, cc, dr, dc)],
                    fill=self.lit_color if bit else self.dim_color
                )

    def update_display(self, row0: list, row1: list):
        """Update from two 40-element lists of font keys."""
        for cc, key in enumerate(row0[:self.CHAR_COLS]):
            self.update_char(0, cc, key)
        for cc, key in enumerate(row1[:self.CHAR_COLS]):
            self.update_char(1, cc, key)

    def blank(self):
        """Dim all pixels (display off)."""
        for iid in self._items.values():
            self.cv.itemconfig(iid, fill=self.dim_color)

    @classmethod
    def required_size(cls, pixel_size: int):
        """Return (width, height) in screen pixels for the given pixel_size."""
        ps = max(1, int(pixel_size))
        if ps == 1:
            dot_w, dot_h, dot_gap, cg, rg, pad = 1, 1, 0, 1, 1, 1
        else:
            dot_w   = max(1, round(ps * 0.75))
            dot_h   = max(1, int(dot_w * 1.75))
            dot_gap = max(0, (ps + 1) // 4)
            cg      = max(1, dot_w * 2)
            rg      = dot_h + dot_gap * 3
            pad     = max(4, ps * 2)
        cw      = cls.DOT_COLS * dot_w + (cls.DOT_COLS - 1) * dot_gap + cg
        ch      = cls.DOT_ROWS * dot_h + (cls.DOT_ROWS - 1) * dot_gap
        width   = 2 * pad + cls.CHAR_COLS * cw - cg
        height  = 2 * pad + cls.CHAR_ROWS * ch + (cls.CHAR_ROWS - 1) * rg
        return width, height


# ╔═══════════════════════════════════════════════════════════╗
# ║  MAIN VFD WINDOW                                          ║
# ╚═══════════════════════════════════════════════════════════╝

class VFDWindow:
    """
    Floating on-top window containing the dot-matrix canvas.
    Refreshes at 20 fps from HUIMainDisplay via root.after().
    """

    def __init__(self, cfg: Config, display: HUIMainDisplay):
        self.cfg     = cfg
        self.display = display
        self.router  = None
        self._matrix    = None
        self._text_vars = None   # used in text mode
        self._last_sysex = -1
        self._animating  = True  # blocks _refresh during welcome animation

        # ── Root window ──────────────────────────────────────
        self.root = tk.Tk()
        self.root.title('HUI-Display')
        self.root.configure(bg=cfg.bg_color)
        self.root.resizable(False, False)
        self.root.attributes('-topmost', True)

        self._build_menu()
        self._build_display()
        self._build_status_bar()

        self._schedule_refresh()
        self.root.after(80, self._welcome_anim_start)  # begin after first paint

    # ── Menu ──────────────────────────────────────────────────

    def _build_menu(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        mb = tk.Menu(self.root, bg=bg, fg=fg,
                     activebackground=dim, activeforeground=fg,
                     relief='flat', borderwidth=0)
        self.root.config(menu=mb)

        app = tk.Menu(mb, tearoff=False, bg=bg, fg=fg,
                      activebackground=dim, activeforeground=fg)
        mb.add_cascade(label='Menu', menu=app)
        app.add_command(label='Configure…', command=self._open_config)
        app.add_separator()
        app.add_command(label='Exit', command=self.root.quit)
        mb.add_command(label='About', command=self._open_about)

    # ── Display (dot-matrix or text) ──────────────────────────

    def _build_display(self):
        """Dispatch to dot-matrix canvas or plain-text labels."""
        if self.cfg.display_mode == 'dot_matrix':
            self._build_canvas()
        else:
            self._build_text_display()

    def _build_canvas(self):
        bg = self.cfg.bg_color
        ps = int(self.cfg.pixel_size)
        cw, ch = VFDDotMatrix.required_size(ps)

        outer = tk.Frame(self.root, bg=bg)
        outer.pack(fill='both', expand=True, padx=0, pady=0)

        # Horizontal scrollbar (only relevant for very large pixel_size)
        MAX_W   = self.root.winfo_screenwidth() - 40
        need_sb = cw > MAX_W
        frame_w = min(cw, MAX_W)

        self._cv = tk.Canvas(outer, width=frame_w, height=ch,
                              bg=bg, highlightthickness=0)
        self._cv.pack(fill='x')

        if need_sb:
            sb = tk.Scrollbar(outer, orient='horizontal',
                               command=self._cv.xview, bg=bg)
            sb.pack(fill='x')
            self._cv.configure(xscrollcommand=sb.set,
                                scrollregion=(0, 0, cw, ch))

        self._matrix = VFDDotMatrix(self._cv, self.cfg)
        self._matrix.blank()

    def _build_text_display(self):
        """Plain monospace-text rendering of the 2×40 display."""
        bg = self.cfg.bg_color
        fg = self.cfg.fg_color
        fn = self.cfg.text_font
        fs = max(8, int(self.cfg.font_size))

        outer = tk.Frame(self.root, bg=bg, padx=10, pady=8)
        outer.pack(fill='both', expand=True)

        self._text_vars = [tk.StringVar(value=' ' * 40) for _ in range(2)]
        for v in self._text_vars:
            tk.Label(outer, textvariable=v, font=(fn, fs),
                     bg=bg, fg=fg, anchor='w', width=40).pack(anchor='w')

    def _rebuild_display(self):
        """Destroy and recreate the display area after any appearance change."""
        self._matrix    = None
        self._text_vars = None
        for w in list(self.root.pack_slaves()):
            if w is not self._status_bar:
                w.destroy()
        self._build_display()
        self._status_bar.pack_forget()
        self._status_bar.pack(side='bottom', fill='x')

    # ── Status bar ────────────────────────────────────────────

    def _build_status_bar(self):
        bg, dim = self.cfg.bg_color, self.cfg.dim_color
        self._status_bar = tk.Frame(self.root, bg=bg, pady=2)
        self._status_bar.pack(side='bottom', fill='x')
        tk.Frame(self._status_bar, bg=dim, height=1).pack(fill='x')
        self._status_var = tk.StringVar(value='Not connected.')
        tk.Label(self._status_bar, textvariable=self._status_var,
                 font=('Segoe UI', 9), bg=bg, fg=dim,
                 anchor='w', padx=8).pack(fill='x')

    def set_status(self, msg: str, error: bool = False):
        self.root.after(0, self._status_var.set, msg)

    # ── Welcome animation ─────────────────────────────────────

    VERSION = 'v1.2'
    _TITLE  = 'HUI-Display'

    def _anim_show(self, row1: str, row2: str):
        """Push two 40-char strings to the display (both modes)."""
        r0 = [ord(c) if 32 <= ord(c) <= 126 else 32 for c in row1.ljust(40)[:40]]
        r1 = [ord(c) if 32 <= ord(c) <= 126 else 32 for c in row2.ljust(40)[:40]]
        if self._matrix:
            self._matrix.update_display(r0, r1)
        if self._text_vars:
            self._text_vars[0].set(row1.ljust(40)[:40])
            self._text_vars[1].set(row2.ljust(40)[:40])

    def _welcome_anim_start(self):
        """Phase 1: wipe-in — fill the display zone by zone left to right."""
        self._anim_phase = 0
        self._welcome_anim_wipe()

    def _welcome_anim_wipe(self):
        z = self._anim_phase
        if z > 7:
            self.root.after(120, self._welcome_anim_typewriter_init)
            return
        r1 = ''.join('**********' if i <= z and z < 4
                     else '          ' for i in range(4))
        r2 = ''.join('**********' if i <= (z - 4) and z >= 4
                     else '          ' for i in range(4))
        self._anim_show(r1, r2)
        self._anim_phase += 1
        self.root.after(60, self._welcome_anim_wipe)

    def _welcome_anim_typewriter_init(self):
        """Phase 2: clear display, then type title character by character."""
        self._anim_show(' ' * 40, ' ' * 40)
        self._anim_phase = 0
        self.root.after(80, self._welcome_anim_typewriter_step)

    def _welcome_anim_typewriter_step(self):
        i = self._anim_phase
        title = self._TITLE
        if i > len(title):
            self.root.after(300, self._welcome_anim_show_version)
            return
        # Build row: always left-aligned at the centre start position (col 14)
        partial = title[:i]
        row1 = (' ' * 14 + partial).ljust(40)
        self._anim_show(row1, ' ' * 40)
        self._anim_phase += 1
        self.root.after(80, self._welcome_anim_typewriter_step)

    def _welcome_anim_show_version(self):
        """Phase 3: both title and version centred; hold, then end."""
        self._anim_show(self._TITLE.center(40), self.VERSION.center(40))
        self.root.after(2200, self._welcome_anim_end)

    def _welcome_anim_end(self):
        """Clear display and hand control back to the normal refresh loop."""
        self._anim_show(' ' * 40, ' ' * 40)
        self._animating = False

    # ── Refresh loop (20 fps) ─────────────────────────────────

    def _schedule_refresh(self):
        self._refresh()
        self.root.after(50, self._schedule_refresh)

    def _refresh(self):
        if self._animating:
            return   # welcome animation is running; don't overwrite it
        sx = self.display.sysex_received
        if sx != self._last_sysex:
            self._last_sysex = sx
            r0, r1 = self.display.get_rows()
            if self.cfg.display_mode == 'dot_matrix':
                if self._matrix:
                    self._matrix.update_display(r0, r1)
            else:
                if self._text_vars:
                    for i, row in enumerate([r0, r1]):
                        text = ''.join(chr(k) if 32 <= k <= 126 else ' ' for k in row)
                        self._text_vars[i].set(text)

    # ── MIDI router wiring ────────────────────────────────────

    def start_midi(self):
        self.router = MIDIRouter(self.cfg, self.display, self.set_status)
        self.router.start()

    def stop_midi(self):
        if self.router:
            self.router.stop()
            self.router = None

    def restart_midi(self):
        self.stop_midi()
        self.display.sysex_received = 0
        self._last_sysex            = -1
        if self._matrix:
            self._matrix.blank()
        if self._text_vars:
            for v in self._text_vars:
                v.set(' ' * 40)
        self.root.after(300, self.start_midi)

    # ── Config dialog ─────────────────────────────────────────

    def _open_config(self):
        ConfigDialog(self.root, self.cfg, on_apply=self._on_config_applied)

    def _on_config_applied(self):
        self.stop_midi()
        self._rebuild_display()
        self.root.configure(bg=self.cfg.bg_color)
        self._status_bar.configure(bg=self.cfg.bg_color)
        self.root.after(200, self.start_midi)

    # ── About dialog ──────────────────────────────────────────

    def _open_about(self):
        AboutDialog(self.root, self.cfg)

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


# ╔═══════════════════════════════════════════════════════════╗
# ║  CONFIGURE DIALOG                                         ║
# ╚═══════════════════════════════════════════════════════════╝

class ConfigDialog(tk.Toplevel):

    def __init__(self, parent, cfg: Config, on_apply):
        super().__init__(parent)
        self.cfg      = cfg
        self.on_apply = on_apply

        self._colors = {
            'fg_color':  cfg.fg_color,
            'bg_color':  cfg.bg_color,
            'dim_color': cfg.dim_color,
        }

        bg, fg, dim = cfg.bg_color, cfg.fg_color, cfg.dim_color
        self.title('Configure — HUI VFD Display')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.configure(bg=bg)

        st = ttk.Style(self)
        st.theme_use('default')
        st.configure('D.TNotebook',     background=bg, borderwidth=0, tabmargins=0)
        st.configure('D.TNotebook.Tab', background=dim, foreground=fg,
                     padding=[14, 5], font=('Segoe UI', 9))
        st.map('D.TNotebook.Tab',
               background=[('selected', bg)], foreground=[('selected', fg)])
        st.configure('D.TCombobox', fieldbackground=dim, background=bg,
                     foreground=fg, selectbackground=dim, arrowcolor=fg)
        st.configure('D.TSpinbox', fieldbackground=dim, foreground=fg,
                     background=bg, arrowcolor=fg)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        nb = ttk.Notebook(self, style='D.TNotebook')
        nb.grid(row=0, column=0, padx=12, pady=(12, 4), sticky='nsew')

        self._build_midi_tab(nb, bg, fg, dim)
        self._build_appearance_tab(nb, bg, fg, dim)

        tk.Frame(self, bg=dim, height=1).grid(row=1, column=0, sticky='ew')
        br = tk.Frame(self, bg=bg, padx=12, pady=10)
        br.grid(row=2, column=0, sticky='ew')

        tk.Button(br, text='Cancel', bg=bg, fg=dim,
                  activebackground=dim, activeforeground=fg,
                  font=('Segoe UI', 10), relief='flat', cursor='hand2',
                  command=self.destroy).pack(side='right', padx=(6, 0))
        tk.Button(br, text='Apply & Reconnect', bg=fg, fg=bg,
                  activebackground=fg, activeforeground=bg,
                  font=('Segoe UI', 10, 'bold'), relief='flat', cursor='hand2',
                  command=self._apply).pack(side='right')

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f'+{x}+{y}')

    def _build_midi_tab(self, nb, bg, fg, dim):
        tab = tk.Frame(nb, bg=bg, padx=14, pady=12)
        nb.add(tab, text='  MIDI Ports  ')

        ins  = mido.get_input_names()
        outs = mido.get_output_names()
        self._port_vars   = {}
        self._port_combos = {}

        rows = [
            ('Pro Tools → Script  (input):',     'pt_sends_to',  'in',  self.cfg.pt_sends_to),
            ('Script → HUI hardware  (output):',  'hui_out_port', 'out', self.cfg.hui_out_port),
            ('HUI hardware → Script  (input):',   'hui_in_port',  'in',  self.cfg.hui_in_port),
            ('Script → Pro Tools  (output):',     'pt_recv_from', 'out', self.cfg.pt_recv_from),
        ]
        for i, (label, key, direction, current) in enumerate(rows):
            tk.Label(tab, text=label, bg=bg, fg=dim,
                     font=('Segoe UI', 9), anchor='w').grid(
                row=i*2, column=0, sticky='w', pady=(10 if i else 0, 2))
            var     = tk.StringVar(value=current)
            choices = ins if direction == 'in' else outs
            cb      = ttk.Combobox(tab, textvariable=var, values=choices,
                                   width=38, style='D.TCombobox')
            cb.grid(row=i*2+1, column=0, sticky='ew')
            self._port_vars[key]   = var
            self._port_combos[key] = (cb, direction)

        tk.Button(tab, text='\u21ba  Refresh port list',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=('Segoe UI', 9), relief='flat', cursor='hand2',
                  command=self._refresh_ports).grid(
            row=len(rows)*2, column=0, sticky='w', pady=(12, 0))

    def _build_appearance_tab(self, nb, bg, fg, dim):
        tab = tk.Frame(nb, bg=bg, padx=14, pady=12)
        nb.add(tab, text='  Appearance  ')

        # ── Colours ──────────────────────────────────────────
        colour_rows = [
            ('Lit pixel / text colour:', 'fg_color'),
            ('Background colour:',       'bg_color'),
            ('Labels & borders:',        'dim_color'),
        ]
        self._swatches = {}
        for i, (label, key) in enumerate(colour_rows):
            tk.Label(tab, text=label, bg=bg, fg=dim,
                     font=('Segoe UI', 9), anchor='w',
                     width=24).grid(row=i, column=0, sticky='w', pady=5)
            sw = tk.Label(tab, bg=self._colors[key], width=4,
                          relief='groove', cursor='hand2')
            sw.grid(row=i, column=1, padx=(0, 8), sticky='w')
            sw.bind('<Button-1>', lambda e, k=key: self._pick_colour(k))
            self._swatches[key] = sw
            tk.Button(tab, text='Choose\u2026', bg=bg, fg=fg,
                      activebackground=dim, activeforeground=fg,
                      font=('Segoe UI', 9), relief='flat', cursor='hand2',
                      command=lambda k=key: self._pick_colour(k)).grid(
                row=i, column=2, sticky='w')

        r = len(colour_rows)
        tk.Frame(tab, bg=dim, height=1).grid(
            row=r, column=0, columnspan=3, sticky='ew', pady=(12, 8))

        # ── Display mode ─────────────────────────────────────
        tk.Label(tab, text='Display mode:', bg=bg, fg=dim,
                 font=('Segoe UI', 9), anchor='w').grid(
            row=r+1, column=0, sticky='w')

        self._mode_var = tk.StringVar(value=self.cfg.display_mode)
        mode_f = tk.Frame(tab, bg=bg)
        mode_f.grid(row=r+1, column=1, columnspan=2, sticky='w')
        for val, label in (('dot_matrix', 'Dot-Matrix'), ('text', 'Text')):
            tk.Radiobutton(mode_f, text=label, variable=self._mode_var,
                           value=val, bg=bg, fg=fg, selectcolor=dim,
                           activebackground=bg, font=('Segoe UI', 9),
                           command=self._on_mode_change).pack(side='left', padx=(0, 12))

        # ── Dot-matrix options (pixel size) ──────────────────
        self._dm_frame = tk.Frame(tab, bg=bg)
        self._dm_frame.grid(row=r+2, column=0, columnspan=3, sticky='ew', pady=(8, 0))

        tk.Label(self._dm_frame, text='Pixel size:', bg=bg, fg=dim,
                 font=('Segoe UI', 9), anchor='w').grid(
            row=0, column=0, sticky='w')
        self._pxsz_var = tk.IntVar(value=self.cfg.pixel_size)
        ttk.Spinbox(self._dm_frame, from_=1, to=16, textvariable=self._pxsz_var,
                    width=5, style='D.TSpinbox').grid(row=0, column=1, sticky='w')
        tk.Label(self._dm_frame,
                 text='px   (1 = pixel-perfect  |  5 = default  |  10 = 4K)',
                 bg=bg, fg=dim, font=('Segoe UI', 9)).grid(
            row=0, column=2, sticky='w', padx=(8, 0))

        # ── Text-mode options (font + size) ──────────────────
        self._txt_frame = tk.Frame(tab, bg=bg)
        self._txt_frame.grid(row=r+2, column=0, columnspan=3, sticky='ew', pady=(8, 0))

        tk.Label(self._txt_frame, text='Font:', bg=bg, fg=dim,
                 font=('Segoe UI', 9), anchor='w').grid(
            row=0, column=0, sticky='w')
        self._font_var = tk.StringVar(value=self.cfg.text_font)
        font_cb = ttk.Combobox(self._txt_frame, textvariable=self._font_var,
                               values=_TEXT_FONTS, width=26, style='D.TCombobox')
        font_cb.grid(row=0, column=1, sticky='w', padx=(0, 12))
        tk.Label(self._txt_frame, text='Size:', bg=bg, fg=dim,
                 font=('Segoe UI', 9)).grid(row=0, column=2, sticky='w')
        self._fsz_var = tk.IntVar(value=self.cfg.font_size)
        ttk.Spinbox(self._txt_frame, from_=8, to=36, textvariable=self._fsz_var,
                    width=4, style='D.TSpinbox').grid(
            row=0, column=3, sticky='w', padx=(4, 0))
        tk.Label(self._txt_frame, text='pt', bg=bg, fg=dim,
                 font=('Segoe UI', 9)).grid(row=0, column=4, sticky='w', padx=(4, 0))

        # Bitmap-font note
        tk.Label(self._txt_frame,
                 text='Tip: Terminal and Fixedsys are Windows bitmap fonts — they\n'
                      'render sharpest at specific sizes (e.g. 9 pt). You may also\n'
                      'type any font name not in the list.',
                 bg=bg, fg=dim, font=('Segoe UI', 8), justify='left').grid(
            row=1, column=0, columnspan=5, sticky='w', pady=(6, 0))

        # Apply initial visibility
        self._on_mode_change()

        # ── Divider + Reset ───────────────────────────────────
        tk.Frame(tab, bg=dim, height=1).grid(
            row=r+3, column=0, columnspan=3, sticky='ew', pady=(14, 8))
        tk.Button(tab, text='Reset to defaults',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=('Segoe UI', 9), relief='flat', cursor='hand2',
                  command=self._reset).grid(
            row=r+4, column=0, columnspan=3, sticky='w')

    def _on_mode_change(self):
        is_dm = self._mode_var.get() == 'dot_matrix'
        if is_dm:
            self._txt_frame.grid_remove()
            self._dm_frame.grid()
        else:
            self._dm_frame.grid_remove()
            self._txt_frame.grid()

    def _reset(self):
        for key in ('fg_color', 'bg_color', 'dim_color'):
            self._colors[key] = _DEFAULTS[key]
            self._swatches[key].configure(bg=_DEFAULTS[key])
        self._pxsz_var.set(_DEFAULTS['pixel_size'])
        self._mode_var.set(_DEFAULTS['display_mode'])
        self._font_var.set(_DEFAULTS['text_font'])
        self._fsz_var.set(_DEFAULTS['font_size'])
        self._on_mode_change()
        self._pxsz_var.set(_DEFAULTS['pixel_size'])

    def _refresh_ports(self):
        ins, outs = mido.get_input_names(), mido.get_output_names()
        for key, (cb, d) in self._port_combos.items():
            cb['values'] = ins if d == 'in' else outs

    def _pick_colour(self, key: str):
        result = colorchooser.askcolor(
            color=self._colors[key], title='Choose colour', parent=self)
        if result[1]:
            self._colors[key] = result[1]
            self._swatches[key].configure(bg=result[1])

    def _apply(self):
        self.cfg.update(
            pt_sends_to  = self._port_vars['pt_sends_to'].get(),
            hui_out_port = self._port_vars['hui_out_port'].get(),
            hui_in_port  = self._port_vars['hui_in_port'].get(),
            pt_recv_from = self._port_vars['pt_recv_from'].get(),
            fg_color     = self._colors['fg_color'],
            bg_color     = self._colors['bg_color'],
            dim_color    = self._colors['dim_color'],
            pixel_size   = self._pxsz_var.get(),
            display_mode = self._mode_var.get(),
            text_font    = self._font_var.get(),
            font_size    = self._fsz_var.get(),
        )
        self.on_apply()
        self.destroy()


# ╔═══════════════════════════════════════════════════════════╗
# ║  ABOUT DIALOG                                             ║
# ╚═══════════════════════════════════════════════════════════╝

class AboutDialog(tk.Toplevel):

    _GPL_URL = 'https://www.gnu.org/licenses/gpl-3.0.html#license-text'

    def __init__(self, parent, cfg: Config):
        super().__init__(parent)
        bg, fg, dim = cfg.bg_color, cfg.fg_color, cfg.dim_color

        self.title('About HUI-Display')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.configure(bg=bg)

        outer = tk.Frame(self, bg=bg, padx=28, pady=22)
        outer.pack()

        tk.Label(outer, text='HUI-Display  v1.2',
                 font=('Segoe UI', 22, 'bold'), bg=bg, fg=fg).pack()
        tk.Label(outer, text='part of the HUI Tools suite',
                 font=('Segoe UI', 10), bg=bg, fg=dim).pack(pady=(3, 0))
        tk.Label(outer, text='\u00a9 2026 Richard Philip',
                 font=('Segoe UI', 10), bg=bg, fg=dim).pack(pady=(2, 16))

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(0, 14))

        tk.Label(outer,
                 text='HUI Protocol is Copyright \u00a9 1997\n'
                      'Mackie Designs and Digidesign (Avid)',
                 font=('Segoe UI', 9), bg=bg, fg=dim, justify='center').pack(pady=(0, 12))

        tk.Label(outer, text='This open-source software is licensed under',
                 font=('Segoe UI', 9), bg=bg, fg=dim).pack()
        link = tk.Label(outer, text='GNU General Public License v3.0',
                        font=('Segoe UI', 9, 'underline'),
                        bg=bg, fg=fg, cursor='hand2')
        link.pack()
        link.bind('<Button-1>', lambda e: webbrowser.open(self._GPL_URL))

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(18, 10))
        tk.Button(outer, text='Close', bg=bg, fg=dim,
                  activebackground=dim, activeforeground=fg,
                  font=('Segoe UI', 10), relief='flat', cursor='hand2',
                  command=self.destroy).pack()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f'+{x}+{y}')


# ╔═══════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                              ║
# ╚═══════════════════════════════════════════════════════════╝

def main():
    # Write error log next to the script on unhandled crash
    log_path = os.path.join(_SCRIPT_DIR, 'hui_display_vfd_error.txt')
    try:
        cfg     = Config()
        display = HUIMainDisplay()
        window  = VFDWindow(cfg, display)
        window.start_midi()
        window.run()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            with open(log_path, 'w') as f:
                f.write(tb)
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
