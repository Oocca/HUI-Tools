#!/usr/bin/env python3
"""
╔════════════════════════════════════════════╗
║          HUI-Test  v1.9                    ║
║  Interactive hardware test tool for the    ║
║  Mackie HUI controller.                    ║
║  Part of the HUI Tools suite.              ║
╚════════════════════════════════════════════╝

© 2026 Richard Philip
Released under the GNU General Public License v3.0
https://www.gnu.org/licenses/gpl-3.0.html

Connects DIRECTLY to HUI hardware — do NOT run at the
same time as HUI-Display with Pro Tools connected.

Port settings are shared with HUI-Display via config.hui.

Protocol reference: HUI_MIDI_protocol.pdf (theageman, 2010)
Owner's manual / Service manual: Mackie Designs, 1997-1998
"""

import sys
import os
import json
import threading
import time
import math
import mido
import tkinter as tk
from tkinter import ttk, colorchooser
import webbrowser


# ╔═══════════════════════════════════════════════════════════╗
# ║  CONFIGURATION                                            ║
# ╚═══════════════════════════════════════════════════════════╝

_DIR = os.path.dirname(os.path.abspath(__file__))
_CFG = os.path.join(_DIR, 'config.hui')

# Only the keys HUI-Test cares about.
# Visual defaults match HUI-Display so the tools look consistent.
_DEFAULTS = {
    'hui_out_port':      'HUI',
    'hui_in_port':       'HUI',
    'fg_color':          '#00eed5',
    'bg_color':          '#06100e',
    'dim_color':         '#005549',
    'font_size':         15,
    'pixel_size':        5,
    'skip_test_warning': False,
}


class Config:
    """Loads from config.hui on startup; merges on save (preserving HUI-Display keys)."""

    def __init__(self):
        self._d = dict(_DEFAULTS)
        self._load()

    def __getattr__(self, k):
        if k.startswith('_'):
            raise AttributeError(k)
        return self._d.get(k, _DEFAULTS.get(k))

    def _load(self):
        try:
            with open(_CFG) as f:
                saved = json.load(f)
            for k in _DEFAULTS:
                if k in saved:
                    self._d[k] = saved[k]
        except Exception:
            pass

    def update(self, **kw):
        for k, v in kw.items():
            if k in _DEFAULTS:
                self._d[k] = v
        # Merge with the full config file to preserve HUI-Display keys
        try:
            with open(_CFG) as f:
                full = json.load(f)
        except Exception:
            full = {}
        full.update(self._d)
        try:
            with open(_CFG, 'w') as f:
                json.dump(full, f, indent=2)
        except Exception:
            pass


# ╔═══════════════════════════════════════════════════════════╗
# ║  HUI PROTOCOL — message factory                           ║
# ╚═══════════════════════════════════════════════════════════╝

_HUI_HDR = [0x00, 0x00, 0x66, 0x05, 0x00]


class HUI:
    """Static factory for every outgoing HUI MIDI message."""

    # ── Connection ────────────────────────────────────────────
    @staticmethod
    def ping() -> mido.Message:
        """Active-sensing ping. HUI should reply with 90 00 7F."""
        return mido.Message('note_on', channel=0, note=0, velocity=0)

    # ── Displays ─────────────────────────────────────────────
    @staticmethod
    def vfd(zone: int, text: str) -> mido.Message:
        """Send 10 chars to one zone (0–7) of the 2×40 main VFD."""
        padded = (text + ' ' * 10)[:10]
        chars  = [(ord(c) if 0x20 <= ord(c) <= 0x7D else 0x20) for c in padded]
        return mido.Message('sysex', data=_HUI_HDR + [0x12, zone & 0x07] + chars)

    @staticmethod
    def scribble(ch: int, text: str) -> mido.Message:
        """Send 4 chars to a channel scribble strip (ch 0-8, where 8 = SELECT-ASSIGN)."""
        padded = (text + '    ')[:4]
        chars  = [(ord(c) if 0x20 <= ord(c) <= 0x7F else 0x20) for c in padded]
        return mido.Message('sysex', data=_HUI_HDR + [0x10, ch & 0x0F] + chars)

    @staticmethod
    def timecode(hh: int, mm: int, ss: int, ff: int) -> mido.Message:
        """
        Send timecode to the 8-digit 7-segment display.
        Encoding: y0=rightmost (frames units), y7=leftmost (hours tens).
        Digits y1-y7 get OR'd with 0x10 to display the decimal-point separator.
        """
        def split(n):
            return n % 10, n // 10
        fu, ft = split(max(0, min(99, ff)))
        su, st = split(max(0, min(59, ss)))
        mu, mt = split(max(0, min(59, mm)))
        hu, ht = split(max(0, min(99, hh)))
        data = [fu, ft | 0x10, su, st | 0x10, mu, mt | 0x10, hu, ht | 0x10]
        return mido.Message('sysex', data=_HUI_HDR + [0x11] + data)

    # ── LEDs ──────────────────────────────────────────────────
    @staticmethod
    def led_on(zone: int, port: int) -> list:
        """Turn on LED at zone/port. Returns two CC messages (zone select + port on)."""
        return [
            mido.Message('control_change', channel=0, control=0x0C, value=zone & 0x1F),
            mido.Message('control_change', channel=0, control=0x2C, value=0x40 | (port & 0x07)),
        ]

    @staticmethod
    def led_off(zone: int, port: int) -> list:
        """Turn off LED at zone/port."""
        return [
            mido.Message('control_change', channel=0, control=0x0C, value=zone & 0x1F),
            mido.Message('control_change', channel=0, control=0x2C, value=port & 0x07),
        ]

    @staticmethod
    def led_zone_all(zone: int, on: bool) -> list:
        """Turn all 8 ports in a zone on or off at once."""
        msgs = [mido.Message('control_change', channel=0, control=0x0C, value=zone & 0x1F)]
        for p in range(8):
            val = (0x40 | p) if on else p
            msgs.append(mido.Message('control_change', channel=0, control=0x2C, value=val))
        return msgs

    # ── VU Meters ─────────────────────────────────────────────
    @staticmethod
    def vu(ch: int, side: int, level: int) -> mido.Message:
        """
        Set VU meter level.
          ch:    0-7 (channel strip)
          side:  0=left, 1=right
          level: 0-12 (0=all off, 12=clip/red)
        Uses polyphonic key pressure (A0): A0 0y sv
        """
        return mido.Message('polytouch', channel=0,
                            note=ch & 0x07,
                            value=((side & 0x01) << 4) | (min(12, max(0, level)) & 0x0F))

    # ── V-Pot Rings ───────────────────────────────────────────
    @staticmethod
    def vpot(pot: int, value: int) -> mido.Message:
        """
        Set V-pot LED ring.
          pot:   0-7  = channel V-pots
                 8-11 = SELECT-ASSIGN V-pots
                 12   = SCROLL V-pot
          value: 0x00-0x3F = ring pattern (see below) + 0x40 = centre LED on
          Patterns (high nibble of value):
            0 = single dot (position 0-10)
            1 = pan / spread from centre
            2 = fill from left (level bar)
            3 = expand from centre (both directions)
        """
        return mido.Message('control_change', channel=0,
                            control=0x10 | (pot & 0x0F),
                            value=value & 0x7F)

    # ── Faders ────────────────────────────────────────────────
    @staticmethod
    def fader(zone: int, pos: int) -> list:
        """
        Move motorised fader.
          zone: 0-7
          pos:  0-16383 (14-bit, 0=bottom, 16383=top)
        Returns two CC messages (hi byte then lo byte).
        """
        hi = (pos >> 7) & 0x7F
        lo =  pos       & 0x7F
        return [
            mido.Message('control_change', channel=0, control=zone & 0x07,         value=hi),
            mido.Message('control_change', channel=0, control=0x20 | (zone & 0x07), value=lo),
        ]


# ╔═══════════════════════════════════════════════════════════╗
# ║  HARDWARE LAYOUT TABLES                                   ║
# ║  Source: HUI_MIDI_protocol.pdf, p.15  +  Owner's Manual  ║
# ╚═══════════════════════════════════════════════════════════╝

ZONE_NAMES = {
    0x00: 'Ch 1 Strip',      0x01: 'Ch 2 Strip',      0x02: 'Ch 3 Strip',
    0x03: 'Ch 4 Strip',      0x04: 'Ch 5 Strip',      0x05: 'Ch 6 Strip',
    0x06: 'Ch 7 Strip',      0x07: 'Ch 8 Strip',
    0x08: 'Keyboard Shortcuts',
    0x09: 'Window',
    0x0A: 'Channel Select',
    0x0B: 'Assignment 1',
    0x0C: 'Assignment 2',
    0x0D: 'Cursor / Mode / Scrub',
    0x0E: 'Transport Main',
    0x0F: 'Transport Loop / RTZ',
    0x10: 'Transport Punch',
    0x11: 'Monitor Input',
    0x12: 'Monitor Output',
    0x13: 'Num Pad 1',
    0x14: 'Num Pad 2',
    0x15: 'Num Pad 3',
    0x16: 'Timecode LEDs',
    0x17: 'Auto Enable',
    0x18: 'Auto Mode',
    0x19: 'Status / Group',
    0x1A: 'Edit',
    0x1B: 'Function Keys',
    0x1C: 'Parameter Edit',
    0x1D: 'Click / Beep / Relay / FS',
}

_CH_PORTS = {0:'fader', 1:'select', 2:'mute', 3:'solo', 4:'auto', 5:'v-sel', 6:'insert', 7:'rec/rdy'}

PORT_NAMES = {
    **{z: dict(_CH_PORTS) for z in range(8)},
    0x08: {0:'ctrl/clt', 1:'shift/add', 2:'edit mode', 3:'undo',     4:'alt/fine', 5:'opt/all',   6:'edit tool', 7:'save'},
    0x09: {0:'mix',      1:'edit',      2:'transport', 3:'mem-loc',  4:'status',   5:'alt'},
    0x0A: {0:'<- chnl',  1:'<- bank',  2:'chnl ->',   3:'bank ->'},
    0x0B: {0:'output',   1:'input',     2:'pan',       3:'send E',   4:'send D',   5:'send C',    6:'send B',    7:'send A'},
    0x0C: {0:'assign',   1:'default',   2:'suspend',   3:'shift',    4:'mute',     5:'bypass',    6:'rec all'},
    0x0D: {0:'down',     1:'left',      2:'mode',      3:'right',    4:'up',       5:'scrub',     6:'shuttle'},
    0x0E: {0:'talkback', 1:'rewind',    2:'fast fwd',  3:'stop',     4:'play',     5:'record'},
    0x0F: {0:'|< rtz',   1:'end >|',   2:'on line',   3:'loop',     4:'qck pnch'},
    0x10: {0:'audition', 1:'pre',       2:'in',        3:'out',      4:'post'},
    0x11: {0:'input 3',  1:'input 2',  2:'input 1',   3:'mute',     4:'discrete'},
    0x12: {0:'output 3', 1:'output 2', 2:'output 1',  3:'dim',      4:'mono'},
    0x13: {0:'0',        1:'1',         2:'4',         3:'2',        4:'5',        5:'.',         6:'3',         7:'6'},
    0x14: {0:'enter',    1:'+'},
    0x15: {0:'7',        1:'8',         2:'9',         3:'-',        4:'clr',      5:'=',         6:'/',         7:'*'},
    0x16: {0:'timecode', 1:'feet',      2:'beat',      3:'rude solo'},
    0x17: {0:'plug-in',  1:'pan',       2:'fader',     3:'snd mute', 4:'send',     5:'mute'},
    0x18: {0:'trim',     1:'latch',     2:'read',      3:'off',      4:'write',    5:'touch'},
    0x19: {0:'phase',    1:'monitor',   2:'auto',      3:'suspend',  4:'create',   5:'group'},
    0x1A: {0:'paste',    1:'cut',       2:'capture',   3:'delete',   4:'copy',     5:'separate'},
    0x1B: {0:'F1',       1:'F2',        2:'F3',        3:'F4',       4:'F5',       5:'F6',        6:'F7',        7:'F8/ESC'},
    0x1C: {0:'ins/para', 1:'assign',    2:'select 1',  3:'select 2', 4:'select 3', 5:'select 4',  6:'bypass',    7:'compare'},
    0x1D: {0:'relay 1',  1:'relay 2',  2:'click',     3:'beep'},
}


# ╔═══════════════════════════════════════════════════════════╗
# ║  MIDI CONNECTION                                          ║
# ╚═══════════════════════════════════════════════════════════╝

def _resolve(name: str, available: list) -> str:
    if name in available:
        return name
    matches = [p for p in available if p.startswith(name)]
    return sorted(matches)[0] if matches else name


class MIDIConn:
    """Direct connection to HUI hardware (output + input)."""

    def __init__(self, cfg: Config, on_msg, on_status):
        self.cfg       = cfg
        self.on_msg    = on_msg      # callback(str)
        self.on_status = on_status   # callback(text, error=False)
        self._out      = None
        self._in       = None
        self._stop     = threading.Event()
        self._ping_timer = None

    def connect(self) -> bool:
        try:
            outs = mido.get_output_names()
            ins  = mido.get_input_names()
            out_name = _resolve(self.cfg.hui_out_port, outs)
            in_name  = _resolve(self.cfg.hui_in_port,  ins)
            self._out = mido.open_output(out_name)
            self._in  = mido.open_input(in_name)
        except Exception as e:
            self.on_status(f'Error: {e}', error=True)
            return False
        self.on_status(f'Connected  ·  Out: {out_name}  ·  In: {in_name}')
        self._stop.clear()
        threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    def disconnect(self):
        self._stop_autopng()
        self._stop.set()
        for p in (self._out, self._in):
            if p:
                try:
                    p.close()
                except Exception:
                    pass
        self._out = self._in = None
        self.on_status('Disconnected')

    def send(self, msgs) -> None:
        if not self._out:
            return
        if isinstance(msgs, mido.Message):
            msgs = [msgs]
        for m in msgs:
            try:
                self._out.send(m)
            except Exception:
                pass

    def start_autopng(self, interval: float = 1.0):
        """Send a ping every `interval` seconds to keep HUI online."""
        self._stop_autopng()
        self._ping_interval = interval

        def _do():
            if self._out and not self._stop.is_set():
                self.send(HUI.ping())
                self._ping_timer = threading.Timer(self._ping_interval, _do)
                self._ping_timer.daemon = True
                self._ping_timer.start()

        self._ping_timer = threading.Timer(interval, _do)
        self._ping_timer.daemon = True
        self._ping_timer.start()

    def _stop_autopng(self):
        if self._ping_timer:
            self._ping_timer.cancel()
            self._ping_timer = None

    def _read_loop(self):
        try:
            for msg in self._in:
                if self._stop.is_set():
                    break
                self.on_msg(msg)
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._out is not None


# ╔═══════════════════════════════════════════════════════════╗
# ║  LED PANEL FILTERS                                        ║
# ╠═══════════════════════════════════════════════════════════╣
# ║  Not everything in PORT_NAMES is an LED.  These filters   ║
# ║  control what the LED test panel shows.                   ║
# ╚═══════════════════════════════════════════════════════════╝

# Zones with NO LEDs at all, or handled elsewhere (Audio tab).
# These are hidden from the LED zone list entirely.
_LED_EXCLUDED = {
    0x13,   # Num Pad 1  — no LEDs on keypad buttons
    0x14,   # Num Pad 2  — no LEDs
    0x15,   # Num Pad 3  — no LEDs
    0x1D,   # Relay / Click / Beep — not LEDs; tested in Audio tab
}

# For zones where only SOME ports have LEDs.
# Keys absent from this dict means "use all PORT_NAMES entries".
_LED_ONLY_PORTS: dict = {
    # Channel zones 0x00–0x07: port 0 is fader touch — no LED.
    # Only ports 1–7 have physical LEDs (select, mute, solo, auto, v-sel, insert, rec/rdy).
    **{z: {1:'select', 2:'mute', 3:'solo', 4:'auto', 5:'v-sel', 6:'insert', 7:'rec/rdy'}
       for z in range(8)},
    # Zone 0x0D: mode, scrub, and shuttle have LEDs.
    # Down, left, right, and up are plain buttons with no LED.
    0x0D: {2: 'mode', 5: 'scrub', 6: 'shuttle'},
}


# ╔═══════════════════════════════════════════════════════════╗
# ║  VFD STATE — tracks outgoing display data                 ║
# ╚═══════════════════════════════════════════════════════════╝

class VFDState:
    """
    Mirrors the current content of the 2×40 VFD.
    Updated whenever HUI-Test sends a VFD zone, so the
    'View VFD Display' window always shows live data.
    """
    ROWS = 2
    COLS = 40

    def __init__(self):
        self._lock = threading.Lock()
        self._buf  = [[' '] * self.COLS for _ in range(self.ROWS)]

    def update_zone(self, zone: int, text: str) -> None:
        row = zone // 4
        col = (zone %  4) * 10
        padded = (text + ' ' * 10)[:10]
        with self._lock:
            for i, c in enumerate(padded):
                self._buf[row][col + i] = c

    def clear(self) -> None:
        with self._lock:
            self._buf = [[' '] * self.COLS for _ in range(self.ROWS)]

    def get_rows(self) -> tuple:
        with self._lock:
            return ''.join(self._buf[0]), ''.join(self._buf[1])


# ╔═══════════════════════════════════════════════════════════╗
# ║  HUI STATE — parses incoming hardware MIDI                ║


# ╔═══════════════════════════════════════════════════════════╗
# ║  DOT-MATRIX VFD RENDERER  (shared with HUI Display)      ║
# ╚═══════════════════════════════════════════════════════════╝

def _hex_to_rgb(h: str):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _rgb_to_hex(r, g, b):
    return f'#{int(r):02x}{int(g):02x}{int(b):02x}'

def _dim_color(hex_color: str, factor: float = 0.08) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex(r * factor, g * factor, b * factor)


# 5×7 bitmap font — each entry is a 7-tuple of 5-bit row patterns (bit4=left, bit0=right)
FONT_5X7 = {
    0x19: ( 3,  5,  4,  4, 12, 12,  0),  # ♪
    0x1A: (12, 18, 12,  0,  0,  0,  0),  # °
    0x1B: (12, 18, 12,  0,  0,  0,  0),  # °
    0x1C: (31, 14,  4,  0,  0,  0,  0),  # ▼
    0x1D: (16, 24, 30, 31, 30, 24, 16),  # ►
    0x1E: ( 1,  3, 15, 31, 15,  3,  1),  # ◄
    0x1F: ( 0,  0,  0,  4, 14, 31,  0),  # ▲
    32: ( 0, 0, 0, 0, 0, 0, 0),
    33: ( 4, 4, 4, 4, 0, 4, 0),
    34: (10,10, 0, 0, 0, 0, 0),
    35: (10,10,31,10,31,10,10),
    36: ( 4,15,20,14, 5,30, 4),
    37: (24,25, 2, 4, 8,19, 3),
    38: (12,18,20, 8,21,18,13),
    39: ( 4, 4, 0, 0, 0, 0, 0),
    40: ( 2, 4, 8, 8, 8, 4, 2),
    41: ( 8, 4, 2, 2, 2, 4, 8),
    42: ( 0, 4,21,14,21, 4, 0),
    43: ( 0, 4, 4,31, 4, 4, 0),
    44: ( 0, 0, 0, 0, 6, 4, 8),
    45: ( 0, 0, 0,31, 0, 0, 0),
    46: ( 0, 0, 0, 0, 0, 6, 6),
    47: ( 0, 1, 2, 4, 8,16, 0),
    48: (14,17,19,21,25,17,14),
    49: ( 4,12, 4, 4, 4, 4,14),
    50: (14,17, 1, 2, 4, 8,31),
    51: (14,17, 1, 6, 1,17,14),
    52: ( 2, 6,10,18,31, 2, 2),
    53: (31,16,30, 1, 1,17,14),
    54: ( 6, 8,16,30,17,17,14),
    55: (31, 1, 2, 4, 8, 8, 8),
    56: (14,17,17,14,17,17,14),
    57: (14,17,17,15, 1, 2,12),
    58: ( 0, 6, 6, 0, 6, 6, 0),
    59: ( 0, 6, 6, 0, 6, 4, 8),
    60: ( 2, 4, 8,16, 8, 4, 2),
    61: ( 0, 0,31, 0,31, 0, 0),
    62: ( 8, 4, 2, 1, 2, 4, 8),
    63: (14,17, 1, 2, 4, 0, 4),
    64: (14,17, 1,13,21,21,14),
    65: (14,17,17,31,17,17,17),
    66: (30,17,17,30,17,17,30),
    67: (14,17,16,16,16,17,14),
    68: (28,18,17,17,17,18,28),
    69: (31,16,16,30,16,16,31),
    70: (31,16,16,30,16,16,16),
    71: (14,17,16,23,17,17,15),
    72: (17,17,17,31,17,17,17),
    73: (14, 4, 4, 4, 4, 4,14),
    74: ( 3, 1, 1, 1, 1,17,14),
    75: (17,18,20,24,20,18,17),
    76: (16,16,16,16,16,16,31),
    77: (17,27,21,17,17,17,17),
    78: (17,25,21,19,17,17,17),
    79: (14,17,17,17,17,17,14),
    80: (30,17,17,30,16,16,16),
    81: (14,17,17,17,21,18,13),
    82: (30,17,17,30,20,18,17),
    83: (14,17,16,14, 1,17,14),
    84: (31, 4, 4, 4, 4, 4, 4),
    85: (17,17,17,17,17,17,14),
    86: (17,17,17,17,17,10, 4),
    87: (17,17,17,21,21,27,17),
    88: (17,17,10, 4,10,17,17),
    89: (17,17,10, 4, 4, 4, 4),
    90: (31, 1, 2, 4, 8,16,31),
    91: (14, 8, 8, 8, 8, 8,14),
    92: ( 0,16, 8, 4, 2, 1, 0),
    93: (14, 2, 2, 2, 2, 2,14),
    94: ( 4,10,17, 0, 0, 0, 0),
    95: ( 0, 0, 0, 0, 0, 0,31),
    96: ( 8, 4, 0, 0, 0, 0, 0),
    97:  ( 0, 0,14, 1,15,17,15),
    98:  (16,16,30,17,17,17,30),
    99:  ( 0, 0,14,16,16,17,14),
    100: ( 1, 1,15,17,17,17,15),
    101: ( 0, 0,14,17,31,16,14),
    102: ( 6, 8, 8,28, 8, 8, 8),
    103: ( 0,15,17,17,15, 1,14),
    104: (16,16,22,25,17,17,17),
    105: ( 4, 0,12, 4, 4, 4,14),
    106: ( 2, 0, 2, 2, 2,18,12),
    107: (16,16,18,20,24,20,18),
    108: (12, 4, 4, 4, 4, 4,14),
    109: ( 0, 0,26,21,21,17,17),
    110: ( 0, 0,22,25,17,17,17),
    111: ( 0, 0,14,17,17,17,14),
    112: ( 0, 0,30,17,17,30,16),
    113: ( 0, 0,15,17,17,15, 1),
    114: ( 0, 0,22,25,16,16,16),
    115: ( 0, 0,14,16,14, 1,14),
    116: ( 8, 8,30, 8, 8, 8, 6),
    117: ( 0, 0,17,17,17,17,14),
    118: ( 0, 0,17,17,17,10, 4),
    119: ( 0, 0,17,17,21,21,10),
    120: ( 0, 0,17,10, 4,10,17),
    121: ( 0,17,17,17,15, 1,14),
    122: ( 0, 0,31, 2, 4, 8,31),
    123: ( 2, 4, 4, 8, 4, 4, 2),
    124: ( 4, 4, 4, 4, 4, 4, 4),
    125: ( 8, 4, 4, 2, 4, 4, 8),
    126: ( 0, 0, 9,22, 0, 0, 0),
}
_FALLBACK_GLYPH = (31, 17, 17, 17, 17, 17, 31)


class VFDDotMatrix:
    """
    Pre-creates all 2×40×35 = 2,800 pixel rectangles on a tk.Canvas and
    updates them by colour change only — fast for 20-fps refresh.
    Pixel geometry is derived from a single pixel_size integer (1–16).
    """
    CHAR_ROWS = 2
    CHAR_COLS = 40
    DOT_ROWS  = 7
    DOT_COLS  = 5

    def __init__(self, canvas, pixel_size: int, fg_color: str, bg_color: str):
        self.cv = canvas
        ps = max(1, int(pixel_size))
        if ps == 1:
            self.dot_w = self.dot_h = 1
            self.dot_gap = 0
            self.char_gap = 1
            self.row_gap = 1
            self.pad = 1
        else:
            self.dot_w    = max(1, round(ps * 0.75))
            self.dot_h    = max(1, int(self.dot_w * 1.75))
            self.dot_gap  = max(0, (ps + 1) // 4)
            self.char_gap = max(1, self.dot_w * 2)
            self.row_gap  = self.dot_h + self.dot_gap * 3
            self.pad      = max(4, ps * 2)
        self.rx        = max(0, ps // 5)
        self.lit_color = fg_color
        self.dim_color = _dim_color(fg_color, 0.07)
        self._items: dict = {}
        self._build()

    def cell_w(self):
        return self.DOT_COLS * self.dot_w + (self.DOT_COLS - 1) * self.dot_gap + self.char_gap

    def cell_h(self):
        return self.DOT_ROWS * self.dot_h + (self.DOT_ROWS - 1) * self.dot_gap

    def canvas_width(self):
        return 2 * self.pad + self.CHAR_COLS * self.cell_w() - self.char_gap

    def canvas_height(self):
        return 2 * self.pad + self.CHAR_ROWS * self.cell_h() + (self.CHAR_ROWS - 1) * self.row_gap

    def char_origin(self, cr, cc):
        x = self.pad + cc * self.cell_w()
        y = self.pad + cr * (self.cell_h() + self.row_gap)
        return x, y

    def _build(self):
        self.cv.delete('all')
        for cr in range(self.CHAR_ROWS):
            for cc in range(self.CHAR_COLS):
                ox, oy = self.char_origin(cr, cc)
                for dr in range(self.DOT_ROWS):
                    for dc in range(self.DOT_COLS):
                        x1 = ox + dc * (self.dot_w + self.dot_gap)
                        y1 = oy + dr * (self.dot_h + self.dot_gap)
                        x2, y2 = x1 + self.dot_w, y1 + self.dot_h
                        iid = self.cv.create_rectangle(
                            x1, y1, x2, y2, fill=self.dim_color, outline='')
                        self._items[(cr, cc, dr, dc)] = iid

    def update_char(self, cr, cc, font_key):
        pattern = FONT_5X7.get(font_key, _FALLBACK_GLYPH)
        for dr in range(self.DOT_ROWS):
            row_bits = pattern[dr]
            for dc in range(self.DOT_COLS):
                bit = (row_bits >> (4 - dc)) & 1
                self.cv.itemconfig(
                    self._items[(cr, cc, dr, dc)],
                    fill=self.lit_color if bit else self.dim_color)

    def update_display(self, row0, row1):
        """Accept two 40-element sequences of font keys (int) or char strings."""
        for cc, key in enumerate(row0[:self.CHAR_COLS]):
            self.update_char(0, cc, ord(key) if isinstance(key, str) else key)
        for cc, key in enumerate(row1[:self.CHAR_COLS]):
            self.update_char(1, cc, ord(key) if isinstance(key, str) else key)

    def blank(self):
        for iid in self._items.values():
            self.cv.itemconfig(iid, fill=self.dim_color)

    @classmethod
    def required_size(cls, pixel_size):
        ps = max(1, int(pixel_size))
        if ps == 1:
            dw, dh, dg, cg, rg, pad = 1, 1, 0, 1, 1, 1
        else:
            dw  = max(1, round(ps * 0.75))
            dh  = max(1, int(dw * 1.75))
            dg  = max(0, (ps + 1) // 4)
            cg  = max(1, dw * 2)
            rg  = dh + dg * 3
            pad = max(4, ps * 2)
        cw = cls.DOT_COLS * dw + (cls.DOT_COLS - 1) * dg + cg
        ch = cls.DOT_ROWS * dh + (cls.DOT_ROWS - 1) * dg
        return (2 * pad + cls.CHAR_COLS * cw - cg,
                2 * pad + cls.CHAR_ROWS * ch + (cls.CHAR_ROWS - 1) * rg)

# ╚═══════════════════════════════════════════════════════════╝

class HUIState:
    """
    Parses every MIDI message received from the HUI hardware,
    handles multi-message sequences, and maintains a live mirror
    of the surface state for the Live View graphical display.

    Multi-message sequences handled:
      Fader position : ctrl 0-7 (hi byte) + ctrl 0x20-0x27 (lo byte)
      Button/touch   : zone-select ctrl 0x0F, then port-state ctrl 0x2C or 0x2F
      V-pot delta    : ctrl 0x40-0x4C  (encodes direction + speed)
      Jog wheel      : ctrl 0x0D       (same delta encoding as V-pots)

    Note on receive vs transmit control numbers (from spec):
      Zone select  transmit=0x0C  receive=0x0F
      Port off     transmit=0x2C  receive=0x2F   (port on: 0x2C both ways)
    """

    N_CH  = 8
    N_POT = 13

    def __init__(self):
        self.lock        = threading.Lock()
        self.fader_pos   = [0]     * self.N_CH
        self.fader_touch = [False] * self.N_CH
        self._fader_hi   = [0]     * self.N_CH
        self.vpot_acc    = [500]   * self.N_POT   # 0-1000, neutral = 500
        self.buttons     = {}                       # {(zone, port): True}
        self._zone       = None                     # pending zone for port decode
        self.jog_total   = 0
        self.ping_flag   = False                    # set on reply, cleared after read
        self.log         = []                       # list of (decoded_str, raw_str)

    def reset(self):
        with self.lock:
            self.fader_pos   = [0]     * self.N_CH
            self.fader_touch = [False] * self.N_CH
            self._fader_hi   = [0]     * self.N_CH
            self.vpot_acc    = [500]   * self.N_POT
            self.buttons     = {}
            self._zone       = None
            self.jog_total   = 0
            self.ping_flag   = False
            self.log         = []

    def process(self, msg: mido.Message):
        """Parse msg; update state; return (decoded|None, raw_str)."""
        raw     = str(msg)
        decoded = self._parse(msg)
        if decoded is not None:
            with self.lock:
                self.log.append((decoded, raw))
                if len(self.log) > 600:
                    self.log.pop(0)
        return decoded, raw

    def _parse(self, msg):
        # ── Ping reply: note_on ch=0 note=0 vel=127 ───────────
        if (msg.type == 'note_on' and msg.channel == 0
                and msg.note == 0 and msg.velocity == 0x7F):
            with self.lock:
                self.ping_flag = True
            return 'Ping reply  \u2192  HUI online'

        if msg.type != 'control_change' or msg.channel != 0:
            return f'[other]  {msg}'

        ctrl, val = msg.control, msg.value

        # ── Fader hi byte  (ctrl 0-7) ─────────────────────────
        if 0 <= ctrl <= 7:
            self._fader_hi[ctrl] = val
            return None   # wait for lo byte

        # ── Fader lo byte  (ctrl 0x20-0x27) ───────────────────
        if 0x20 <= ctrl <= 0x27:
            ch  = ctrl & 0x07
            pos = (self._fader_hi[ch] << 7) | val
            pct = round(pos * 100 / 16383)
            with self.lock:
                self.fader_pos[ch] = pos
            return f'Ch {ch+1}  Fader  \u2192  {pct}%  [{pos}/16383]'

        # ── Zone select  (receive: ctrl 0x0F) ─────────────────
        if ctrl == 0x0F:
            self._zone = val
            return None   # wait for port state

        # ── Port state  (receive: ctrl 0x2C or 0x2F) ──────────
        if ctrl in (0x2C, 0x2F):
            if self._zone is None:
                return f'[port, no zone]  ctrl={ctrl:02X} val={val:02X}'
            zone  = self._zone
            port  = val & 0x07
            is_on = (val & 0x40) != 0
            with self.lock:
                if is_on:
                    self.buttons[(zone, port)] = True
                else:
                    self.buttons.pop((zone, port), None)
                if zone <= 7 and port == 0:
                    self.fader_touch[zone] = is_on

            if zone <= 7 and port == 0:
                return f'Ch {zone+1}  Fader  \u2192  {"touched" if is_on else "released"}'

            z = ZONE_NAMES.get(zone,  f'Zone {zone:02X}')
            p = PORT_NAMES.get(zone, {}).get(port, f'Port {port}')
            a = '\u25bc pressed' if is_on else '\u25b2 released'
            return f'{z}  \u00b7  {p}  \u2192  {a}'

        # ── V-pot delta  (ctrl 0x40-0x4C = pots 0-12) ─────────
        if 0x40 <= ctrl <= 0x4C:
            pot   = ctrl - 0x40
            delta = (val - 0x40) if val > 0x40 else -val
            with self.lock:
                self.vpot_acc[pot] = max(0, min(1000, self.vpot_acc[pot] + delta * 10))
            name = (f'Ch {pot+1} V-Pot' if pot < 8
                    else f'Assign Pot {pot-7}' if pot < 12
                    else 'Scroll Pot')
            d = f'CW \xd7{delta}' if delta > 0 else f'CCW \xd7{-delta}'
            return f'{name}  \u2192  {d}'

        # ── Jog wheel  (ctrl 0x0D) ─────────────────────────────
        if ctrl == 0x0D:
            delta = (val - 0x40) if val > 0x40 else -val
            with self.lock:
                self.jog_total += delta
            d = f'CW \xd7{delta}' if delta > 0 else f'CCW \xd7{-delta}'
            return f'Jog Wheel  \u2192  {d}  (total: {self.jog_total})'

        return f'[unknown CC]  ctrl={ctrl:02X}  val={val:02X}'



class HUITestApp:

    # Font shortcuts
    F9  = ('Segoe UI',  9)
    F10 = ('Segoe UI', 10)
    F10B= ('Segoe UI', 10, 'bold')
    F11 = ('Segoe UI', 11, 'bold')
    F13 = ('Segoe UI', 13, 'bold')

    def __init__(self, cfg: Config):
        self.cfg  = cfg
        self.midi = MIDIConn(cfg, self._on_rx, self._set_status)

        # LED state: {zone: {port: bool}}
        self.led_state    = {z: {p: False for p in range(8)} for z in range(0x1E)}
        self._relay1_on   = False
        self._relay2_on   = False
        self._beep_on     = False
        self._selected_zone = 0x00

        # VFD display state (tracks what we send outward)
        self._vfd_state = VFDState()
        self._vfd_win   = None   # floating VFD Display window, if open

        # HUI incoming state  (tracks what the hardware sends back)
        self._hui_state     = HUIState()
        self._live_log_shown = 0   # how many log entries already rendered

        # Demo / animation flags (one flag per independent animation engine)
        self._led_demo_running   = False
        self._scrib_cycle_running = False
        self._vfd_cycle_running  = False
        self._vegas_running      = False

        # Connection ping tracking
        self._ping_received    = False   # True once a ping reply arrives
        self._ping_timeout_id  = None    # root.after ID for the 5-second check

        # Test Wizard state
        self._wiz_running  = False
        self._wiz_step_idx = 0
        self._wiz_results  = []   # list of (label, passed: bool|None)

        # Fader demo state
        self._demo_running = False
        self._demo_thread  = None

        self.root = tk.Tk()
        self.root.title('HUI-Test')
        self.root.configure(bg=cfg.bg_color)
        self.root.minsize(860, 600)

        self._build_menu()
        self._build_status_bar()
        self._build_connect_bar()
        self._build_notebook()

        # Show startup warning after mainloop begins (100 ms delay)
        self.root.after(100, self._show_warning)

    # ── Menu ──────────────────────────────────────────────────
    def _build_menu(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        mb = tk.Menu(self.root, bg=bg, fg=fg, activebackground=dim, activeforeground=fg,
                     relief='flat', borderwidth=0)
        self.root.config(menu=mb)
        app = tk.Menu(mb, tearoff=0, bg=bg, fg=fg, activebackground=dim, activeforeground=fg)
        mb.add_cascade(label='Menu', menu=app)
        app.add_command(label='Configure\u2026', command=self._open_config)
        app.add_separator()
        app.add_command(label='View VFD Display', command=self._open_vfd_display)
        app.add_separator()
        app.add_command(label='Exit', command=self.root.quit)
        mb.add_command(label='About', command=self._open_about)

    # ── Status bar ────────────────────────────────────────────
    def _build_status_bar(self):
        bg, dim = self.cfg.bg_color, self.cfg.dim_color
        bar = tk.Frame(self.root, bg=bg, pady=3)
        bar.pack(side='bottom', fill='x')
        tk.Frame(bar, bg=dim, height=1).pack(fill='x')
        self.status_var = tk.StringVar(value='Not connected.')
        self.status_lbl = tk.Label(bar, textvariable=self.status_var,
                                   font=self.F9, bg=bg, fg=dim, anchor='w', padx=8)
        self.status_lbl.pack(fill='x')

    # ── Connect bar ───────────────────────────────────────────
    def _build_connect_bar(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        bar = tk.Frame(self.root, bg=bg, padx=8, pady=6)
        bar.pack(side='bottom', fill='x')
        tk.Frame(bar, bg=dim, height=1).pack(fill='x', pady=(0, 6))

        self._conn_btn = tk.Button(bar, text='  \u25b6  Connect  ',
                                    bg=fg, fg=bg, font=self.F10B,
                                    relief='flat', cursor='hand2',
                                    command=self._toggle_connect)
        self._conn_btn.pack(side='left')

        self._autopng_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text='Auto-ping (keeps HUI online)',
                       variable=self._autopng_var, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg,
                       font=self.F9, command=self._autopng_changed).pack(side='left', padx=12)

        self._port_lbl = tk.Label(bar,
            text=f'Out: {self.cfg.hui_out_port}   In: {self.cfg.hui_in_port}',
            font=self.F9, bg=bg, fg=dim)
        self._port_lbl.pack(side='right', padx=8)

    # ── Notebook ──────────────────────────────────────────────
    def _build_notebook(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        st = ttk.Style(self.root)
        st.theme_use('default')
        st.configure('T.TNotebook', background=bg, borderwidth=0, tabmargins=0)
        st.configure('T.TNotebook.Tab', background=dim, foreground=fg,
                     padding=[12, 4], font=self.F9)
        st.map('T.TNotebook.Tab',
               background=[('selected', bg)],
               foreground=[('selected', fg)])

        nb = ttk.Notebook(self.root, style='T.TNotebook')
        nb.pack(fill='both', expand=True, padx=4, pady=4)

        self._tab_connection(nb)
        self._tab_live_view(nb)     # ← graphical hardware monitor
        self._tab_display(nb)
        self._tab_leds(nb)
        self._tab_meters_vpots(nb)
        self._tab_faders(nb)
        self._tab_audio(nb)
        self._tab_goodies(nb)
        self._tab_wizard(nb)

    # ════════════════════════════════════════════════════════
    # TAB 1 — CONNECTION
    # ════════════════════════════════════════════════════════
    def _tab_connection(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Connection  ')

        # Ping row
        ping_f = self._lframe(tab, 'Ping  (90 00 00)')
        ping_f.pack(fill='x', pady=(0, 8))
        pr = tk.Frame(ping_f, bg=bg, padx=8, pady=6)
        pr.pack(fill='x')
        self._btn(pr, 'Send Ping', self._send_ping).pack(side='left')
        tk.Label(pr, text='Last reply:', font=self.F9, bg=bg, fg=dim).pack(side='left', padx=(14, 4))
        self._ping_var = tk.StringVar(value='—')
        tk.Label(pr, textvariable=self._ping_var, font=self.F11, bg=bg, fg=fg).pack(side='left')

        # MIDI log
        log_f = self._lframe(tab, 'MIDI Receive Log')
        log_f.pack(fill='both', expand=True)
        ctrl = tk.Frame(log_f, bg=bg, padx=6, pady=3)
        ctrl.pack(fill='x')
        self._btn(ctrl, 'Clear', self._clear_log).pack(side='right')
        tk.Label(ctrl, text='Incoming messages from HUI hardware (button presses, fader moves, etc.)',
                 font=self.F9, bg=bg, fg=dim).pack(side='left')

        inner = tk.Frame(log_f, bg=bg)
        inner.pack(fill='both', expand=True, padx=6, pady=(0, 6))
        self._log = tk.Text(inner, bg=bg, fg=fg, font=self.F9, height=18,
                            state='disabled', insertbackground=fg,
                            highlightbackground=dim, highlightthickness=1)
        sb = tk.Scrollbar(inner, command=self._log.yview, bg=dim, troughcolor=bg)
        self._log.configure(yscrollcommand=sb.set)
        self._log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

    # ════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════
    # TAB 2 — LIVE VIEW  (fills window; scales on resize; all zones)
    # ════════════════════════════════════════════════════════
    def _tab_live_view(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg)
        nb.add(tab, text='  Live View  ')

        # Canvas fills the tab and rebuilds its contents on resize
        cv_wrap = tk.Frame(tab, bg=bg)
        cv_wrap.pack(fill='both', expand=True, padx=2, pady=2)
        self._live_cv = tk.Canvas(cv_wrap, bg=bg, highlightthickness=0)
        self._live_cv.pack(fill='both', expand=True)
        self._lv_L = {}   # layout geometry dict, set during _build_live_canvas
        self._live_cv.bind('<Configure>',
            lambda e: self.root.after(20, self._build_live_canvas))

        # Log controls
        tk.Frame(tab, bg=dim, height=1).pack(fill='x', padx=4)
        ctrl_row = tk.Frame(tab, bg=bg, padx=8, pady=4)
        ctrl_row.pack(fill='x')
        self._show_decoded = tk.BooleanVar(value=True)
        self._show_raw     = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl_row, text='Decoded log',
                       variable=self._show_decoded, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg, font=self.F9,
                       command=self._toggle_live_log).pack(side='left')
        tk.Checkbutton(ctrl_row, text='Show raw MIDI',
                       variable=self._show_raw, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg, font=self.F9,
                       command=self._rebuild_live_log).pack(side='left', padx=12)
        self._btn(ctrl_row, 'Clear', self._clear_live_log).pack(side='right')

        self._lv_log_frame = tk.Frame(tab, bg=bg)
        self._lv_log_frame.pack(fill='both', expand=False)
        lv_inner = tk.Frame(self._lv_log_frame, bg=bg)
        lv_inner.pack(fill='both', expand=True, padx=6, pady=(2, 6))
        self._lv_log = tk.Text(lv_inner, bg=bg, fg=fg, font=self.F9,
                                height=6, state='disabled',
                                highlightbackground=dim, highlightthickness=1)
        self._lv_log.tag_configure('raw', foreground=dim)
        sb = tk.Scrollbar(lv_inner, command=self._lv_log.yview, bg=dim, troughcolor=bg)
        self._lv_log.configure(yscrollcommand=sb.set)
        self._lv_log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        # Start refresh loop only after all widgets exist
        self._schedule_live_refresh()

    # ── Canvas construction (rebuilt on every resize) ─────────

    def _build_live_canvas(self):
        cv  = self._live_cv
        CW  = cv.winfo_width()
        CH  = cv.winfo_height()
        if CW < 80 or CH < 80:
            return
        cv.delete('all')
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        FS  = max(5, int(CW / 190))   # base font, scales with width

        # Section widths (proportional — sum to CW)
        LS_W = max(52,  int(CW * 0.065))       # left control strip
        CH_W = max(50,  int(CW * 0.52 / 8))    # per channel strip
        TR_W = max(110, int(CW * 0.185))        # transport + jog
        RP_W = max(0,   CW - LS_W - 8*CH_W - TR_W)  # right panel

        X_LS = 0
        X_CH = LS_W
        X_TR = X_CH + 8 * CH_W
        X_RP = X_TR + TR_W

        # Jog geometry (computed here, stored for use in refresh)
        JCX = X_TR + TR_W // 2
        JCY = int(CH * 0.730)
        JR  = max(8, int(min(CH * 0.110, TR_W * 0.155)))

        self._lv_L = dict(LS_W=LS_W, CH_W=CH_W, TR_W=TR_W, RP_W=RP_W,
                          X_CH=X_CH, X_TR=X_TR, X_RP=X_RP,
                          CW=CW, CH=CH, FS=FS,
                          jog_cx=JCX, jog_cy=JCY, jog_r=JR)

        # Section dividers
        for x in [X_CH, X_TR, X_RP]:
            if 0 < x < CW:
                cv.create_line(x, 0, x, CH, fill=dim, width=1)

        self._draw_lv_left_strip(cv, X_LS, LS_W, CH, FS)
        for ch in range(8):
            self._draw_lv_channel(cv, X_CH + ch * CH_W, CH_W, CH, FS, ch)
        self._draw_lv_transport(cv, X_TR, TR_W, CH, FS, JCX, JCY, JR)
        if RP_W > 40:
            self._draw_lv_right_panel(cv, X_RP, RP_W, CH, FS)

    def _lv_btn(self, cv, zone, port, label, x1, y1, x2, y2, fs):
        """Draw one button rectangle + text, both tagged for state updates."""
        dim, bg = self.cfg.dim_color, self.cfg.bg_color
        tag  = f'z{zone:02x}_{port}'
        ttag = tag + 't'
        cv.create_rectangle(x1, y1, x2, y2,
            fill=bg, outline=dim, width=1, tags=tag)
        cv.create_text((x1+x2)/2, (y1+y2)/2, text=label,
            fill=dim, font=('Segoe UI', max(4, fs)),
            anchor='center', tags=ttag)

    def _draw_lv_left_strip(self, cv, x0, w, h, fs):
        """
        Physical HUI left control strip (left of channel strips), top to bottom:
        REC ALL, BYPASS | SEND A-E, PAN, MUTE, SHIFT |
        SUSPEND, DEFAULT | OUTPUT, INPUT, ASSIGN | ←CH, ←BK, CH→, BK→
        """
        items = [
            (0x0C, 6, 'REC\nALL'), (0x0C, 5, 'BYPS'),
            None,
            (0x0B, 7, 'SND A'), (0x0B, 6, 'SND B'), (0x0B, 5, 'SND C'),
            (0x0B, 4, 'SND D'), (0x0B, 3, 'SND E'), (0x0B, 2, 'PAN'),
            (0x0C, 4, 'MUTE'), (0x0C, 3, 'SHFT'),
            None,
            (0x0C, 2, 'SUSP'), (0x0C, 1, 'DFLT'),
            None,
            (0x0B, 0, 'OUT'), (0x0B, 1, 'IN'), (0x0C, 0, 'ASGN'),
            None,
            (0x0A, 0, '\u2190CH'), (0x0A, 1, '\u2190BK'),
            (0x0A, 2, 'CH\u2192'), (0x0A, 3, 'BK\u2192'),
        ]
        n_btns = sum(1 for i in items if i is not None)
        n_seps = items.count(None)
        unit_h = h / (n_btns + n_seps * 0.4 + 0.5)
        pad    = max(1, int(w * 0.04))
        y      = unit_h * 0.25
        for item in items:
            if item is None:
                y += unit_h * 0.4
            else:
                zone, port, lbl = item
                self._lv_btn(cv, zone, port, lbl,
                    x0+pad, y, x0+w-pad, y+unit_h*0.88, max(4, fs-1))
                y += unit_h

    def _draw_lv_channel(self, cv, x0, w, h, fs, ch):
        """
        Single channel strip.  From top to bottom, this mirrors the physical
        HUI channel strip: label, V-pot ring, then buttons REC/RDY, INSERT,
        V-SEL, AUTO, SOLO, MUTE, SELECT, and finally the motorised fader.
        """
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        cx = x0 + w / 2

        # Proportional Y landmarks
        Y_LBL   = h * 0.014
        Y_POT_C = h * 0.105
        POT_R   = min(w * 0.30, h * 0.065)
        DOT_R   = max(2, int(POT_R * 0.16))
        Y_LED0  = h * 0.215
        LED_H   = h * 0.064
        LED_G   = h * 0.005
        Y_FAD_T = h * 0.50
        Y_FAD_B = h * 0.955
        FW      = w * 0.28
        FX1, FX2 = cx - FW/2, cx + FW/2
        pad     = max(1, int(w * 0.04))

        # Label
        cv.create_text(x0 + w*0.27, Y_LBL + h*0.012,
            text=f'CH{ch+1}', fill=dim,
            font=('Segoe UI', max(5, fs), 'bold'), anchor='w',
            tags=f'c{ch}_lbl')

        # Fader touch indicator (small circle, top-right)
        TR = max(3, int(w * 0.07))
        TX = x0 + w - pad - TR
        TY = Y_LBL + h * 0.006
        cv.create_oval(TX-TR, TY, TX+TR, TY+TR*2,
            fill=bg, outline=dim, width=1, tags=f'c{ch}_touch')

        # V-pot ring: 11 dots in a 270° arc, 8-o'clock → top → 4-o'clock
        for i in range(11):
            angle = math.radians(225 - i * 27)
            px = cx + POT_R * math.cos(angle)
            py = Y_POT_C - POT_R * math.sin(angle)   # screen y flipped
            cv.create_oval(px-DOT_R, py-DOT_R, px+DOT_R, py+DOT_R,
                fill=dim, outline='', tags=f'c{ch}_pd{i}')
        cv.create_oval(cx-DOT_R, Y_POT_C-DOT_R, cx+DOT_R, Y_POT_C+DOT_R,
            fill=dim, outline='')   # centre marker (static)

        # LED buttons — 4 rows, matching physical channel strip top-to-bottom
        led_rows = [
            [(7, 'REC'), (6, 'INS')],
            [(5, 'VSL'), (4, 'AUT')],
            [(3, 'SOL'), (2, 'MUT')],
            [(1, 'SEL', True)],         # full-width row
        ]
        for ri, row in enumerate(led_rows):
            yt = Y_LED0 + ri * (LED_H + LED_G)
            yb = yt + LED_H
            if len(row) == 1 and len(row[0]) == 3:
                port, lbl, _ = row[0]
                self._lv_btn(cv, ch, port, lbl, x0+pad, yt, x0+w-pad, yb, max(4, fs-1))
            else:
                hw = (w - 2*pad - max(1, int(pad*0.4))) / 2
                for bi, (port, lbl) in enumerate(row):
                    bx1 = x0 + pad + bi * (hw + max(1, int(pad*0.4)))
                    self._lv_btn(cv, ch, port, lbl, bx1, yt, bx1+hw, yb, max(4, fs-1))

        # Fader track (background) + fill rect + percentage label
        cv.create_rectangle(FX1, Y_FAD_T, FX2, Y_FAD_B, fill=dim, outline=dim)
        cv.create_rectangle(FX1, Y_FAD_B, FX2, Y_FAD_B,
            fill=fg, outline='', tags=f'c{ch}_ffill')
        cv.create_text(cx, Y_FAD_B + (h - Y_FAD_B)*0.4,
            text='0%', fill=dim,
            font=('Segoe UI', max(4, fs-1)), tags=f'c{ch}_fpct')

    def _draw_lv_transport(self, cv, x0, w, h, fs, JCX, JCY, JR):
        """
        Transport button rows (matching physical HUI transport section), then
        jog wheel needle indicator and ping flash.
        """
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        pad = max(2, int(w * 0.025))

        # 5 button rows filling top 56% of height
        rows = [
            [(0x0E,1,'RWND'),(0x0E,2,'FFWD'),(0x0E,3,'STOP'),(0x0E,4,'PLAY'),(0x0E,5,'REC')],
            [(0x0F,0,'RTZ'), (0x0F,1,'END'), (0x0F,2,'ONLN'),(0x0F,3,'LOOP'),(0x0F,4,'QPNC')],
            [(0x10,0,'AUD'), (0x10,1,'PRE'), (0x10,2,'IN'),  (0x10,3,'OUT'), (0x10,4,'POST')],
            [(0x0E,0,'TLKBK'),(0x0D,2,'MODE'),(0x0D,5,'SCRB'),(0x0D,6,'SHTL')],
            [(0x0D,4,'\u25b2'),(0x0D,1,'\u25c0'),(0x0D,0,'\u25bc'),(0x0D,3,'\u25b6')],
        ]
        BTN_AREA = h * 0.56
        ROW_H    = BTN_AREA / len(rows)
        GAP      = ROW_H * 0.12
        BTN_H    = ROW_H - GAP

        for ri, row in enumerate(rows):
            yt = ri * ROW_H + GAP * 0.5
            yb = yt + BTN_H
            aw = w - 2*pad
            bw = aw / len(row)
            for ci, (zone, port, lbl) in enumerate(row):
                bx1 = x0 + pad + ci*bw + pad*0.1
                bx2 = bx1 + bw - pad*0.2
                self._lv_btn(cv, zone, port, lbl, bx1, yt, bx2, yb, fs)

        # Jog wheel
        lw = max(1, int(JR / 9))
        cv.create_text(JCX, JCY - JR - max(5, int(h*0.025)),
            text='JOG WHEEL', fill=dim,
            font=('Segoe UI', max(4, fs-1), 'bold'), anchor='center')
        cv.create_oval(JCX-JR, JCY-JR, JCX+JR, JCY+JR,
            outline=dim, fill=bg, width=lw)
        cv.create_line(JCX, JCY, JCX, JCY-JR+lw,
            fill=dim, width=lw, tags='jog_line')
        cv.create_text(JCX, JCY + JR + max(5, int(h*0.02)),
            text='Total: 0', fill=dim,
            font=('Segoe UI', max(4, fs-1)), tags='jog_txt')

        # Ping indicator
        PY = int(h * 0.920)
        PR = max(5, int(h * 0.022))
        cv.create_text(JCX, PY - PR - max(4, int(h*0.015)),
            text='PING', fill=dim,
            font=('Segoe UI', max(4, fs-1), 'bold'), anchor='center')
        cv.create_oval(JCX-PR*2, PY-PR, JCX+PR*2, PY+PR,
            fill=bg, outline=dim, width=1, tags='ping_dot')
        cv.create_text(JCX, PY + PR + max(4, int(h*0.015)),
            text='\u2014', fill=dim,
            font=('Segoe UI', max(4, fs-1)), tags='ping_txt')

    def _draw_lv_right_panel(self, cv, x0, w, h, fs):
        """
        Right panel: all remaining zones as labelled button groups, arranged
        top-to-bottom to match the physical HUI switch matrix section.
        """
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        pad = max(1, int(w * 0.025))

        # Groups in physical-HUI order: keyboard shortcuts → auto → status → edit
        groups = [
            ('KB SHORTCUTS', 0x08,
             {0:'CTL',1:'SHF',2:'EMD',3:'UND',4:'ALT',5:'OPT',6:'ETL',7:'SAV'}),
            ('WINDOW',       0x09,
             {0:'MIX',1:'EDT',2:'TRN',3:'MLC',4:'STA',5:'ALT'}),
            ('DSP PARAM',    0x1C,
             {0:'I/P',1:'ASG',2:'SL1',3:'SL2',4:'SL3',5:'SL4',6:'BYP',7:'CMP'}),
            ('AUTO ENABLE',  0x17,
             {0:'PLG',1:'PAN',2:'FAD',3:'SMT',4:'SND',5:'MUT'}),
            ('AUTO MODE',    0x18,
             {0:'TRM',1:'LTC',2:'RED',3:'OFF',4:'WRT',5:'TCH'}),
            ('STATUS/GRP',   0x19,
             {0:'PHS',1:'MON',2:'AUT',3:'SUS',4:'CRE',5:'GRP'}),
            ('EDIT',         0x1A,
             {0:'PST',1:'CUT',2:'CAP',3:'DEL',4:'CPY',5:'SEP'}),
            ('FUNC KEYS',    0x1B,
             {0:'F1',1:'F2',2:'F3',3:'F4',4:'F5',5:'F6',6:'F7',7:'F8'}),
            ('MON IN',       0x11,
             {0:'IN3',1:'IN2',2:'IN1',3:'MUT',4:'DSC'}),
            ('MON OUT',      0x12,
             {0:'OT3',1:'OT2',2:'OT1',3:'DIM',4:'MNO'}),
            ('TC LEDs',      0x16,
             {0:'TC',1:'FT',2:'BT',3:'SOLO'}),
            ('NUM PAD',      0x13,
             {0:'0',1:'1',2:'4',3:'2',4:'5',5:'.',6:'3',7:'6'}),
            ('NUM +/ENT',    0x14,
             {0:'ENT',1:'+'}),
            ('NUM PAD',      0x15,
             {0:'7',1:'8',2:'9',3:'-',4:'CLR',5:'=',6:'/',7:'*'}),
        ]
        n      = len(groups)
        grp_h  = h / n
        for gi, (name, zone, ports) in enumerate(groups):
            gy0  = gi * grp_h
            # Group label
            cv.create_text(x0 + w/2, gy0 + grp_h*0.18,
                text=name, fill=dim,
                font=('Segoe UI', max(4, fs-1), 'bold'), anchor='center')
            # Button row
            nb   = len(ports)
            aw   = w - 2*pad
            bw   = aw / nb
            gap  = max(1, bw * 0.05)
            by1  = gy0 + grp_h * 0.36
            by2  = gy0 + grp_h * 0.95
            for bi, (port, lbl) in enumerate(sorted(ports.items())):
                bx1 = x0 + pad + bi*bw + gap/2
                bx2 = bx1 + bw - gap
                self._lv_btn(cv, zone, port, lbl, bx1, by1, bx2, by2, max(4, fs-2))

    # ── Canvas refresh loop (runs at 20 fps) ──────────────────

    def _schedule_live_refresh(self):
        self._refresh_live_view()
        self.root.after(50, self._schedule_live_refresh)

    def _refresh_live_view(self):
        """Update all tagged canvas items from the current HUIState snapshot."""
        cv  = self._live_cv
        L   = self._lv_L
        s   = self._hui_state
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color

        with s.lock:
            fp = list(s.fader_pos)
            ft = list(s.fader_touch)
            va = list(s.vpot_acc)
            bt = dict(s.buttons)
            jt = s.jog_total
            pf = s.ping_flag
            if pf and not self._wiz_running:
                s.ping_flag = False

        # ── All zone/port buttons (update by tag; missing tags are silently skipped)
        ALL_ZONES = list(range(8)) + [
            0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
            0x0E, 0x0F, 0x10, 0x11, 0x12,
            0x13, 0x14, 0x15,
            0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x16,
        ]
        for zone in ALL_ZONES:
            for port in PORT_NAMES.get(zone, {}):
                tag  = f'z{zone:02x}_{port}'
                ttag = tag + 't'
                if not cv.find_withtag(tag):
                    continue
                pressed = bt.get((zone, port), False)
                cv.itemconfig(tag,  fill=fg if pressed else bg,
                                    outline=fg if pressed else dim)
                cv.itemconfig(ttag, fill=bg if pressed else dim)

        # ── Channel strip specials (need layout geometry)
        if not L:
            self._update_live_log_incremental()
            return

        CH_W   = L['CH_W'];  X_CH = L['X_CH'];  CH = L['CH']
        Y_FAD_T = CH * 0.50;  Y_FAD_B = CH * 0.955;  FH = Y_FAD_B - Y_FAD_T

        for ch in range(8):
            x0  = X_CH + ch * CH_W
            cx  = x0 + CH_W / 2
            FW  = CH_W * 0.28
            FX1, FX2 = cx - FW/2, cx + FW/2

            # Touch indicator
            ttag = f'c{ch}_touch'
            if cv.find_withtag(ttag):
                cv.itemconfig(ttag,
                    fill='#ff5555' if ft[ch] else bg,
                    outline='#ff5555' if ft[ch] else dim)

            # V-pot ring (single bright dot at accumulated position)
            active = round(va[ch] / 100)   # 0-10
            for i in range(11):
                dtag = f'c{ch}_pd{i}'
                if cv.find_withtag(dtag):
                    cv.itemconfig(dtag, fill=fg if i == active else dim)

            # Fader fill + percentage
            pos    = fp[ch]
            fill_h = pos * FH / 16383
            ftag   = f'c{ch}_ffill'
            if cv.find_withtag(ftag):
                cv.coords(ftag, FX1, Y_FAD_B - fill_h, FX2, Y_FAD_B)
            ptag = f'c{ch}_fpct'
            if cv.find_withtag(ptag):
                pct = round(pos * 100 / 16383)
                cv.itemconfig(ptag, text=f'{pct}%', fill=fg if pct > 0 else dim)

        # ── Jog needle (36 clicks = one full rotation)
        JCX = L['jog_cx'];  JCY = L['jog_cy'];  JR = L['jog_r']
        if cv.find_withtag('jog_line'):
            angle = math.radians(-90 + (jt * 10) % 360)
            ex = JCX + JR * math.cos(angle)
            ey = JCY + JR * math.sin(angle)
            cv.coords('jog_line', JCX, JCY, ex, ey)
            cv.itemconfig('jog_line', fill=fg if jt != 0 else dim)
        if cv.find_withtag('jog_txt'):
            cv.itemconfig('jog_txt', text=f'Total: {jt}')

        # ── Ping flash
        if pf and cv.find_withtag('ping_dot'):
            cv.itemconfig('ping_dot', fill=fg, outline=fg)
            cv.itemconfig('ping_txt', text='ONLINE', fill=fg)
            self.root.after(400, self._dim_ping)

        self._update_live_log_incremental()

    def _dim_ping(self):
        cv = self._live_cv
        bg, dim = self.cfg.bg_color, self.cfg.dim_color
        if cv.find_withtag('ping_dot'):
            cv.itemconfig('ping_dot', fill=bg,   outline=dim)
            cv.itemconfig('ping_txt', text='\u2014', fill=dim)

    # ── Decoded log ───────────────────────────────────────────

    def _toggle_live_log(self):
        if self._show_decoded.get():
            self._lv_log_frame.pack(fill='both', expand=False)
        else:
            self._lv_log_frame.pack_forget()

    def _rebuild_live_log(self):
        """Full rebuild of displayed log (called when raw-MIDI toggle changes)."""
        with self._hui_state.lock:
            log = list(self._hui_state.log)
        show_raw = self._show_raw.get()
        self._lv_log.configure(state='normal')
        self._lv_log.delete('1.0', 'end')
        for decoded, raw in log:
            self._lv_log.insert('end', decoded + '\n')
            if show_raw:
                self._lv_log.insert('end', f'    \u2514\u2500 {raw}\n', 'raw')
        self._lv_log.see('end')
        self._lv_log.configure(state='disabled')
        self._live_log_shown = len(log)

    def _update_live_log_incremental(self):
        """Append only entries added since last call; handles log wrap-around."""
        if not self._show_decoded.get():
            return
        with self._hui_state.lock:
            log = list(self._hui_state.log)

        # If the log has shrunk (entries popped from front), reset display
        if self._live_log_shown > len(log):
            self._live_log_shown = 0
            self._lv_log.configure(state='normal')
            self._lv_log.delete('1.0', 'end')
            self._lv_log.configure(state='disabled')

        new = log[self._live_log_shown:]
        if not new:
            return

        show_raw = self._show_raw.get()
        self._lv_log.configure(state='normal')
        for decoded, raw in new:
            self._lv_log.insert('end', decoded + '\n')
            if show_raw:
                self._lv_log.insert('end', f'    \u2514\u2500 {raw}\n', 'raw')
        lines = int(self._lv_log.index('end').split('.')[0])
        if lines > 400:
            self._lv_log.delete('1.0', f'{lines - 400}.0')
        self._lv_log.see('end')
        self._lv_log.configure(state='disabled')
        self._live_log_shown = len(log)

    def _clear_live_log(self):
        self._lv_log.configure(state='normal')
        self._lv_log.delete('1.0', 'end')
        self._lv_log.configure(state='disabled')
        self._live_log_shown = len(self._hui_state.log)

        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg)
        nb.add(tab, text='  Live View  ')

        # ── Graphical canvas ──────────────────────────────────
        canvas_wrap = tk.Frame(tab, bg=bg)
        canvas_wrap.pack(fill='x', padx=4, pady=4)

        self._live_cv = tk.Canvas(canvas_wrap,
                                   width=760, height=314,
                                   bg=bg, highlightthickness=0)
        self._live_cv.pack()
        self._lv_items = {}          # tag → canvas item id
        self._build_live_canvas()
        # NOTE: _schedule_live_refresh() is called at the END of this method,
        # after self._show_decoded and self._lv_log are created.

        # ── Log controls ──────────────────────────────────────
        tk.Frame(tab, bg=dim, height=1).pack(fill='x', padx=4)

        ctrl_row = tk.Frame(tab, bg=bg, padx=8, pady=4)
        ctrl_row.pack(fill='x')

        self._show_decoded = tk.BooleanVar(value=True)
        self._show_raw     = tk.BooleanVar(value=False)

        tk.Checkbutton(ctrl_row, text='Decoded log',
                       variable=self._show_decoded, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg, font=self.F9,
                       command=self._toggle_live_log).pack(side='left')

        tk.Checkbutton(ctrl_row, text='Show raw MIDI',
                       variable=self._show_raw, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg, font=self.F9,
                       command=self._rebuild_live_log).pack(side='left', padx=12)

        self._btn(ctrl_row, 'Clear', self._clear_live_log).pack(side='right')

        # ── Log text area (collapsible) ───────────────────────
        self._lv_log_frame = tk.Frame(tab, bg=bg)
        self._lv_log_frame.pack(fill='both', expand=True)

        lv_inner = tk.Frame(self._lv_log_frame, bg=bg)
        lv_inner.pack(fill='both', expand=True, padx=6, pady=(2, 6))

        self._lv_log = tk.Text(lv_inner, bg=bg, fg=fg, font=self.F9,
                                height=7, state='disabled',
                                highlightbackground=dim, highlightthickness=1)
        self._lv_log.tag_configure('raw', foreground=dim)
        self._lv_log.tag_configure('sep', foreground=dim)

        sb = tk.Scrollbar(lv_inner, command=self._lv_log.yview, bg=dim, troughcolor=bg)
        self._lv_log.configure(yscrollcommand=sb.set)
        self._lv_log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        # All widgets now exist — safe to start the refresh loop
        self._schedule_live_refresh()

    # ════════════════════════════════════════════════════════
    # TAB 3 — DISPLAY
    # ════════════════════════════════════════════════════════
    def _tab_display(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Display  ')

        # ── 2×40 Main VFD ──────────────────────────────────
        vfd_f = self._lframe(tab, '2\u00d740 Main VFD  (SysEx 0x12)')
        vfd_f.pack(fill='x', pady=(0, 8))
        vi = tk.Frame(vfd_f, bg=bg, padx=8, pady=6)
        vi.pack(fill='x')
        vi.grid_columnconfigure(1, weight=1)

        self._vfd1 = self._display_entry(vi, 'Row 1:', 0, 40)
        self._vfd2 = self._display_entry(vi, 'Row 2:', 1, 40)

        vb = tk.Frame(vfd_f, bg=bg, padx=8)
        vb.pack(fill='x', pady=(0, 6))
        self._btn(vb, 'Send to VFD',        self._send_vfd).pack(side='left', padx=(0, 6))
        self._btn(vb, 'Clear VFD',           self._clear_vfd).pack(side='left', padx=(0, 6))
        self._btn(vb, 'Test pattern 1-2-3-…', self._vfd_test_pattern).pack(side='left', padx=(0, 6))
        self._btn(vb, 'Fill with \u2588',    self._vfd_fill_blocks).pack(side='left')

        # ── Scribble Strips ────────────────────────────────
        sc_f = self._lframe(tab, 'Channel Scribble Strips — 4 chars each  (SysEx 0x10)')
        sc_f.pack(fill='x', pady=(0, 8))
        si = tk.Frame(sc_f, bg=bg, padx=8, pady=6)
        si.pack()

        self._scribble_vars = []
        labels = [f'Ch {i+1}' for i in range(8)] + ['Sel/Asgn']
        vcmd4 = (self.root.register(lambda P: len(P) <= 4), '%P')
        for i, lbl in enumerate(labels):
            col = tk.Frame(si, bg=bg)
            col.grid(row=0, column=i, padx=3)
            tk.Label(col, text=lbl, font=self.F9, bg=bg, fg=dim).pack()
            v = tk.StringVar(value='')
            tk.Entry(col, textvariable=v, width=5,
                     bg=dim, fg=fg, font=('Courier New', 10, 'bold'),
                     insertbackground=fg,
                     validate='key', validatecommand=vcmd4).pack()
            self._scribble_vars.append(v)

        sb2 = tk.Frame(sc_f, bg=bg, padx=8)
        sb2.pack(fill='x', pady=(0, 6))
        self._btn(sb2, 'Send All',    self._send_scribbles).pack(side='left', padx=(0, 6))
        self._btn(sb2, 'Clear All',   self._clear_scribbles).pack(side='left', padx=(0, 6))
        self._btn(sb2, 'Ch 1…8 / Sel', self._scribble_test).pack(side='left')

        # ── Timecode ───────────────────────────────────────
        tc_f = self._lframe(tab, 'Timecode Display — HH:MM:SS:FF  (SysEx 0x11)')
        tc_f.pack(fill='x')
        ti = tk.Frame(tc_f, bg=bg, padx=8, pady=8)
        ti.pack(fill='x')
        self._tc_var = tk.StringVar(value='01:00:00:00')
        tk.Entry(ti, textvariable=self._tc_var, width=12,
                 bg=dim, fg=fg, font=('Courier New', 16, 'bold'),
                 insertbackground=fg).pack(side='left', padx=(0, 8))
        self._btn(ti, 'Send', self._send_timecode).pack(side='left', padx=(0, 6))
        self._btn(ti, 'Clear (zeros)', lambda: [self._tc_var.set('00:00:00:00'), self._send_timecode()]).pack(side='left')
        tk.Label(ti, text='  Note: separators \':\' are entered in display by the decimal-point bits.',
                 font=self.F9, bg=bg, fg=dim).pack(side='left', padx=12)

        # ── Timecode Counter ───────────────────────────────
        tc_cnt = tk.Frame(tc_f, bg=bg, padx=8, pady=0)
        tc_cnt.pack(fill='x', pady=(0, 8))
        self._tc_running = False
        self._tc_fps_var = tk.StringVar(value='25')
        self._tc_count_btn = tk.Button(tc_cnt,
            text='\u25b6  Start Counter',
            bg=dim, fg=fg, activebackground=dim, activeforeground=fg,
            font=self.F9, relief='flat', cursor='hand2', padx=6, pady=2,
            command=self._toggle_tc_counter)
        self._tc_count_btn.pack(side='left', padx=(0, 10))
        tk.Label(tc_cnt, text='FPS:', font=self.F9, bg=bg, fg=dim).pack(side='left', padx=(0, 4))
        tk.Entry(tc_cnt, textvariable=self._tc_fps_var, width=5,
                 bg=dim, fg=fg, font=self.F9,
                 insertbackground=fg).pack(side='left')
        tk.Label(tc_cnt, text='  Counts forward from the HH:MM:SS:FF value above.',
                 font=self.F9, bg=bg, fg=dim).pack(side='left', padx=10)

        # ── Character Cycling ──────────────────────────────
        cy_f = self._lframe(tab, 'Character Cycling — cycles all printable chars until stopped')
        cy_f.pack(fill='x', pady=(8, 0))
        cy_i = tk.Frame(cy_f, bg=bg, padx=10, pady=8)
        cy_i.pack(fill='x')

        # Scribble cycle
        self._scrib_cycle_btn = tk.Button(cy_i,
            text='\u25b6  Cycle Scribble Strips',
            bg=dim, fg=fg, activebackground=dim, activeforeground=fg,
            font=self.F9, relief='flat', cursor='hand2', padx=6, pady=2,
            command=self._toggle_scrib_cycle)
        self._scrib_cycle_btn.grid(row=0, column=0, padx=(0, 10), sticky='w')

        # VFD cycle
        self._vfd_cycle_btn = tk.Button(cy_i,
            text='\u25b6  Cycle Main VFD',
            bg=dim, fg=fg, activebackground=dim, activeforeground=fg,
            font=self.F9, relief='flat', cursor='hand2', padx=6, pady=2,
            command=self._toggle_vfd_cycle)
        self._vfd_cycle_btn.grid(row=0, column=1, padx=(0, 16), sticky='w')

        # Shared speed slider
        tk.Label(cy_i, text='Speed:', bg=bg, fg=dim, font=self.F9).grid(
            row=0, column=2, sticky='w', padx=(0, 6))
        self._cycle_speed = tk.DoubleVar(value=1.0)
        tk.Scale(cy_i, variable=self._cycle_speed, from_=0.1, to=10.0,
                 resolution=0.1, orient='horizontal', length=130,
                 bg=bg, fg=fg, troughcolor=dim, highlightthickness=0,
                 showvalue=True, sliderlength=12, font=self.F9
                 ).grid(row=0, column=3, sticky='w')

        # Sequential offset checkbox
        self._cycle_offset = tk.BooleanVar(value=True)
        tk.Checkbutton(cy_i, text='Sequential offset (each position shows next char)',
                       variable=self._cycle_offset, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg,
                       font=self.F9).grid(row=0, column=4, sticky='w', padx=(16, 0))



    # ════════════════════════════════════════════════════════
    # TAB 3 — LEDs
    # ════════════════════════════════════════════════════════
    def _tab_leds(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  LEDs  ')

        # Global buttons
        glob = tk.Frame(tab, bg=bg)
        glob.pack(fill='x', pady=(0, 8))
        self._btn(glob, 'All LEDs ON  (all zones)',  self._leds_all_on).pack(side='left', padx=(0, 6))
        self._btn(glob, 'All LEDs OFF (all zones)', self._leds_all_off).pack(side='left', padx=(0, 6))
        tk.Label(glob, text='Zone/Port layout from HUI MIDI protocol spec, p.15',
                 font=self.F9, bg=bg, fg=dim).pack(side='left', padx=12)

        pane = tk.Frame(tab, bg=bg)
        pane.pack(fill='both', expand=True)

        # Zone list
        zf = tk.Frame(pane, bg=bg, highlightbackground=dim, highlightthickness=1)
        zf.pack(side='left', fill='y', padx=(0, 8))
        tk.Label(zf, text='  Zone  ', font=self.F9, bg=bg, fg=dim, pady=4).pack()
        self._zone_lb = tk.Listbox(zf, bg=bg, fg=fg, font=self.F9, width=26,
                                    selectbackground=dim, selectforeground=fg,
                                    activestyle='none', borderwidth=0,
                                    highlightbackground=dim)
        self._led_zones = [z for z in sorted(ZONE_NAMES) if z not in _LED_EXCLUDED]
        for z in self._led_zones:
            self._zone_lb.insert('end', f'  {z:02X}  {ZONE_NAMES[z]}')
        self._zone_lb.pack(fill='y', expand=True, padx=4, pady=4)
        self._zone_lb.bind('<<ListboxSelect>>', self._on_zone_select)

        # Port buttons area
        rf = tk.Frame(pane, bg=bg)
        rf.pack(side='left', fill='both', expand=True)
        tk.Label(rf, text='Ports in selected zone — click to toggle LED:',
                 font=self.F9, bg=bg, fg=dim).pack(anchor='w', pady=(0, 8))

        self._port_btn_frame = tk.Frame(rf, bg=bg)
        self._port_btn_frame.pack(fill='x')
        self._port_btns = []
        for p in range(8):
            btn = tk.Button(self._port_btn_frame, text=f'Port {p}',
                            width=11, relief='flat', cursor='hand2',
                            bg=dim, fg=fg, font=self.F9,
                            command=lambda pp=p: self._toggle_led(pp))
            btn.grid(row=p // 4, column=p % 4, padx=4, pady=4, sticky='ew')
            self._port_btns.append(btn)

        zone_ctrl = tk.Frame(rf, bg=bg)
        zone_ctrl.pack(fill='x', pady=8)
        self._btn(zone_ctrl, 'Zone: All Ports ON',  self._zone_all_on).pack(side='left', padx=(0, 6))
        self._btn(zone_ctrl, 'Zone: All Ports OFF', self._zone_all_off).pack(side='left')

        # Select first zone
        self._zone_lb.selection_set(0)
        self._on_zone_select(None)

        # ── LED Demo Modes ────────────────────────────────────
        demo_f = self._lframe(tab, 'LED Demo Modes')
        demo_f.pack(fill='x', pady=(8, 0))
        demo_i = tk.Frame(demo_f, bg=bg, padx=10, pady=8)
        demo_i.pack(fill='x')

        self._led_demo_btn = tk.Button(demo_i, text='  \u25b6  Start  ',
                                        bg=fg, fg=bg, font=self.F10B,
                                        relief='flat', cursor='hand2',
                                        command=self._toggle_led_demo)
        self._led_demo_btn.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky='ns')

        tk.Label(demo_i, text='Mode:', bg=bg, fg=dim, font=self.F9).grid(
            row=0, column=1, sticky='w')
        self._led_demo_mode = tk.StringVar(value='Christmas Lights!')
        _modes = ['Christmas Lights!', 'Chase', 'Strobe', 'Heartbeat']
        ttk.Combobox(demo_i, textvariable=self._led_demo_mode, values=_modes,
                     state='readonly', width=18).grid(row=1, column=1, sticky='w', pady=(0,4))

        tk.Label(demo_i, text='Speed:', bg=bg, fg=dim, font=self.F9).grid(
            row=0, column=2, sticky='w', padx=(14, 6))
        self._led_speed = tk.DoubleVar(value=1.0)
        tk.Scale(demo_i, variable=self._led_speed, from_=0.2, to=5.0, resolution=0.1,
                 orient='horizontal', length=130, bg=bg, fg=fg, troughcolor=dim,
                 highlightthickness=0, showvalue=True, sliderlength=12,
                 font=self.F9).grid(row=1, column=2, sticky='w', padx=(14, 0))

    # ════════════════════════════════════════════════════════
    # TAB 4 — METERS & V-POTS
    # ════════════════════════════════════════════════════════
    def _tab_meters_vpots(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Meters & V-Pots  ')

        # ── VU Meters ──────────────────────────────────────
        vu_f = self._lframe(tab, 'VU Meters  (polytouch A0)  — 0 = off   12 = clip / red')
        vu_f.pack(fill='x', pady=(0, 8))
        vu_i = tk.Frame(vu_f, bg=bg, padx=8, pady=6)
        vu_i.pack()

        self._vu_vars = []
        for i in range(8):
            col = tk.Frame(vu_i, bg=bg, highlightbackground=dim, highlightthickness=1)
            col.grid(row=0, column=i, padx=3, pady=2)
            tk.Label(col, text=f'CH{i+1}', font=self.F9, bg=bg, fg=dim, width=5).pack()
            lv = tk.IntVar(value=0)
            rv = tk.IntVar(value=0)
            self._vu_vars.append((lv, rv))
            for label, var, side in [('L', lv, 0), ('R', rv, 1)]:
                tk.Label(col, text=label, font=self.F9, bg=bg, fg=dim).pack()
                tk.Scale(col, variable=var, from_=12, to=0, orient='vertical',
                         length=90, width=14, bg=bg, fg=fg, troughcolor=dim,
                         highlightthickness=0, showvalue=False, sliderlength=10,
                         command=lambda v, ii=i, s=side: self.midi.send(HUI.vu(ii, s, int(v)))
                         ).pack(pady=(0, 2))

        vu_b = tk.Frame(vu_f, bg=bg, padx=8)
        vu_b.pack(fill='x', pady=(0, 6))
        self._btn(vu_b, 'All Off',  self._vu_all_off).pack(side='left', padx=(0, 6))
        self._btn(vu_b, 'All Clip', self._vu_all_max).pack(side='left', padx=(0, 10))
        self._vu_sweep_running = False
        self._vu_sweep_btn = tk.Button(vu_b, text='\u25b6  Sweep',
            bg=dim, fg=fg, activebackground=dim, activeforeground=fg,
            font=self.F9, relief='flat', cursor='hand2', padx=6, pady=2,
            command=self._toggle_vu_sweep)
        self._vu_sweep_btn.pack(side='left', padx=(0, 10))
        tk.Label(vu_b, text='Speed:', font=self.F9, bg=bg, fg=dim).pack(side='left', padx=(0, 4))
        self._vu_speed = tk.DoubleVar(value=1.0)
        tk.Scale(vu_b, variable=self._vu_speed, from_=0.2, to=5.0, resolution=0.1,
            orient='horizontal', length=110, bg=bg, fg=fg, troughcolor=dim,
            highlightthickness=0, showvalue=True, sliderlength=12,
            font=self.F9).pack(side='left')

        # ── V-Pot Rings ────────────────────────────────────
        vp_f = self._lframe(tab, 'V-Pot Rings  (CC B0 1p vv)  — Mode × position + optional centre LED')
        vp_f.pack(fill='x')
        vp_i = tk.Frame(vp_f, bg=bg, padx=6, pady=6)
        vp_i.pack()

        self._vp_mode  = []
        self._vp_pos   = []
        self._vp_ctr   = []
        pot_labels = [f'Ch{i+1}' for i in range(8)] + ['P1','P2','P3','P4','Scrl']

        mode_opts = [('Dot',    0x00), ('Pan',    0x10),
                     ('Level',  0x20), ('Spread', 0x30)]

        for i, lbl in enumerate(pot_labels):
            col = tk.Frame(vp_i, bg=bg, highlightbackground=dim, highlightthickness=1)
            col.grid(row=0, column=i, padx=2, pady=2)
            tk.Label(col, text=lbl, font=self.F9, bg=bg, fg=dim).pack()

            mode_v = tk.StringVar(value='Dot')
            pos_v  = tk.IntVar(value=0)
            ctr_v  = tk.BooleanVar(value=False)
            self._vp_mode.append(mode_v)
            self._vp_pos.append(pos_v)
            self._vp_ctr.append(ctr_v)

            mode_cb = ttk.Combobox(col, textvariable=mode_v, state='readonly', width=6,
                                   values=[m[0] for m in mode_opts])
            mode_cb.set('Dot')
            mode_cb.pack(padx=2, pady=2)
            mode_cb.bind('<<ComboboxSelected>>', lambda e, ii=i: self._send_vpot(ii))

            tk.Scale(col, variable=pos_v, from_=0, to=11, orient='horizontal',
                     length=68, width=12, bg=bg, fg=fg, troughcolor=dim,
                     highlightthickness=0, showvalue=False, sliderlength=10,
                     command=lambda v, ii=i: self._send_vpot(ii)).pack(padx=2)

            tk.Checkbutton(col, text='ctr', variable=ctr_v, bg=bg, fg=dim,
                           selectcolor=bg, activebackground=bg, font=self.F9,
                           command=lambda ii=i: self._send_vpot(ii)).pack()

        # Store mode option name→value map for the combobox
        self._vp_mode_map = {m[0]: m[1] for m in mode_opts}

        vp_b = tk.Frame(vp_f, bg=bg, padx=8)
        vp_b.pack(fill='x', pady=(0, 6))
        self._btn(vp_b, 'All Off',        self._vp_all_off).pack(side='left', padx=(0, 6))
        self._btn(vp_b, 'All Dot Max',    self._vp_all_max).pack(side='left', padx=(0, 6))
        self._btn(vp_b, 'All Spread Max', self._vp_all_spread).pack(side='left', padx=(0, 10))
        self._vp_sweep_running = False
        self._vp_together = tk.BooleanVar(value=False)
        self._vp_sweep_btn = tk.Button(vp_b, text='\u25b6  Sweep',
            bg=dim, fg=fg, activebackground=dim, activeforeground=fg,
            font=self.F9, relief='flat', cursor='hand2', padx=6, pady=2,
            command=self._toggle_vp_sweep)
        self._vp_sweep_btn.pack(side='left', padx=(0, 10))
        tk.Label(vp_b, text='Speed:', font=self.F9, bg=bg, fg=dim).pack(side='left', padx=(0, 4))
        self._vp_speed = tk.DoubleVar(value=1.0)
        tk.Scale(vp_b, variable=self._vp_speed, from_=0.2, to=5.0, resolution=0.1,
            orient='horizontal', length=110, bg=bg, fg=fg, troughcolor=dim,
            highlightthickness=0, showvalue=True, sliderlength=12,
            font=self.F9).pack(side='left')
        tk.Checkbutton(vp_b, text='All in sync',
                       variable=self._vp_together, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg, font=self.F9
                       ).pack(side='left', padx=(12, 0))

    # ════════════════════════════════════════════════════════
    # TAB 5 — FADERS
    # ════════════════════════════════════════════════════════
    def _tab_faders(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Faders  ')

        tk.Label(tab,
                 text='Drives motorised faders directly.  0% = bottom (fully down),  100% = top (fully up).',
                 font=self.F9, bg=bg, fg=dim).pack(anchor='w', pady=(0, 10))

        frow = tk.Frame(tab, bg=bg)
        frow.pack()

        self._fader_vars  = []
        self._fader_labels = []
        for i in range(8):
            col = tk.Frame(frow, bg=bg, highlightbackground=dim, highlightthickness=1, padx=5, pady=5)
            col.grid(row=0, column=i, padx=4)
            tk.Label(col, text=f'CH{i+1}', font=self.F9, bg=bg, fg=dim).pack()
            pct_lbl = tk.Label(col, text=' 0%', font=self.F9, bg=bg, fg=fg, width=4)
            pct_lbl.pack()
            self._fader_labels.append(pct_lbl)
            fv = tk.IntVar(value=0)
            self._fader_vars.append(fv)
            tk.Scale(col, variable=fv, from_=100, to=0, orient='vertical',
                     length=160, width=22, bg=bg, fg=fg, troughcolor=dim,
                     highlightthickness=0, sliderlength=16, showvalue=False,
                     command=lambda v, ii=i: self._on_fader(ii, int(v))).pack(pady=4)

        brow = tk.Frame(tab, bg=bg)
        brow.pack(pady=8)
        self._btn(brow, 'All to Top    (100%)', lambda: self._faders_all(100)).pack(side='left', padx=4)
        self._btn(brow, 'All to Centre  (50%)', lambda: self._faders_all( 50)).pack(side='left', padx=4)
        self._btn(brow, 'All to Bottom   (0%)', lambda: self._faders_all(  0)).pack(side='left', padx=4)

        # ── Automated Demo ─────────────────────────────────
        demo_f = self._lframe(tab, 'Automated Motion Demo')
        demo_f.pack(fill='x', pady=(8, 0))
        demo_i = tk.Frame(demo_f, bg=bg, padx=10, pady=8)
        demo_i.pack(fill='x')

        # Start / Stop
        self._demo_btn = tk.Button(demo_i, text='  \u25b6  Start Demo  ',
                                    bg=self.cfg.fg_color, fg=bg,
                                    font=self.F10B, relief='flat', cursor='hand2',
                                    command=self._toggle_demo)
        self._demo_btn.grid(row=0, column=0, rowspan=2, padx=(0, 16), sticky='ns')

        # Wave type
        tk.Label(demo_i, text='Motion:', font=self.F9, bg=bg, fg=dim).grid(
            row=0, column=1, sticky='w')
        self._demo_wave = tk.StringVar(value='Sine')
        ttk.Combobox(demo_i, textvariable=self._demo_wave, state='readonly', width=10,
                     values=['Sine', 'Square', 'Triangle', 'Sawtooth', 'Bounce']
                     ).grid(row=1, column=1, sticky='w', pady=(0, 4))

        # Phase spread
        self._demo_phase = tk.BooleanVar(value=True)
        tk.Checkbutton(demo_i, text='Phase spread (wave effect across channels)',
                       variable=self._demo_phase, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg,
                       font=self.F9).grid(row=0, column=2, columnspan=3, sticky='w', padx=(16, 0))

        # Speed
        tk.Label(demo_i, text='Speed:', font=self.F9, bg=bg, fg=dim).grid(
            row=1, column=2, sticky='w', padx=(16, 6))
        self._demo_speed = tk.DoubleVar(value=1.0)
        tk.Scale(demo_i, variable=self._demo_speed, from_=0.2, to=4.0,
                 resolution=0.1, orient='horizontal', length=120,
                 bg=bg, fg=fg, troughcolor=dim, highlightthickness=0,
                 showvalue=True, sliderlength=12, font=self.F9
                 ).grid(row=1, column=3, sticky='w')

        # Amplitude
        tk.Label(demo_i, text='Amplitude:', font=self.F9, bg=bg, fg=dim).grid(
            row=1, column=4, sticky='w', padx=(12, 6))
        self._demo_amp = tk.IntVar(value=80)
        tk.Scale(demo_i, variable=self._demo_amp, from_=10, to=100,
                 orient='horizontal', length=120,
                 bg=bg, fg=fg, troughcolor=dim, highlightthickness=0,
                 showvalue=True, sliderlength=12, font=self.F9
                 ).grid(row=1, column=5, sticky='w')

        # Centre
        tk.Label(demo_i, text='Centre:', font=self.F9, bg=bg, fg=dim).grid(
            row=1, column=6, sticky='w', padx=(12, 6))
        self._demo_centre = tk.IntVar(value=50)
        tk.Scale(demo_i, variable=self._demo_centre, from_=0, to=100,
                 orient='horizontal', length=100,
                 bg=bg, fg=fg, troughcolor=dim, highlightthickness=0,
                 showvalue=True, sliderlength=12, font=self.F9
                 ).grid(row=1, column=7, sticky='w')

    # ════════════════════════════════════════════════════════
    # TAB 6 — AUDIO & RELAYS
    # ════════════════════════════════════════════════════════
    def _tab_audio(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Audio & Relays  ')

        tk.Label(tab,
                 text='All controls use Zone 0x1D.  Relays and beeper use LED zone/port protocol.',
                 font=self.F9, bg=bg, fg=dim).pack(anchor='w', pady=(0, 12))

        # Relays
        rf = self._lframe(tab, 'Relays  (Zone 1D, Port 0 and Port 1)')
        rf.pack(fill='x', pady=(0, 8))
        ri = tk.Frame(rf, bg=bg, padx=8, pady=8)
        ri.pack(fill='x')

        self._r1_btn = tk.Button(ri, text='Relay 1  OFF', width=14,
                                  bg=dim, fg=fg, font=self.F10B,
                                  relief='flat', cursor='hand2',
                                  command=self._toggle_relay1)
        self._r1_btn.grid(row=0, column=0, padx=8, pady=4)

        self._r2_btn = tk.Button(ri, text='Relay 2  OFF', width=14,
                                  bg=dim, fg=fg, font=self.F10B,
                                  relief='flat', cursor='hand2',
                                  command=self._toggle_relay2)
        self._r2_btn.grid(row=0, column=1, padx=8, pady=4)

        tk.Label(ri, text='Relay outputs can drive external indicator lights, record lamps, etc.',
                 font=self.F9, bg=bg, fg=dim).grid(row=1, column=0, columnspan=2, pady=(4, 0), sticky='w')

        # Click
        cf = self._lframe(tab, 'Click  (Zone 1D, Port 2) — fires once, no off needed')
        cf.pack(fill='x', pady=(0, 8))
        ci = tk.Frame(cf, bg=bg, padx=8, pady=8)
        ci.pack(fill='x')
        self._btn(ci, '\u25b6  Single Click', self._send_click).pack(side='left', padx=(0, 12))
        tk.Label(ci, text='Repeat:', font=self.F9, bg=bg, fg=dim).pack(side='left', padx=(0, 4))
        self._click_n = tk.IntVar(value=3)
        tk.Spinbox(ci, from_=1, to=30, textvariable=self._click_n, width=4,
                   bg=dim, fg=fg, font=self.F9, buttonbackground=dim).pack(side='left', padx=(0, 6))
        self._btn(ci, 'Click N times', self._click_n_times).pack(side='left')

        # Beep
        bf = self._lframe(tab, 'Beeper  (Zone 1D, Port 3) — stays on until turned off')
        bf.pack(fill='x')
        bi = tk.Frame(bf, bg=bg, padx=8, pady=8)
        bi.pack(fill='x')
        self._beep_btn = tk.Button(bi, text='Beeper  OFF', width=14,
                                    bg=dim, fg=fg, font=self.F10B,
                                    relief='flat', cursor='hand2',
                                    command=self._toggle_beep)
        self._beep_btn.pack(side='left', padx=(0, 12))
        tk.Label(bi, text='Warning: sends the HUI\'s internal beeper until toggled off.',
                 font=self.F9, bg=bg, fg=dim).pack(side='left')

    # ════════════════════════════════════════════════════════
    # MIDI ACTIONS
    # ════════════════════════════════════════════════════════
    def _send_ping(self):
        self._ping_var.set('waiting\u2026')
        self.midi.send(HUI.ping())

    # -- Display --
    def _vfd_send_zone(self, zone: int, text: str) -> None:
        """Send one VFD zone and update the internal state buffer."""
        self.midi.send(HUI.vfd(zone, text))
        self._vfd_state.update_zone(zone, text)

    def _send_vfd(self):
        r1 = (self._vfd1.get() + ' ' * 40)[:40]
        r2 = (self._vfd2.get() + ' ' * 40)[:40]
        for z in range(4):
            self._vfd_send_zone(z,     r1[z*10:(z+1)*10])
            self._vfd_send_zone(z + 4, r2[z*10:(z+1)*10])

    def _clear_vfd(self):
        for e in (self._vfd1, self._vfd2):
            e.delete(0, 'end')
        for z in range(8):
            self._vfd_send_zone(z, ' ' * 10)

    def _vfd_test_pattern(self):
        self._vfd1.delete(0, 'end')
        self._vfd1.insert(0, '1234567890' * 4)
        self._vfd2.delete(0, 'end')
        self._vfd2.insert(0, 'ABCDEFGHIJ' * 4)
        self._send_vfd()

    def _vfd_fill_blocks(self):
        self._vfd1.delete(0, 'end'); self._vfd1.insert(0, '\u2588' * 40)
        self._vfd2.delete(0, 'end'); self._vfd2.insert(0, '\u2588' * 40)
        self._send_vfd()

    def _send_scribbles(self):
        for i, v in enumerate(self._scribble_vars):
            self.midi.send(HUI.scribble(i, (v.get() + '    ')[:4]))

    def _clear_scribbles(self):
        for v in self._scribble_vars:
            v.set('    ')
        self._send_scribbles()

    def _scribble_test(self):
        labels = [f'Ch{i+1}' for i in range(8)] + ['Sel.']
        for i, v in enumerate(self._scribble_vars):
            v.set(labels[i].ljust(4)[:4])
        self._send_scribbles()

    def _send_timecode(self):
        try:
            raw = self._tc_var.get().replace(':', '')
            if len(raw) != 8:
                return
            hh, mm, ss, ff = int(raw[0:2]), int(raw[2:4]), int(raw[4:6]), int(raw[6:8])
            self.midi.send(HUI.timecode(hh, mm, ss, ff))
        except Exception:
            pass

    # -- LEDs --
    def _on_zone_select(self, _event):
        sel = self._zone_lb.curselection()
        if not sel:
            return
        self._selected_zone = self._led_zones[sel[0]]
        self._refresh_port_buttons()

    def _refresh_port_buttons(self):
        zone = self._selected_zone
        # Use override if defined (e.g. zone 0x0D only shows 'mode')
        ports = _LED_ONLY_PORTS.get(zone, PORT_NAMES.get(zone, {}))
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        for p, btn in enumerate(self._port_btns):
            if p in ports:
                state = self.led_state[zone].get(p, False)
                btn.configure(text=ports[p],
                              bg=fg if state else dim,
                              fg=bg if state else fg,
                              state='normal')
            else:
                # Port exists in PORT_NAMES but has no LED — hide it
                btn.configure(text='', bg=bg, fg=bg, state='disabled',
                              relief='flat', cursor='')

    def _toggle_led(self, port):
        zone  = self._selected_zone
        new   = not self.led_state[zone].get(port, False)
        self.led_state[zone][port] = new
        self.midi.send(HUI.led_on(zone, port) if new else HUI.led_off(zone, port))
        self._refresh_port_buttons()

    def _zone_all_on(self):
        z = self._selected_zone
        ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
        if z in _LED_ONLY_PORTS:
            # Only some ports have LEDs — send individually
            for p in ports:
                self.led_state[z][p] = True
                self.midi.send(HUI.led_on(z, p))
        else:
            for p in ports:
                self.led_state[z][p] = True
            self.midi.send(HUI.led_zone_all(z, True))
        self._refresh_port_buttons()

    def _zone_all_off(self):
        z = self._selected_zone
        ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
        if z in _LED_ONLY_PORTS:
            for p in ports:
                self.led_state[z][p] = False
                self.midi.send(HUI.led_off(z, p))
        else:
            for p in ports:
                self.led_state[z][p] = False
            self.midi.send(HUI.led_zone_all(z, False))
        self._refresh_port_buttons()

    def _leds_all_on(self):
        for z in self._led_zones:          # already excludes _LED_EXCLUDED zones
            ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
            if z in _LED_ONLY_PORTS:
                for p in ports:
                    self.led_state[z][p] = True
                    self.midi.send(HUI.led_on(z, p))
            else:
                for p in ports:
                    self.led_state[z][p] = True
                self.midi.send(HUI.led_zone_all(z, True))
        self._refresh_port_buttons()

    def _leds_all_off(self):
        for z in self._led_zones:
            ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
            if z in _LED_ONLY_PORTS:
                for p in ports:
                    self.led_state[z][p] = False
                    self.midi.send(HUI.led_off(z, p))
            else:
                for p in ports:
                    self.led_state[z][p] = False
                self.midi.send(HUI.led_zone_all(z, False))
        self._refresh_port_buttons()

    # -- Meters --
    def _vu_all_off(self):
        for i, (lv, rv) in enumerate(self._vu_vars):
            lv.set(0); rv.set(0)
            self.midi.send(HUI.vu(i, 0, 0))
            self.midi.send(HUI.vu(i, 1, 0))

    def _vu_all_max(self):
        for i, (lv, rv) in enumerate(self._vu_vars):
            lv.set(12); rv.set(12)
            self.midi.send(HUI.vu(i, 0, 12))
            self.midi.send(HUI.vu(i, 1, 12))

    def _vu_sweep(self):
        """Kept for backward compatibility; just triggers a one-shot sweep."""
        self._toggle_vu_sweep()

    def _toggle_vu_sweep(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        if self._vu_sweep_running:
            self._vu_sweep_running = False
            self._vu_sweep_btn.configure(text='\u25b6  Sweep', bg=dim, fg=fg)
        else:
            self._vu_sweep_running = True
            self._vu_sweep_btn.configure(text='\u25a0  Stop', bg='#cc3333', fg='#ffffff')
            threading.Thread(target=self._vu_sweep_loop, daemon=True).start()

    def _vu_sweep_loop(self):
        t = 0.0
        DT = 0.04
        while self._vu_sweep_running:
            speed = self._vu_speed.get()
            v = min(12, round((math.sin(t * speed) + 1.0) * 6.0))
            for i, (lv, rv) in enumerate(self._vu_vars):
                self.root.after(0, lv.set, v)
                self.root.after(0, rv.set, v)
                self.midi.send(HUI.vu(i, 0, v))
                self.midi.send(HUI.vu(i, 1, v))
            t  += DT
            time.sleep(DT)
        # Silence all meters when stopped
        for i, (lv, rv) in enumerate(self._vu_vars):
            self.root.after(0, lv.set, 0)
            self.root.after(0, rv.set, 0)
            self.midi.send(HUI.vu(i, 0, 0))
            self.midi.send(HUI.vu(i, 1, 0))

    # -- Timecode counter --
    def _toggle_tc_counter(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        if self._tc_running:
            self._tc_running = False
            self._tc_count_btn.configure(text='\u25b6  Start Counter', bg=dim, fg=fg)
        else:
            self._tc_running = True
            self._tc_count_btn.configure(text='\u25a0  Stop Counter', bg='#cc3333', fg='#ffffff')
            self._tc_tick()

    def _tc_tick(self):
        if not self._tc_running:
            return
        try:
            fps = max(1, min(240, int(self._tc_fps_var.get())))
        except ValueError:
            fps = 25
        # Parse current HH:MM:SS:FF value
        try:
            raw = self._tc_var.get().replace(':', '')
            hh = int(raw[0:2]); mm = int(raw[2:4])
            ss = int(raw[4:6]); ff = int(raw[6:8])
        except Exception:
            hh, mm, ss, ff = 0, 0, 0, 0
        # Increment one frame
        ff += 1
        if ff >= fps: ff = 0; ss += 1
        if ss >= 60:  ss = 0; mm += 1
        if mm >= 60:  mm = 0; hh = (hh + 1) % 100
        self._tc_var.set(f'{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}')
        self._send_timecode()
        self.root.after(int(1000 / fps), self._tc_tick)


    def _send_vpot(self, pot: int):
        mode_name = self._vp_mode[pot].get()
        # mode var is StringVar holding the display name; look up numeric offset
        mode_off  = self._vp_mode_map.get(mode_name, 0)
        pos       = self._vp_pos[pot].get() & 0x0F
        value     = mode_off | pos
        if self._vp_ctr[pot].get():
            value |= 0x40
        self.midi.send(HUI.vpot(pot, value))

    def _vp_all_off(self):
        for i in range(13):
            self.midi.send(HUI.vpot(i, 0x00))

    def _vp_all_max(self):
        for i in range(13):
            self.midi.send(HUI.vpot(i, 0x0B))

    def _vp_all_spread(self):
        for i in range(13):
            self.midi.send(HUI.vpot(i, 0x36))  # mode 3, max spread

    def _toggle_vp_sweep(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        if self._vp_sweep_running:
            self._vp_sweep_running = False
            self._vp_sweep_btn.configure(text='\u25b6  Sweep', bg=dim, fg=fg)
        else:
            self._vp_sweep_running = True
            self._vp_sweep_btn.configure(text='\u25a0  Stop', bg='#cc3333', fg='#ffffff')
            threading.Thread(target=self._vp_sweep_loop, daemon=True).start()

    def _vp_sweep_loop(self):
        """
        Animates all 13 V-pot rings continuously in Dot mode.
        Position ranges from 1 (leftmost) to 11 (rightmost) — position 0
        would blank the ring entirely, so it is excluded.
        The 'together' option moves all pots in sync; otherwise each gets a
        phase offset so they form a rolling wave across the surface.
        """
        t = 0.0
        DT = 0.04
        while self._vp_sweep_running:
            speed    = self._vp_speed.get()
            together = self._vp_together.get()
            for pot in range(13):
                phase = 0.0 if together else pot * (2.0 * math.pi / 13.0)
                # Range 1-11: shift sin result away from 0
                pos = 1 + min(10, round((math.sin(t * speed + phase) + 1.0) * 5.0))
                self.midi.send(HUI.vpot(pot, 0x00 | pos))
            t += DT
            time.sleep(DT)
        # Rest all rings at centre when stopped
        for pot in range(13):
            self.midi.send(HUI.vpot(pot, 0x06))   # Dot mode, position 6 (centre)


    def _on_fader(self, zone: int, pct: int):
        self._fader_labels[zone].configure(text=f'{pct:3d}%')
        self.midi.send(HUI.fader(zone, int(pct * 16383 / 100)))

    def _faders_all(self, pct: int):
        self._stop_demo()
        for i, fv in enumerate(self._fader_vars):
            fv.set(pct)
            self._on_fader(i, pct)

    def _toggle_demo(self):
        if self._demo_running:
            self._stop_demo()
        else:
            self._start_demo()

    def _start_demo(self):
        if self._demo_running:
            return
        self._demo_running = True
        bg = self.cfg.bg_color
        self._demo_btn.configure(text='  \u25a0  Stop Demo  ',
                                  bg='#ff5555', fg='#ffffff')
        self._demo_thread = threading.Thread(target=self._demo_loop, daemon=True)
        self._demo_thread.start()

    def _stop_demo(self):
        self._demo_running      = False
        self._led_demo_running  = False
        self._scrib_cycle_running = False
        self._vfd_cycle_running = False
        bg, fg = self.cfg.bg_color, self.cfg.fg_color
        if hasattr(self, '_demo_btn'):
            self._demo_btn.configure(text='  \u25b6  Start Demo  ', bg=fg, fg=bg)
        if hasattr(self, '_led_demo_btn'):
            self._led_demo_btn.configure(text='  \u25b6  Start  ', bg=fg, fg=bg)
        if hasattr(self, '_scrib_cycle_btn'):
            self._scrib_cycle_btn.configure(text='\u25b6  Cycle Scribble Strips',
                                             bg=self.cfg.dim_color, fg=fg)
        if hasattr(self, '_vfd_cycle_btn'):
            self._vfd_cycle_btn.configure(text='\u25b6  Cycle Main VFD',
                                           bg=self.cfg.dim_color, fg=fg)

    # ════════════════════════════════════════════════════════
    # LED DEMO MODES
    # ════════════════════════════════════════════════════════

    def _toggle_led_demo(self):
        bg, fg = self.cfg.bg_color, self.cfg.fg_color
        if self._led_demo_running:
            self._led_demo_running = False
            self._led_demo_btn.configure(text='  \u25b6  Start  ', bg=fg, fg=bg)
            self._leds_all_off()
        else:
            self._led_demo_running = True
            self._led_demo_btn.configure(text='  \u25a0  Stop  ', bg='#ff5555', fg='#ffffff')
            mode = self._led_demo_mode.get()
            target = {
                'Christmas Lights!': self._led_demo_christmas,
                'Chase':             self._led_demo_chase,
                'Strobe':            self._led_demo_strobe,
                'Heartbeat':         self._led_demo_heartbeat,
            }.get(mode, self._led_demo_christmas)
            threading.Thread(target=target, daemon=True).start()

    def _all_led_pairs(self):
        """Flat list of (zone, port) for every LED in the panel."""
        pairs = []
        for z in self._led_zones:
            ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
            for p in ports:
                pairs.append((z, p))
        return pairs

    def _led_demo_christmas(self):
        """Each LED blinks independently at a random interval."""
        import random
        pairs = self._all_led_pairs()
        states    = {led: False  for led in pairs}
        countdown = {led: random.uniform(0.05, 1.2) for led in pairs}
        last_t = time.time()
        while self._led_demo_running:
            speed = max(0.01, self._led_speed.get())
            now   = time.time()
            dt    = now - last_t
            last_t = now
            for led in pairs:
                countdown[led] -= dt * speed
                if countdown[led] <= 0:
                    states[led] = not states[led]
                    z, p = led
                    self.led_state[z][p] = states[led]
                    self.midi.send(HUI.led_on(z, p) if states[led] else HUI.led_off(z, p))
                    countdown[led] = random.uniform(0.05, 1.2)
            time.sleep(0.02)

    def _led_demo_chase(self):
        """One lit LED sweeps through all LEDs in sequence."""
        pairs = self._all_led_pairs()
        n     = len(pairs)
        if n == 0:
            return
        idx      = 0
        prev     = None
        elapsed  = 0.0
        dt       = 0.02
        while self._led_demo_running:
            speed   = max(0.01, self._led_speed.get())
            elapsed += dt * speed
            if elapsed >= 0.06:      # step interval at speed=1: ~60 ms
                elapsed = 0.0
                if prev:
                    z, p = prev
                    self.led_state[z][p] = False
                    self.midi.send(HUI.led_off(z, p))
                z, p = pairs[idx % n]
                self.led_state[z][p] = True
                self.midi.send(HUI.led_on(z, p))
                prev = pairs[idx % n]
                idx  = (idx + 1) % n
            time.sleep(dt)
        if prev:
            z, p = prev
            self.led_state[z][p] = False
            self.midi.send(HUI.led_off(z, p))

    def _led_demo_strobe(self):
        """All LEDs flash on and off in unison."""
        dt      = 0.02
        elapsed = 0.0
        led_on  = False
        while self._led_demo_running:
            speed   = max(0.01, self._led_speed.get())
            elapsed += dt * speed
            if elapsed >= 0.25:     # half-period at speed=1: 250 ms → 2 Hz
                elapsed = 0.0
                led_on  = not led_on
                for z, p in self._all_led_pairs():
                    self.led_state[z][p] = led_on
                    self.midi.send(HUI.led_on(z, p) if led_on else HUI.led_off(z, p))
            time.sleep(dt)

    def _led_demo_heartbeat(self):
        """All LEDs beat in a heartbeat (lub-dub) rhythm."""
        pairs   = self._all_led_pairs()

        def _set_all(on):
            for z, p in pairs:
                self.led_state[z][p] = on
                self.midi.send(HUI.led_on(z, p) if on else HUI.led_off(z, p))

        # Pattern: (state, seconds at speed=1)
        pattern = [(True, 0.07), (False, 0.12), (True, 0.07),
                   (False, 0.12), (False, 0.62)]
        while self._led_demo_running:
            speed = max(0.01, self._led_speed.get())
            for on, dur in pattern:
                if not self._led_demo_running:
                    break
                _set_all(on)
                time.sleep(dur / speed)

    # ════════════════════════════════════════════════════════
    # CHARACTER CYCLING  (scribble strips + main VFD)
    # ════════════════════════════════════════════════════════

    # Printable HUI charset for cycling
    _CYCLE_CHARS = [chr(c) for c in range(0x20, 0x7E)]   # space…}  (94 chars)

    def _toggle_scrib_cycle(self):
        fg, dim = self.cfg.fg_color, self.cfg.dim_color
        if self._scrib_cycle_running:
            self._scrib_cycle_running = False
            self._scrib_cycle_btn.configure(text='\u25b6  Cycle Scribble Strips',
                                             bg=dim, fg=fg)
        else:
            self._scrib_cycle_running = True
            self._scrib_cycle_btn.configure(text='\u25a0  Stop Scribble Cycle',
                                             bg='#ff5555', fg='#ffffff')
            threading.Thread(target=self._scrib_cycle_loop, daemon=True).start()

    def _scrib_cycle_loop(self):
        chars   = self._CYCLE_CHARS
        n       = len(chars)
        N_STRIP = 9
        idx     = 0
        dt      = 0.02
        elapsed = 0.0
        while self._scrib_cycle_running:
            speed   = max(0.01, self._cycle_speed.get())
            elapsed += dt * speed
            if elapsed >= 0.12:     # step rate at speed=1: ~8 chars/sec
                elapsed = 0.0
                offset  = self._cycle_offset.get()
                for i in range(N_STRIP):
                    c   = chars[(idx + (i * 3 if offset else 0)) % n]
                    txt = c * 4
                    self.root.after(0, self._scribble_vars[i].set, txt)
                    self.midi.send(HUI.scribble(i, txt))
                idx = (idx + 1) % n
            time.sleep(dt)
        # Blank strips on stop
        for i in range(9):
            self.root.after(0, self._scribble_vars[i].set, '')
            self.midi.send(HUI.scribble(i, '    '))

    def _toggle_vfd_cycle(self):
        fg, dim = self.cfg.fg_color, self.cfg.dim_color
        if self._vfd_cycle_running:
            self._vfd_cycle_running = False
            self._vfd_cycle_btn.configure(text='\u25b6  Cycle Main VFD',
                                           bg=dim, fg=fg)
        else:
            self._vfd_cycle_running = True
            self._vfd_cycle_btn.configure(text='\u25a0  Stop VFD Cycle',
                                           bg='#ff5555', fg='#ffffff')
            threading.Thread(target=self._vfd_cycle_loop, daemon=True).start()

    def _vfd_cycle_loop(self):
        chars   = self._CYCLE_CHARS
        n       = len(chars)
        idx     = 0
        dt      = 0.02
        elapsed = 0.0
        while self._vfd_cycle_running:
            speed   = max(0.01, self._cycle_speed.get())
            elapsed += dt * speed
            if elapsed >= 0.10:
                elapsed = 0.0
                offset  = self._cycle_offset.get()
                row1    = ''.join(chars[(idx + (col if offset else 0)) % n]
                                  for col in range(40))
                row2    = ''.join(chars[(idx + (col + 20 if offset else 0)) % n]
                                  for col in range(40))
                for z in range(4):
                    self._vfd_send_zone(z,     row1[z*10:(z+1)*10])
                    self._vfd_send_zone(z + 4, row2[z*10:(z+1)*10])
                idx = (idx + 1) % n
            time.sleep(dt)
        # Blank VFD on stop
        for z in range(8):
            self._vfd_send_zone(z, ' ' * 10)

    # ════════════════════════════════════════════════════════
    # TAB 8 — GOODIES
    # ════════════════════════════════════════════════════════


    # ════════════════════════════════════════════════════════
    # TAB 9 — TEST WIZARD
    # ════════════════════════════════════════════════════════
    def _tab_wizard(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Test Wizard  ')

        # ── Header & start button ─────────────────────────────
        hdr = tk.Frame(tab, bg=bg)
        hdr.pack(fill='x', pady=(0, 8))

        tk.Label(hdr,
                 text='Automated Test Wizard — tests all detectable HUI hardware features in sequence.',
                 font=self.F9, bg=bg, fg=dim).pack(anchor='w')
        tk.Label(hdr,
                 text='Auto steps wait for hardware input. Manual steps send a stimulus and ask you to confirm.',
                 font=self.F9, bg=bg, fg=dim).pack(anchor='w', pady=(2, 8))

        btn_row = tk.Frame(tab, bg=bg)
        btn_row.pack(fill='x', pady=(0, 4))
        self._wiz_start_btn = self._btn(btn_row, '  \u25b6  Run Test Wizard  ', self._wiz_start)
        self._wiz_start_btn.pack(side='left')
        self._wiz_stop_btn = tk.Button(btn_row, text='  \u25a0  Stop  ',
                                        bg=self.cfg.dim_color, fg=fg,
                                        activebackground=dim, activeforeground=fg,
                                        font=self.F10B, relief='flat', cursor='hand2',
                                        command=self._wiz_stop, state='disabled')
        self._wiz_stop_btn.pack(side='left', padx=(8, 0))
        self._wiz_export_btn = self._btn(btn_row, '  \u2193  Export Report  ', self._wiz_export)
        self._wiz_export_btn.pack(side='right')
        self._wiz_adv_btn = self._btn(btn_row, '  \u2699  Step Selection\u2026  ', self._wiz_open_advanced)
        self._wiz_adv_btn.pack(side='right', padx=(0, 8))

        opt_row = tk.Frame(tab, bg=bg)
        opt_row.pack(fill='x', pady=(0, 8))
        self._wiz_autoskip_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opt_row, text='Auto-skip steps on timeout',
                       variable=self._wiz_autoskip_var,
                       bg=bg, fg=dim, selectcolor=bg, activebackground=bg,
                       font=self.F9).pack(side='left')

        # ── Progress bar ──────────────────────────────────────
        prog_f = tk.Frame(tab, bg=bg)
        prog_f.pack(fill='x', pady=(0, 8))
        self._wiz_prog_lbl = tk.Label(prog_f, text='', font=self.F9, bg=bg, fg=dim)
        self._wiz_prog_lbl.pack(anchor='w')
        self._wiz_prog_bar = tk.Canvas(prog_f, height=10, bg=dim,
                                        highlightthickness=0)
        self._wiz_prog_bar.pack(fill='x', pady=(2, 0))
        self._wiz_prog_fill = self._wiz_prog_bar.create_rectangle(
            0, 0, 0, 10, fill=fg, outline='')

        # ── Current step panel ────────────────────────────────
        step_f = self._lframe(tab, 'Current Step')
        step_f.pack(fill='x', pady=(0, 8))
        si = tk.Frame(step_f, bg=bg, padx=10, pady=10)
        si.pack(fill='x')

        self._wiz_step_title = tk.Label(si, text='—',
                                         font=('Segoe UI', 13, 'bold'),
                                         bg=bg, fg=fg, anchor='w', justify='left')
        self._wiz_step_title.pack(fill='x')
        self._wiz_step_desc  = tk.Label(si, text='',
                                         font=self.F9, bg=bg, fg=dim,
                                         anchor='w', justify='left', wraplength=700)
        self._wiz_step_desc.pack(fill='x', pady=(4, 10))

        # Manual confirm buttons (hidden until needed)
        self._wiz_confirm_frame = tk.Frame(si, bg=bg)
        self._wiz_confirm_frame.pack(anchor='w')
        self._wiz_pass_btn = tk.Button(self._wiz_confirm_frame,
                                        text='  \u2714  Pass  ',
                                        bg='#226622', fg='#ffffff',
                                        activebackground='#44aa44', activeforeground='#ffffff',
                                        font=self.F10B, relief='flat', cursor='hand2',
                                        command=lambda: self._wiz_manual_result(True))
        self._wiz_fail_btn = tk.Button(self._wiz_confirm_frame,
                                        text='  \u2716  Fail  ',
                                        bg='#882222', fg='#ffffff',
                                        activebackground='#cc4444', activeforeground='#ffffff',
                                        font=self.F10B, relief='flat', cursor='hand2',
                                        command=lambda: self._wiz_manual_result(False))
        self._wiz_timeout_lbl = tk.Label(self._wiz_confirm_frame, text='',
                                          font=self.F9, bg=bg, fg=dim)

        # ── Results log ───────────────────────────────────────
        log_f = self._lframe(tab, 'Results')
        log_f.pack(fill='both', expand=True)
        li = tk.Frame(log_f, bg=bg, padx=6, pady=6)
        li.pack(fill='both', expand=True)
        self._wiz_log = tk.Text(li, bg=bg, fg=dim, font=self.F9,
                                  state='disabled', relief='flat',
                                  highlightthickness=0, height=8)
        sb = tk.Scrollbar(li, command=self._wiz_log.yview, bg=dim, troughcolor=bg)
        self._wiz_log.configure(yscrollcommand=sb.set)
        self._wiz_log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self._wiz_log.tag_configure('pass', foreground='#44cc44')
        self._wiz_log.tag_configure('fail', foreground='#ff5555')
        self._wiz_log.tag_configure('skip', foreground=dim)
        self._wiz_log.tag_configure('head', foreground=fg, font=('Segoe UI', 9, 'bold'))

    # ════════════════════════════════════════════════════════
    # WIZARD ENGINE
    # ════════════════════════════════════════════════════════

    # Each step: (label, type, setup_fn, check_fn_or_None, timeout_s)
    # type: 'auto'   — wait for hardware input, check_fn returns True/False/None
    #       'manual' — send stimulus via setup_fn, then show Pass/Fail buttons
    #       'info'   — no hardware test, show info then auto-advance after 1 s


    def _wiz_build_steps(self):
        S = self  # shorthand

        # ── Helper: fader auto-detect move test ───────────────────
        def fader_move(ch, timeout=18):
            label = f'Fader {ch+1}: Move'
            def _setup(c=ch):
                S._wiz_flash_zone(c)
                S._wiz_snapshot[f'fader_{c}_base'] = None
                S._wiz_step_desc.configure(
                    text=f'Move Fader {c+1} up and down through its full range.')
            def _check(c=ch):
                with S._hui_state.lock:
                    pos = S._hui_state.fader_pos[c]
                key = f'fader_{c}_base'
                if S._wiz_snapshot.get(key) is None:
                    S._wiz_snapshot[key] = pos
                    return None
                if abs(pos - S._wiz_snapshot[key]) > 600:
                    return True
                return None
            return (label, 'auto', _setup, _check, timeout)

        # ── Helper: fader-touch auto-step ─────────────────────────
        def fader_touch(ch, timeout=10):
            label = f'Fader {ch+1}: Touch sensor'
            def _setup(c=ch):
                S._wiz_flash_zone(c)
                with S._hui_state.lock:
                    S._hui_state.fader_touch[c] = False
                S._wiz_step_desc.configure(
                    text=f'Touch (then release) the cap of Fader {c+1}.')
            def _check(c=ch):
                with S._hui_state.lock:
                    return True if S._hui_state.fader_touch[c] else None
            return (label, 'auto', _setup, _check, timeout)

        # ── Helper: v-pot auto-step ────────────────────────────────
        def vpot_auto(idx, name, timeout=12):
            label = f'{name}: Rotate encoder'
            def _setup(i=idx, n=name):
                S._wiz_snapshot[f'vpot_{i}_base'] = None
                S._wiz_step_desc.configure(
                    text=f'Rotate the [{n}] encoder in either direction.')
            def _check(i=idx):
                with S._hui_state.lock:
                    acc = S._hui_state.vpot_acc[i]
                key = f'vpot_{i}_base'
                if S._wiz_snapshot.get(key) is None:
                    S._wiz_snapshot[key] = acc
                    return None
                if abs(acc - S._wiz_snapshot[key]) >= 20:
                    return True
                return None
            return (label, 'auto', _setup, _check, timeout)

        # ── Helper: section divider ────────────────────────────────
        def section(title, desc=''):
            def _setup(t=title, d=desc):
                S._wiz_step_desc.configure(
                    text=d if d else f'Now testing: {t}')
            return (f'[Section] {title}', 'section', _setup, None, 2)

        # ── Helper: grouped button test ────────────────────────────
        # Lights ALL buttons in the group at startup, tracks which are pressed,
        # dims each LED as it's pressed, then on continue reports any missed.
        def btn_group(label, buttons, desc, timeout=45):
            # buttons = list of (zone, port, display_name)
            def _setup(btns=buttons, lbl=label, d=desc):
                S._wiz_snapshot['btn_group_btns']    = btns
                S._wiz_snapshot['btn_group_pressed']  = set()
                S._wiz_snapshot['btn_group_label']    = lbl
                # Record log length so poll can detect taps via new log entries
                with S._hui_state.lock:
                    S._wiz_snapshot['btn_log_start'] = len(S._hui_state.log)
                # Light all buttons in the group
                S._leds_all_off()
                for z, p, _ in btns:
                    try:
                        S.midi.send(HUI.led_on(z, p))
                        S.led_state[z][p] = True
                    except Exception:
                        pass
                S._wiz_step_desc.configure(
                    text=d + '\n\nLit buttons: press each one. '
                             'Its LED will turn off when detected.\n'
                             'Click  Continue  when done (or when timeout).')

            def _poll():
                # Called by the timer — detects newly-pressed buttons and dims them.
                # Checks BOTH currently-held state AND new log entries (catches quick taps).
                if not S._wiz_running:
                    return
                btns      = S._wiz_snapshot.get('btn_group_btns', [])
                pressed   = S._wiz_snapshot.get('btn_group_pressed', set())
                log_start = S._wiz_snapshot.get('btn_log_start', 0)

                with S._hui_state.lock:
                    hw_btns  = dict(S._hui_state.buttons)
                    new_logs = list(S._hui_state.log[log_start:])
                    S._wiz_snapshot['btn_log_start'] = len(S._hui_state.log)

                for z, p, name in btns:
                    key = (z, p)
                    if key in pressed:
                        continue
                    # Detect via held state
                    detected = hw_btns.get(key, False)
                    # Detect via log (catches taps released before this poll)
                    if not detected:
                        zname = ZONE_NAMES.get(z, f'Zone {z:02X}').lower()
                        pname = PORT_NAMES.get(z, {}).get(p, '').lower()
                        full_pat = f'{zname} \u00b7 {pname}'  # e.g. 'cursor / mode / scrub · down'
                        for decoded, _ in new_logs:
                            if decoded and 'pressed' in decoded.lower() and full_pat in decoded.lower():
                                detected = True
                                break
                    if detected:
                        pressed.add(key)
                        # No-LED buttons: numpad and cursor arrows — beep instead
                        if z in (0x13, 0x14, 0x15) or (z == 0x0D and p in (0, 1, 3, 4)):
                            def _beep():
                                import time
                                S.midi.send(HUI.led_on(0x1D, 3))
                                time.sleep(0.06)
                                S.midi.send(HUI.led_off(0x1D, 3))
                            threading.Thread(target=_beep, daemon=True).start()
                        else:
                            S.midi.send(HUI.led_off(z, p))
                            try: S.led_state[z][p] = False
                            except Exception: pass
                S._wiz_snapshot['btn_group_pressed'] = pressed

                # Auto-continue when every button in the group has been pressed
                btns_set = {(z, p) for z, p, _ in btns}
                if btns_set and btns_set.issubset(pressed):
                    S._wiz_log_write('    All buttons detected — auto-advancing.\n', 'pass')
                    # Cancel the tick countdown and advance immediately
                    if getattr(S, '_wiz_tick_id', None):
                        S.root.after_cancel(S._wiz_tick_id)
                        S._wiz_tick_id = None
                    if getattr(S, '_wiz_cancel_after', None):
                        S.root.after_cancel(S._wiz_cancel_after)
                        S._wiz_cancel_after = None
                    S._wiz_snapshot['btn_group_poll_fn'] = None   # stop further polls
                    finish_fn = S._wiz_snapshot.get('btn_group_finish_fn')
                    if finish_fn:
                        finish_fn()
                    S._wiz_step_idx += 1
                    S.root.after(400, S._wiz_advance)

            def _on_continue(lbl=label, btns=buttons):
                # Called when user hits Continue/Pass — evaluate results
                pressed = S._wiz_snapshot.get('btn_group_pressed', set())
                missing = [(z, p, n) for z, p, n in btns if (z, p) not in pressed]
                if missing:
                    S._wiz_record(lbl, False,
                        f'{len(pressed)}/{len(btns)} pressed')
                    for z, p, n in missing:
                        S._wiz_log_write(
                            f'    \u2716  Not pressed: {n}  (zone {z:02X} port {p})\n',
                            'fail')
                else:
                    S._wiz_record(lbl, True, f'All {len(btns)} pressed')
                # Turn off any remaining lit LEDs
                for z, p, _ in btns:
                    S.midi.send(HUI.led_off(z, p))
                    try: S.led_state[z][p] = False
                    except Exception: pass

            # Build a manual step where the engine shows Pass/Continue buttons,
            # but we intercept and use _on_continue to record the real result
            def _wrapped_setup(s=_setup, p=_poll, oc=_on_continue):
                s()
                # Poll for button presses every 150 ms during the manual wait
                S._wiz_snapshot['btn_group_poll_fn'] = p
                S._wiz_snapshot['btn_group_finish_fn'] = oc
                S._btn_group_poll()

            return (label, 'btn_group', _wrapped_setup, None, timeout)

        # ════════════════════════════════════════════════════════════
        return [

            # ── Connection ───────────────────────────────────────────
            ('Connection: Ping', 'auto',
             S._wiz_setup_ping, S._wiz_check_ping, 5),

            # ── Faders: move + touch ─────────────────────────────────
            section('Faders — Move & Touch',
                    'Move and touch each fader cap. '
                    'The LEDs above each strip will flash to show which to test.'),
            fader_move(0), fader_touch(0),
            fader_move(1), fader_touch(1),
            fader_move(2), fader_touch(2),
            fader_move(3), fader_touch(3),
            fader_move(4), fader_touch(4),
            fader_move(5), fader_touch(5),
            fader_move(6), fader_touch(6),
            fader_move(7), fader_touch(7),

            # ── V-pots: rotate ───────────────────────────────────────
            section('V-Pots — Rotate encoders',
                    'Rotate each encoder in either direction.'),
            vpot_auto(0,  'Ch 1 V-Pot'), vpot_auto(1,  'Ch 2 V-Pot'),
            vpot_auto(2,  'Ch 3 V-Pot'), vpot_auto(3,  'Ch 4 V-Pot'),
            vpot_auto(4,  'Ch 5 V-Pot'), vpot_auto(5,  'Ch 6 V-Pot'),
            vpot_auto(6,  'Ch 7 V-Pot'), vpot_auto(7,  'Ch 8 V-Pot'),
            vpot_auto(8,  'Assign Pot 1'), vpot_auto(9,  'Assign Pot 2'),
            vpot_auto(10, 'Assign Pot 3'), vpot_auto(11, 'Assign Pot 4'),
            vpot_auto(12, 'Scroll Encoder'),

            # ── Jog wheel ────────────────────────────────────────────
            section('Jog Wheel'),
            ('Jog Wheel: Rotate CW', 'auto',
             S._wiz_setup_jog, S._wiz_check_jog, 10),

            # ── Buttons ──────────────────────────────────────────────
            section('Buttons',
                    'Press every lit button in each group. '
                    'The LED will turn off as each button is detected.'),

            # ── Channel strip buttons (grouped per channel) ──────────
            btn_group('Channel Strip Buttons — Ch 1',
                [(0x00,1,'Ch1 Select'),(0x00,2,'Ch1 Mute'),(0x00,3,'Ch1 Solo'),
                 (0x00,4,'Ch1 Auto'),(0x00,5,'Ch1 V-Sel'),(0x00,6,'Ch1 Insert'),
                 (0x00,7,'Ch1 Rec/Rdy')],
                'Press every lit button on Channel 1.'),

            btn_group('Channel Strip Buttons — Ch 2',
                [(0x01,1,'Ch2 Select'),(0x01,2,'Ch2 Mute'),(0x01,3,'Ch2 Solo'),
                 (0x01,4,'Ch2 Auto'),(0x01,5,'Ch2 V-Sel'),(0x01,6,'Ch2 Insert'),
                 (0x01,7,'Ch2 Rec/Rdy')],
                'Press every lit button on Channel 2.'),

            btn_group('Channel Strip Buttons — Ch 3–4',
                [(0x02,1,'Ch3 Select'),(0x02,2,'Ch3 Mute'),(0x02,3,'Ch3 Solo'),
                 (0x02,4,'Ch3 Auto'),(0x02,5,'Ch3 V-Sel'),(0x02,6,'Ch3 Insert'),
                 (0x02,7,'Ch3 Rec/Rdy'),
                 (0x03,1,'Ch4 Select'),(0x03,2,'Ch4 Mute'),(0x03,3,'Ch4 Solo'),
                 (0x03,4,'Ch4 Auto'),(0x03,5,'Ch4 V-Sel'),(0x03,6,'Ch4 Insert'),
                 (0x03,7,'Ch4 Rec/Rdy')],
                'Press every lit button on Channels 3 and 4.'),

            btn_group('Channel Strip Buttons — Ch 5–6',
                [(0x04,1,'Ch5 Select'),(0x04,2,'Ch5 Mute'),(0x04,3,'Ch5 Solo'),
                 (0x04,4,'Ch5 Auto'),(0x04,5,'Ch5 V-Sel'),(0x04,6,'Ch5 Insert'),
                 (0x04,7,'Ch5 Rec/Rdy'),
                 (0x05,1,'Ch6 Select'),(0x05,2,'Ch6 Mute'),(0x05,3,'Ch6 Solo'),
                 (0x05,4,'Ch6 Auto'),(0x05,5,'Ch6 V-Sel'),(0x05,6,'Ch6 Insert'),
                 (0x05,7,'Ch6 Rec/Rdy')],
                'Press every lit button on Channels 5 and 6.'),

            btn_group('Channel Strip Buttons — Ch 7–8',
                [(0x06,1,'Ch7 Select'),(0x06,2,'Ch7 Mute'),(0x06,3,'Ch7 Solo'),
                 (0x06,4,'Ch7 Auto'),(0x06,5,'Ch7 V-Sel'),(0x06,6,'Ch7 Insert'),
                 (0x06,7,'Ch7 Rec/Rdy'),
                 (0x07,1,'Ch8 Select'),(0x07,2,'Ch8 Mute'),(0x07,3,'Ch8 Solo'),
                 (0x07,4,'Ch8 Auto'),(0x07,5,'Ch8 V-Sel'),(0x07,6,'Ch8 Insert'),
                 (0x07,7,'Ch8 Rec/Rdy')],
                'Press every lit button on Channels 7 and 8.'),

            # ── KB Shortcuts ─────────────────────────────────────────
            btn_group('Keyboard Shortcuts',
                [(0x08,0,'CTL'),(0x08,1,'SHF'),(0x08,2,'EMD'),(0x08,3,'UND'),
                 (0x08,4,'ALT'),(0x08,5,'OPT'),(0x08,6,'ETL'),(0x08,7,'SAV')],
                'Press all Keyboard Shortcuts buttons.'),

            # ── Window ───────────────────────────────────────────────
            btn_group('Window Buttons',
                [(0x09,0,'Mix'),(0x09,1,'Edit'),(0x09,2,'Transp'),
                 (0x09,3,'Mem-Loc'),(0x09,4,'Status'),(0x09,5,'Alt')],
                'Press all Window buttons.'),

            # ── Channel/Bank scroll ──────────────────────────────────
            btn_group('Channel / Bank Scroll',
                [(0x0A,0,'← Chnl'),(0x0A,1,'← Bank'),
                 (0x0A,2,'Chnl →'),(0x0A,3,'Bank →')],
                'Press all four scroll arrow buttons.'),

            # ── Assignment 1 ─────────────────────────────────────────
            btn_group('Assignment 1',
                [(0x0B,0,'Output'),(0x0B,1,'Input'),(0x0B,2,'Pan'),
                 (0x0B,3,'Send E'),(0x0B,4,'Send D'),(0x0B,5,'Send C'),
                 (0x0B,6,'Send B'),(0x0B,7,'Send A')],
                'Press all Assignment 1 buttons.'),

            # ── Assignment 2 ─────────────────────────────────────────
            btn_group('Assignment 2',
                [(0x0C,0,'Assign'),(0x0C,1,'Default'),(0x0C,2,'Suspend'),
                 (0x0C,3,'Shift'),(0x0C,4,'Mute'),(0x0C,5,'Bypass'),
                 (0x0C,6,'Rec All')],
                'Press all Assignment 2 buttons.'),

            # ── Cursor / Mode / Scrub ────────────────────────────────
            btn_group('Cursor / Mode / Scrub',
                [(0x0D,0,'Down'),(0x0D,1,'Left'),(0x0D,2,'Mode'),
                 (0x0D,3,'Right'),(0x0D,4,'Up'),(0x0D,5,'Scrub'),
                 (0x0D,6,'Shuttle')],
                'Press all Cursor / Mode / Scrub buttons.\n'
                'Note: Down, Left, Right and Up have no LEDs — '
                'a short beep will confirm each of those four.'),

            # ── Transport ────────────────────────────────────────────
            btn_group('Transport',
                [(0x0E,0,'Talkback'),(0x0E,1,'Rewind'),(0x0E,2,'Fast Fwd'),
                 (0x0E,3,'Stop'),(0x0E,4,'Play'),(0x0E,5,'Record')],
                'Press all main Transport buttons.'),

            # ── Transport loop/RTZ ───────────────────────────────────
            btn_group('Transport — Loop / RTZ',
                [(0x0F,0,'RTZ'),(0x0F,1,'End'),(0x0F,2,'On Line'),
                 (0x0F,3,'Loop'),(0x0F,4,'Qck Punch')],
                'Press all Loop / RTZ buttons.'),

            # ── Transport punch ──────────────────────────────────────
            btn_group('Transport — Punch / Audition',
                [(0x10,0,'Audition'),(0x10,1,'Pre'),(0x10,2,'In'),
                 (0x10,3,'Out'),(0x10,4,'Post')],
                'Press all Punch buttons.'),

            # ── Num Pad — specified order ────────────────────────────
            # 0, ., Ent, 1, 2, 3, 4, 5, 6, +, 7, 8, 9, -, CLR, =, /, *
            btn_group('Numeric Keypad',
                [(0x13,0,'0'),  (0x13,5,'.'),   (0x14,0,'Enter'),
                 (0x13,1,'1'),  (0x13,3,'2'),   (0x13,6,'3'),
                 (0x13,2,'4'),  (0x13,4,'5'),   (0x13,7,'6'),
                 (0x14,1,'+'),
                 (0x15,0,'7'),  (0x15,1,'8'),   (0x15,2,'9'),
                 (0x15,3,'-'),  (0x15,4,'CLR'), (0x15,5,'='),
                 (0x15,6,'/'),  (0x15,7,'*')],
                'Press all numeric keypad buttons in the order shown above the faders.\n'
                'Note: the keypad has no LEDs — a short beep will confirm each keypress.'),

            # ── Auto Enable ──────────────────────────────────────────
            btn_group('Auto Enable',
                [(0x17,0,'Plug-in'),(0x17,1,'Pan'),(0x17,2,'Fader'),
                 (0x17,3,'Snd Mute'),(0x17,4,'Send'),(0x17,5,'Mute')],
                'Press all Auto Enable buttons.'),

            # ── Auto Mode ────────────────────────────────────────────
            btn_group('Auto Mode',
                [(0x18,0,'Trim'),(0x18,1,'Latch'),(0x18,2,'Read'),
                 (0x18,3,'Off'),(0x18,4,'Write'),(0x18,5,'Touch')],
                'Press all Auto Mode buttons.'),

            # ── Status / Group ───────────────────────────────────────
            btn_group('Status / Group',
                [(0x19,0,'Phase'),(0x19,1,'Monitor'),(0x19,2,'Auto'),
                 (0x19,3,'Suspend'),(0x19,4,'Create'),(0x19,5,'Group')],
                'Press all Status / Group buttons.'),

            # ── Edit ─────────────────────────────────────────────────
            btn_group('Edit',
                [(0x1A,0,'Paste'),(0x1A,1,'Cut'),(0x1A,2,'Capture'),
                 (0x1A,3,'Delete'),(0x1A,4,'Copy'),(0x1A,5,'Separate')],
                'Press all Edit buttons.'),

            # ── Function Keys ────────────────────────────────────────
            btn_group('Function Keys',
                [(0x1B,0,'F1'),(0x1B,1,'F2'),(0x1B,2,'F3'),(0x1B,3,'F4'),
                 (0x1B,4,'F5'),(0x1B,5,'F6'),(0x1B,6,'F7'),(0x1B,7,'F8/ESC')],
                'Press all Function Key buttons.'),

            # ── Parameter Edit ───────────────────────────────────────
            btn_group('Parameter Edit',
                [(0x1C,0,'Ins/Para'),(0x1C,1,'Assign'),(0x1C,2,'Select 1'),
                 (0x1C,3,'Select 2'),(0x1C,4,'Select 3'),(0x1C,5,'Select 4'),
                 (0x1C,6,'Bypass'),(0x1C,7,'Compare')],
                'Press all Parameter Edit buttons.'),

            # ── Output Tests ─────────────────────────────────────────
            section('Output Tests',
                    'Each test sends a stimulus to the hardware. '
                    'Confirm visually whether the hardware responded correctly.'),

            # ── Manual: VFD display ──────────────────────────────────
            ('VFD Display: Check main display', 'manual',
             S._wiz_setup_vfd, None, 45),

            # ── Manual: Scribble strips ──────────────────────────────
            ('Scribble Strips: Check channel labels', 'manual',
             S._wiz_setup_scribble, None, 30),

            # ── Manual: All LEDs ─────────────────────────────────────
            ('LEDs: Check all indicators', 'manual',
             S._wiz_setup_leds, None, 30),

            # ── Manual: V-pot rings ──────────────────────────────────
            ('V-Pot Rings: Check all ring LEDs', 'manual',
             S._wiz_setup_vpot_rings, None, 60),

            # ── Manual: VU meters ────────────────────────────────────
            ('VU Meters: Check all segments', 'manual',
             S._wiz_setup_vu, None, 25),

            # ── Ready → manual: Fader motors ─────────────────────────
            ('Fader Motors: Motorised sweep', 'ready',
             S._wiz_setup_motor, None, 25),

            # ── Manual: Timecode ─────────────────────────────────────
            ('Timecode Counter: Check 7-segment display', 'manual',
             S._wiz_setup_timecode, None, 30),

            # ── Manual: Click ────────────────────────────────────────
            ('Click Circuit: Check internal speaker', 'manual',
             S._wiz_setup_click, None, 20),

            # ── Manual: Beeper ───────────────────────────────────────
            ('Beeper: Check beeper output', 'manual',
             S._wiz_setup_beeper, None, 20),

            # ── Manual: Relay 1 ──────────────────────────────────────
            ('Relay 1: Check relay output', 'manual',
             S._wiz_setup_relay1, None, 20),

            # ── Manual: Relay 2 ──────────────────────────────────────
            ('Relay 2: Check relay output', 'manual',
             S._wiz_setup_relay2, None, 20),
        ]

    # ════════════════════════════════════════════════════════
    # WIZARD ENGINE
    # ════════════════════════════════════════════════════════

    def _wiz_start(self):
        if not self.midi.connected:
            self._wiz_log_write('Not connected — please connect first.\n', 'fail')
            return
        if self._wiz_running:
            return
        self._wiz_running  = True
        self._wiz_step_idx = 0
        self._wiz_results  = []
        self._wiz_snapshot = {}
        self._wiz_cancel_after = None
        self._wiz_vfd_stop   = False
        self._wiz_vpot_stop  = False
        self._wiz_tc_stop    = False
        self._wiz_audio_stop = False
        self._wiz_motor_stop = False

        all_steps = self._wiz_build_steps()

        # Filter by section and by individual btn_group
        sec_enabled   = getattr(self, '_wiz_section_enabled', {})
        group_enabled = getattr(self, '_wiz_group_enabled', {})
        cur_sec   = None
        filtered  = []
        keep_sec  = True
        for step in all_steps:
            label, kind = step[0], step[1]
            if kind == 'section':
                cur_sec  = label.replace('[Section] ', '')
                keep_sec = sec_enabled.get(cur_sec, True)
                if keep_sec:
                    filtered.append(step)
            elif not keep_sec:
                pass   # whole section disabled
            elif kind == 'btn_group' and not group_enabled.get(label, True):
                pass   # this specific group disabled
            else:
                filtered.append(step)
        self._wiz_steps = filtered

        bg, fg = self.cfg.bg_color, self.cfg.fg_color
        self._wiz_start_btn.configure(state='disabled', bg=self.cfg.dim_color)
        self._wiz_stop_btn.configure(state='normal', bg='#ff5555', fg='#ffffff')
        self._wiz_log_clear()
        self._wiz_log_write(
            f'Test Wizard started  —  {len(self._wiz_steps)} steps\n', 'head')
        self._wiz_advance()

    def _wiz_open_advanced(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        CHECKED_CLR = '#44cc44'   # visible green for checkbox indicator

        win = tk.Toplevel(self.root)
        win.title('Test Wizard — Step Selection')
        win.configure(bg=bg)
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()

        outer = tk.Frame(win, bg=bg, padx=16, pady=14)
        outer.pack(fill='both', expand=True)
        tk.Label(outer, text='Select which tests to include:',
                 font=self.F10B, bg=bg, fg=fg).pack(anchor='w', pady=(0, 8))

        all_steps = self._wiz_build_steps()
        if not hasattr(self, '_wiz_section_enabled'):
            self._wiz_section_enabled = {}
        if not hasattr(self, '_wiz_group_enabled'):
            self._wiz_group_enabled = {}

        ALWAYS_ON = {'Connection: Ping'}

        # Parse into sections → items
        sections  = []
        cur_sec   = None
        cur_items = []
        for label, kind, *_ in all_steps:
            if kind == 'section':
                if cur_sec is not None:
                    sections.append((cur_sec, cur_items))
                cur_sec   = label.replace('[Section] ', '')
                cur_items = []
            elif label not in ALWAYS_ON:
                cur_items.append((label, kind))
        if cur_sec is not None:
            sections.append((cur_sec, cur_items))

        sec_vars   = {}
        group_vars = {}

        # Two-column grid for sections
        cols_frame = tk.Frame(outer, bg=bg)
        cols_frame.pack(fill='both', expand=True)
        col_frames = [tk.Frame(cols_frame, bg=bg), tk.Frame(cols_frame, bg=bg)]
        col_frames[0].pack(side='left', fill='both', expand=True, padx=(0, 12))
        col_frames[1].pack(side='left', fill='both', expand=True)

        def _make_section_widget(parent, sec_label, items):
            sec_default = self._wiz_section_enabled.get(sec_label, True)
            sv = tk.BooleanVar(value=sec_default)
            sec_vars[sec_label] = sv

            sec_cb = tk.Checkbutton(parent, text=sec_label, variable=sv,
                                    bg=bg, fg=fg, selectcolor=CHECKED_CLR,
                                    activebackground=bg, font=self.F10B,
                                    anchor='w')
            sec_cb.pack(fill='x', pady=(6, 0))

            btn_groups = [(lbl, k) for lbl, k in items if k == 'btn_group']
            if btn_groups:
                sub_frame = tk.Frame(parent, bg=bg, padx=18)
                sub_frame.pack(fill='x')
                sub_labels = {}
                for grp_label, _ in btn_groups:
                    grp_default = self._wiz_group_enabled.get(grp_label, True)
                    gv = tk.BooleanVar(value=grp_default)
                    group_vars[grp_label] = gv
                    row = tk.Frame(sub_frame, bg=bg)
                    row.pack(fill='x')
                    cb = tk.Checkbutton(row, text=grp_label, variable=gv,
                                        bg=bg, fg=dim, selectcolor=CHECKED_CLR,
                                        activebackground=bg, font=self.F9,
                                        anchor='w')
                    cb.pack(side='left')
                    sub_labels[grp_label] = cb

                # When section toggled: update sub-checkbox text colour only
                # (always leave them clickable so user can pre-select)
                def _cascade(sv=sv, cbs=sub_labels):
                    enabled = sv.get()
                    for cb in cbs.values():
                        try:
                            cb.configure(fg=dim if enabled else '#555555')
                        except Exception:
                            pass
                sv.trace_add('write', lambda *_, fn=_cascade: fn())

        # Distribute sections across two columns (fill left first)
        half = (len(sections) + 1) // 2
        for i, (sec_label, items) in enumerate(sections):
            col = 0 if i < half else 1
            _make_section_widget(col_frames[col], sec_label, items)

        # Divider + All/None + Apply/Cancel
        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(12, 8))
        ctrl = tk.Frame(outer, bg=bg)
        ctrl.pack(fill='x')

        def _all():
            for v in sec_vars.values():   v.set(True)
            for v in group_vars.values(): v.set(True)
        def _none():
            for v in sec_vars.values():   v.set(False)
            for v in group_vars.values(): v.set(False)

        self._btn(ctrl, 'Select All',  _all).pack(side='left', padx=(0, 8))
        self._btn(ctrl, 'Select None', _none).pack(side='left')

        def _apply():
            for sec, var in sec_vars.items():
                self._wiz_section_enabled[sec] = var.get()
            for grp, var in group_vars.items():
                self._wiz_group_enabled[grp] = var.get()
            n_sec = sum(1 for v in sec_vars.values() if v.get())
            n_grp = sum(1 for v in group_vars.values() if v.get())
            self._wiz_log_write(
                f'{n_sec} sections, {n_grp} button groups enabled.\n', 'skip')
            win.destroy()

        tk.Button(ctrl, text='Apply', bg=fg, fg=bg,
                  activebackground=fg, activeforeground=bg,
                  font=('Segoe UI', 10, 'bold'), relief='flat', cursor='hand2',
                  command=_apply).pack(side='right')
        tk.Button(ctrl, text='Cancel', bg=bg, fg=dim,
                  activebackground=dim, activeforeground=fg,
                  font=self.F9, relief='flat', cursor='hand2',
                  command=win.destroy).pack(side='right', padx=(0, 8))

        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = min(win.winfo_reqheight(), int(win.winfo_screenheight() * 0.85))
        x = self.root.winfo_x() + (self.root.winfo_width()  - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        win.geometry(f'{max(w, 640)}x{h}+{x}+{y}')

    def _wiz_stop(self):
        self._wiz_running = False
        if getattr(self, '_wiz_cancel_after', None):
            self.root.after_cancel(self._wiz_cancel_after)
            self._wiz_cancel_after = None
        self._wiz_finish(aborted=True)

    def _wiz_advance(self):
        if not self._wiz_running:
            return
        if self._wiz_step_idx >= len(self._wiz_steps):
            self._wiz_finish(aborted=False)
            return

        label, kind, setup_fn, check_fn, timeout = \
            self._wiz_steps[self._wiz_step_idx]

        # Progress bar
        total  = len(self._wiz_steps)
        done   = self._wiz_step_idx
        self._wiz_prog_lbl.configure(
            text=f'Step {done+1} of {total}  —  {label}')
        bar_w  = self._wiz_prog_bar.winfo_width()
        fill_w = int(bar_w * done / total) if total else 0
        self._wiz_prog_bar.coords(self._wiz_prog_fill, 0, 0, fill_w, 10)

        # Reset snapshot BEFORE setup so setup can write baselines
        self._wiz_snapshot   = {}
        self._wiz_ready_phase2 = False   # reset two-phase ready flag
        self._wiz_step_title.configure(text=label)
        self._wiz_step_desc.configure(text='')

        # Button visibility
        if kind == 'section':
            self._wiz_pass_btn.pack_forget()
            self._wiz_fail_btn.pack_forget()
            self._wiz_timeout_lbl.pack_forget()
        elif kind in ('manual', 'ready', 'btn_group'):
            if kind == 'btn_group':
                self._wiz_pass_btn.configure(text='  \u25b6  Continue  ')
            elif kind == 'ready':
                self._wiz_pass_btn.configure(text='  \u25b6  I\'m Ready  ')
            else:
                self._wiz_pass_btn.configure(text='  \u2714  Pass  ')
            self._wiz_pass_btn.pack(side='left', padx=(0, 8))
            if kind == 'manual':
                self._wiz_fail_btn.pack(side='left')
            else:
                self._wiz_fail_btn.pack_forget()
            self._wiz_timeout_lbl.pack(side='left', padx=(16, 0))
        else:
            self._wiz_pass_btn.pack_forget()
            self._wiz_fail_btn.pack_forget()
            self._wiz_timeout_lbl.pack_forget()

        # Write current step to VFD — skip during the VFD test (it writes its own content)
        VFD_TEST_LABEL = 'VFD Display: Check main display'
        if self.midi.connected and label != VFD_TEST_LABEL and kind != 'section':
            def _vfd_safe(text):
                """Strip wizard prefix and replace non-ASCII with safe equivalents."""
                text = text.replace('[Section] ', '')
                text = text.replace('\u2014', ' - ')   # em dash → ' - '
                text = text.replace('\u2013', '-')      # en dash → '-'
                text = text.replace('\u2026', '...')    # ellipsis → '...'
                return text

            _vfd_hints = {
                'auto':      'Waiting for hardware...',
                'manual':    'Inspect & confirm',
                'ready':     'Click I\'m Ready when safe',
                'btn_group': 'Press each lit button',
            }
            row1 = (_vfd_safe(label) + ' ' * 40)[:40]
            row2 = (_vfd_hints.get(kind, '') + ' ' * 40)[:40]
            self._wiz_vfd_row2 = row2   # saved so _wiz_record can append result
            for z in range(4):
                self._vfd_send_zone(z,     row1[z*10:(z+1)*10])
                self._vfd_send_zone(z + 4, row2[z*10:(z+1)*10])
        else:
            self._wiz_vfd_row2 = ' ' * 40

        # Run setup
        try:
            if setup_fn:
                setup_fn()
        except Exception as e:
            self._wiz_record(label, None, f'Setup error: {e}')
            self._wiz_step_idx += 1
            self.root.after(300, self._wiz_advance)
            return

        # Fallback desc for auto steps
        if kind == 'auto' and not self._wiz_step_desc.cget('text'):
            self._wiz_step_desc.configure(
                text=f'Waiting for hardware input…  (timeout: {timeout}s)')

        # Dispatch
        if kind == 'section':
            # Auto-advance after 2 s without recording a result
            self._wiz_cancel_after = self.root.after(
                2000, self._wiz_advance_section)
        elif kind in ('manual', 'ready', 'btn_group'):
            # Motor ('ready') step must never auto-skip
            self._wiz_no_autoskip = (kind == 'ready')
            self._wiz_start_timeout(label, timeout)
        elif kind == 'auto':
            # Delay first poll by 300 ms so hardware state settles after setup
            self._wiz_cancel_after = self.root.after(
                300, self._wiz_poll_auto, label, check_fn, timeout, 0)

    def _wiz_advance_section(self):
        """Move past a section divider without recording a result."""
        if not self._wiz_running:
            return
        self._wiz_step_idx += 1
        self._wiz_advance()

    def _wiz_poll_auto(self, label, check_fn, timeout, elapsed_ms):
        if not self._wiz_running:
            return
        try:
            result = check_fn()
        except Exception:
            result = None   # don't fail on poll exception — keep waiting

        if result is True:
            self._wiz_record(label, True)
            self._leds_all_off()
            self._wiz_step_idx += 1
            self._wiz_cancel_after = self.root.after(900, self._wiz_advance)
            return
        if elapsed_ms >= timeout * 1000:
            self._wiz_record(label, None)   # timeout = skip, not fail
            self._leds_all_off()
            self._wiz_step_idx += 1
            self._wiz_cancel_after = self.root.after(900, self._wiz_advance)
            return

        self._wiz_cancel_after = self.root.after(
            100, self._wiz_poll_auto, label, check_fn, timeout, elapsed_ms + 100)

    def _wiz_start_timeout(self, label, timeout):
        """Start countdown for a manual/btn_group step."""
        self._wiz_manual_label   = label
        self._wiz_timeout_remain = timeout
        # Use a SEPARATE id so poll_auto can't clobber it
        if getattr(self, '_wiz_tick_id', None):
            self.root.after_cancel(self._wiz_tick_id)
        self._wiz_tick_id = self.root.after(1000, self._wiz_tick_timeout)
        self._wiz_timeout_lbl.configure(text=f'Auto-skip in {timeout}s')

    def _wiz_tick_timeout(self):
        if not self._wiz_running:
            return
        self._wiz_timeout_remain -= 1
        remain = self._wiz_timeout_remain

        # Check auto-skip setting and whether this step forbids it
        autoskip = getattr(self, '_wiz_autoskip_var', None)
        step_forbids = getattr(self, '_wiz_no_autoskip', False)
        will_skip = (autoskip is None or autoskip.get()) and not step_forbids

        if will_skip:
            self._wiz_timeout_lbl.configure(text=f'Auto-skip in {remain}s')
        else:
            self._wiz_timeout_lbl.configure(text='Waiting for response\u2026')

        if remain <= 0 and will_skip:
            self._wiz_tick_id = None
            self._wiz_manual_result(None)
            return
        self._wiz_tick_id = self.root.after(1000, self._wiz_tick_timeout)

    def _wiz_manual_result(self, passed):
        # Cancel tick chain with its own id
        if getattr(self, '_wiz_tick_id', None):
            self.root.after_cancel(self._wiz_tick_id)
            self._wiz_tick_id = None
        # Cancel poll chain
        if getattr(self, '_wiz_cancel_after', None):
            self.root.after_cancel(self._wiz_cancel_after)
            self._wiz_cancel_after = None

        label, kind, setup_fn, check_fn, timeout = \
            self._wiz_steps[self._wiz_step_idx]

        if kind == 'btn_group':
            finish_fn = self._wiz_snapshot.get('btn_group_finish_fn')
            if finish_fn:
                finish_fn()
            self._wiz_step_idx += 1
            self.root.after(800, self._wiz_cleanup_step)
            self.root.after(1100, self._wiz_advance)
            return

        if kind == 'ready' and passed is True and not getattr(self, '_wiz_ready_phase2', False):
            # "I'm Ready" clicked — fire phase 2 (sweep), then show Pass/Fail
            self._wiz_ready_phase2 = True
            try:
                if setup_fn:
                    setup_fn()
            except Exception:
                pass
            if check_fn is None:
                self._wiz_pass_btn.configure(text='  \u2714  Pass  ')
                self._wiz_fail_btn.pack(side='left')
                self._wiz_no_autoskip = False
                self._wiz_start_timeout(self._wiz_manual_label, timeout)
            else:
                self._wiz_cancel_after = self.root.after(
                    200, self._wiz_poll_auto, label, check_fn, timeout, 0)
            return

        self._wiz_record(self._wiz_manual_label, passed)
        self._wiz_step_idx += 1
        self.root.after(800, self._wiz_cleanup_step)
        self.root.after(1100, self._wiz_advance)

    def _wiz_cleanup_step(self):
        self._wiz_vfd_stop   = True
        self._wiz_vpot_stop  = True
        self._wiz_tc_stop    = True
        self._wiz_audio_stop = True
        self._wiz_motor_stop = True
        self._leds_all_off()
        for i in range(8):
            self.midi.send(HUI.vu(i, 0, 0))
            self.midi.send(HUI.vu(i, 1, 0))
        for i in range(13):
            self.midi.send(HUI.vpot(i, 0x00))
        for z in range(8):
            self._vfd_send_zone(z, ' ' * 10)
        for i in range(9):
            self.midi.send(HUI.scribble(i, '    '))

    def _wiz_record(self, label, passed, note=''):
        self._wiz_results.append((label, passed))
        sym  = '\u2714  PASS' if passed is True else \
               ('\u2716  FAIL' if passed is False else '\u2015  SKIP')
        tag  = 'pass' if passed is True else \
               ('fail' if passed is False else 'skip')
        line = f'  {sym}  {label}'
        if note:
            line += f'  ({note})'
        self._wiz_log_write(line + '\n', tag)

        # Flash result word on rightmost part of VFD row 2
        if self.midi.connected:
            word = 'PASS' if passed is True else ('FAIL' if passed is False else 'SKIP')
            row2 = getattr(self, '_wiz_vfd_row2', ' ' * 40)
            # Place result word at chars 36–39, preserving chars 30–35
            zone7 = (row2[30:36] + word)[:10]
            self._vfd_send_zone(7, zone7)

    def _wiz_finish(self, aborted=False):
        self._wiz_running = False
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        self._wiz_start_btn.configure(state='normal', bg=fg, fg=bg)
        self._wiz_stop_btn.configure(state='disabled', bg=dim, fg=fg)
        self._wiz_pass_btn.pack_forget()
        self._wiz_fail_btn.pack_forget()
        self._wiz_timeout_lbl.pack_forget()
        bar_w = self._wiz_prog_bar.winfo_width()
        self._wiz_prog_bar.coords(self._wiz_prog_fill, 0, 0, bar_w, 10)

        if aborted:
            self._wiz_step_title.configure(text='Wizard stopped.')
            self._wiz_step_desc.configure(text='')
            self._wiz_log_write('\n— Wizard stopped by user —\n', 'skip')
            self._wiz_prog_lbl.configure(text='Stopped.')
            self.root.after(200, self._wiz_cleanup_step)
            return

        n_pass = sum(1 for _, p in self._wiz_results if p is True)
        n_fail = sum(1 for _, p in self._wiz_results if p is False)
        n_skip = sum(1 for _, p in self._wiz_results if p is None)
        total  = len(self._wiz_results)
        self._wiz_step_title.configure(text='Complete!')
        self._wiz_step_desc.configure(
            text=f'{n_pass}/{total} passed  \u00b7  {n_fail} failed  \u00b7  {n_skip} skipped')
        self._wiz_prog_lbl.configure(
            text=f'Done \u2014 {n_pass} passed, {n_fail} failed, {n_skip} skipped')
        self._wiz_log_write(
            f'\n{"─"*46}\n'
            f'  Result: {n_pass} passed  {n_fail} failed  {n_skip} skipped\n', 'head')
        self.root.after(300, self._reset_hardware)

    def _wiz_log_write(self, text, tag=''):
        self._wiz_log.configure(state='normal')
        if tag:
            self._wiz_log.insert('end', text, tag)
        else:
            self._wiz_log.insert('end', text)
        self._wiz_log.see('end')
        self._wiz_log.configure(state='disabled')

    def _wiz_log_clear(self):
        self._wiz_log.configure(state='normal')
        self._wiz_log.delete('1.0', 'end')
        self._wiz_log.configure(state='disabled')

    def _wiz_export(self):
        import os, datetime
        from tkinter import filedialog
        ts = datetime.datetime.now().strftime('%Y-%m-%d  %H:%M:%S')
        default_name = 'HUI_Test_Report_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.txt'
        path = filedialog.asksaveasfilename(
            parent        = self.root,
            title         = 'Save Test Report',
            initialdir    = _DIR,
            initialfile   = default_name,
            defaultextension = '.txt',
            filetypes     = [('Text files', '*.txt'), ('All files', '*.*')],
        )
        if not path:
            return   # user cancelled
        lines = [f'HUI-Test  Automated Test Report  —  {ts}\n',
                 '=' * 52 + '\n']
        for label, passed in self._wiz_results:
            sym = 'PASS' if passed is True else ('FAIL' if passed is False else 'SKIP')
            lines.append(f'{sym:4s}  {label}\n')
        n_p = sum(1 for _, p in self._wiz_results if p is True)
        n_f = sum(1 for _, p in self._wiz_results if p is False)
        n_s = sum(1 for _, p in self._wiz_results if p is None)
        lines += ['=' * 52 + '\n',
                  f'{n_p} passed  {n_f} failed  {n_s} skipped\n']
        try:
            with open(path, 'w') as f:
                f.writelines(lines)
            self._wiz_log_write(f'Report saved to {path}\n', 'skip')
        except Exception as e:
            self._wiz_log_write(f'Could not save report: {e}\n', 'fail')

    # ════════════════════════════════════════════════════════
    # WIZARD FLASH HELPER
    # ════════════════════════════════════════════════════════

    def _btn_group_poll(self):
        """Poll for button presses during a btn_group step (every 150ms)."""
        if not self._wiz_running:
            return
        poll_fn = self._wiz_snapshot.get('btn_group_poll_fn')
        if poll_fn:
            try:
                poll_fn()
            except Exception:
                pass
        self._wiz_cancel_after = self.root.after(150, self._btn_group_poll)

    def _wiz_flash_zone(self, zone):
        """Flash zone LEDs 3× on a background thread — slower, more visible."""
        self._leds_all_off()
        def _do(z=zone):
            import time
            ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
            for _ in range(3):
                for p in ports:
                    self.midi.send(HUI.led_on(z, p))
                time.sleep(0.30)
                for p in ports:
                    self.midi.send(HUI.led_off(z, p))
                time.sleep(0.22)
        threading.Thread(target=_do, daemon=True).start()

    # ════════════════════════════════════════════════════════
    # WIZARD STEP IMPLEMENTATIONS
    # ════════════════════════════════════════════════════════

    def _wiz_setup_ping(self):
        with self._hui_state.lock:
            self._hui_state.ping_flag = False
        self.midi.send(HUI.ping())
        self._wiz_step_desc.configure(
            text='Sending a ping to the HUI.\n'
                 'The hardware should reply within 2 seconds if online.')

    def _wiz_check_ping(self):
        with self._hui_state.lock:
            pf = self._hui_state.ping_flag
        if pf:
            with self._hui_state.lock:
                self._hui_state.ping_flag = False
            return True
        return None

    def _wiz_setup_jog(self):
        with self._hui_state.lock:
            self._wiz_snapshot['jog_base'] = self._hui_state.jog_total
        self._wiz_step_desc.configure(
            text='Rotate the jog wheel clockwise by at least 3 clicks.')

    def _wiz_check_jog(self):
        with self._hui_state.lock:
            total = self._hui_state.jog_total
        if total > self._wiz_snapshot.get('jog_base', total) + 3:
            return True
        return None

    # ── Manual step setups ───────────────────────────────────

    def _wiz_setup_vfd(self):
        self._wiz_vfd_stop = True

        pattern = 'HUI-Test**'
        row1 = (pattern * 10)[:40]   # 40 chars, no spaces
        row2 = (pattern * 10)[:40]
        for z in range(4):
            self._vfd_send_zone(z,     row1[z*10:(z+1)*10])
            self._vfd_send_zone(z + 4, row2[z*10:(z+1)*10])

        self._wiz_step_desc.configure(
            text='Both rows of the VFD are now fully populated with "HUI-Test**" repeating '
                 '— all 80 character positions, no gaps.\n\n'
                 'Click  Pass  if both rows are fully lit end-to-end with no blank positions.\n'
                 'Click  Fail  if any portion is blank or garbled.')

    def _wiz_setup_scribble(self):
        labels = ['TST1', 'TST2', 'TST3', 'TST4', 'TST5', 'TST6', 'TST7', 'TST8', 'TST9']
        for i, lbl in enumerate(labels):
            self.midi.send(HUI.scribble(i, lbl))
        self._wiz_step_desc.configure(
            text='Look at the 9 scribble-strip displays above the faders.\n'
                 'They should show TST1, TST2 … TST9 from left to right.\n'
                 'Click Pass if all are readable, Fail if any are blank or wrong.')

    def _wiz_setup_leds(self):
        self._leds_all_on()
        self._wiz_step_desc.configure(
            text='All LEDs on the surface are now lit.\n'
                 'Scan the entire surface for any LEDs that failed to illuminate.\n'
                 'Click Pass if all lit, Fail if any were dark.')

    def _wiz_setup_vpot_rings(self):
        self._wiz_vpot_stop = True   # stop any running animation

        # Mode 2 (level/fill-from-left) at position 11 lights all 11 ring LEDs.
        # Adding 0x40 turns on the centre indicator LED as well.
        # Value = (mode << 4) | position | 0x40 = 0x20 | 11 | 0x40 = 0x6B
        for pot in range(13):
            self.midi.send(HUI.vpot(pot, 0x6B))   # all ring LEDs + centre on

        self._wiz_step_desc.configure(
            text='All 13 V-pot rings are now fully lit — all 11 LEDs and the centre indicator.\n\n'
                 'Inspect each ring on the surface. All dots should be on, '
                 'including the small centre LED beneath each knob.\n\n'
                 'Click  Pass  if every LED on every ring is lit.\n'
                 'Click  Fail  if any LEDs are dark or missing.')

    def _wiz_setup_vu(self):
        for i in range(8):
            self.midi.send(HUI.vu(i, 0, 12))
            self.midi.send(HUI.vu(i, 1, 12))
        self._wiz_step_desc.configure(
            text='All VU meter columns (L + R for channels 1–8) are now at full clip (12).\n'
                 'Check that all 16 columns show all segments lit, including the red clip LED.\n'
                 'Click Pass if all segments lit, Fail if any column is missing segments.')

    def _wiz_setup_motor(self):
        if not self._wiz_snapshot.get('motor_warned'):
            # Phase 1: warning — user must click Ready before motors move
            self._wiz_snapshot['motor_warned'] = True
            self._wiz_step_desc.configure(
                text='⚠  All 8 motorised faders are about to sweep bottom → top → bottom.\n\n'
                     'Make sure nothing is resting on or obstructing any fader cap!\n\n'
                     'Click  ▶ I\'m Ready  when it is safe to proceed.')
        else:
            # Phase 2: sweep fires, user watches and confirms
            self._wiz_step_desc.configure(
                text='Faders are sweeping bottom → top → bottom.\n'
                     'Watch that all 8 move smoothly and reach both ends of travel.\n\n'
                     'Click  Pass  if all faders moved correctly.\n'
                     'Click  Fail  if any fader was stuck or did not complete the sweep.')
            # Show status on VFD while sweep runs
            self._wiz_motor_stop = False
            msg = 'Faders moving, please wait...'
            row1 = (msg + ' ' * 40)[:40]
            for z in range(4):
                self._vfd_send_zone(z,     row1[z*10:(z+1)*10])
                self._vfd_send_zone(z + 4, ' ' * 10)
            def _on_sweep_done():
                if not self._wiz_motor_stop:
                    done = 'Sweep done -- confirm Pass or Fail'
                    r = (done + ' ' * 40)[:40]
                    for z in range(4):
                        self._vfd_send_zone(z,     r[z*10:(z+1)*10])
                        self._vfd_send_zone(z + 4, ' ' * 10)
            def _sweep():
                import time
                for pos in [0, 16383, 0]:
                    if self._wiz_motor_stop:
                        break
                    for i in range(8):
                        self.midi.send(HUI.fader(i, pos))
                    time.sleep(1.5)
                self.root.after(0, _on_sweep_done)
            threading.Thread(target=_sweep, daemon=True).start()

    def _wiz_setup_timecode(self):
        self._wiz_tc_stop = False
        self._wiz_step_desc.configure(
            text='Each digit of the timecode display is cycling through 0–9 '
                 'independently, like a slot machine.\n'
                 'All 8 digits should be visibly changing.\n\n'
                 'Click  Pass  if all digits are cycling, '
                 'Fail  if any digit is stuck, dark, or missing segments.')
        def _cycle():
            import time
            speeds   = [7, 11, 13, 17, 19, 23, 29, 31]
            counters = [0] * 8
            while not self._wiz_tc_stop:
                for i in range(8):
                    counters[i] = (counters[i] + 1) % (speeds[i] * 10)
                d  = [c // s for c, s in zip(counters, speeds)]
                hh = d[0] * 10 + d[1]
                mm = d[2] * 10 + d[3]
                ss = d[4] * 10 + d[5]
                ff = d[6] * 10 + d[7]
                self.midi.send(HUI.timecode(hh % 24, mm % 60, ss % 60, ff % 100))
                time.sleep(0.05)
            self.midi.send(HUI.timecode(0, 0, 0, 0))
        threading.Thread(target=_cycle, daemon=True).start()

    def _wiz_setup_click(self):
        self._wiz_audio_stop = False
        self._wiz_step_desc.configure(
            text='The click circuit will fire repeatedly for 5 seconds.\n'
                 'Listen for a rapid series of clicks from the HUI speaker.\n'
                 'Click Pass if you heard clicks, Fail if the speaker was silent.')
        def _clicks():
            import time
            for _ in range(20):
                if self._wiz_audio_stop:
                    break
                self.midi.send([
                    mido.Message('control_change', channel=0, control=0x0C, value=0x1D),
                    mido.Message('control_change', channel=0, control=0x2C, value=0x42),
                ])
                time.sleep(0.25)
        threading.Thread(target=_clicks, daemon=True).start()

    def _wiz_setup_beeper(self):
        self._wiz_audio_stop = False
        self._wiz_step_desc.configure(
            text='The beeper will sound 5 times (500 ms on, 500 ms off).\n'
                 'Listen for 5 distinct beep pulses from the HUI.\n'
                 'Click  Pass  if you heard beeps, Fail  if silent.')
        def _beep():
            import time
            for _ in range(5):
                if self._wiz_audio_stop:
                    break
                self.midi.send(HUI.led_on(0x1D, 3))
                for _ in range(5):   # 500 ms in 100 ms slices so stop is responsive
                    if self._wiz_audio_stop:
                        break
                    time.sleep(0.1)
                self.midi.send(HUI.led_off(0x1D, 3))
                for _ in range(5):
                    if self._wiz_audio_stop:
                        break
                    time.sleep(0.1)
            self.midi.send(HUI.led_off(0x1D, 3))   # ensure off on exit
        threading.Thread(target=_beep, daemon=True).start()

    def _wiz_setup_relay1(self):
        self._wiz_audio_stop = False
        self._wiz_step_desc.configure(
            text='Relay 1 will now open and close 5 times rapidly.\n'
                 'Listen for the relay clicking — you should hear 5 distinct clicks.\n\n'
                 'Click  Pass  if you heard clicks, Fail  if the relay was silent.')
        def _relay():
            import time
            for _ in range(5):
                if self._wiz_audio_stop:
                    break
                self.midi.send(HUI.led_on(0x1D, 0))
                time.sleep(0.25)
                self.midi.send(HUI.led_off(0x1D, 0))
                time.sleep(0.25)
            self.midi.send(HUI.led_off(0x1D, 0))   # ensure off
            self._relay1_on = False
        threading.Thread(target=_relay, daemon=True).start()

    def _wiz_setup_relay2(self):
        self._wiz_audio_stop = False
        self._wiz_step_desc.configure(
            text='Relay 2 will now open and close 5 times rapidly.\n'
                 'Listen for the relay clicking — you should hear 5 distinct clicks.\n\n'
                 'Click  Pass  if you heard clicks, Fail  if the relay was silent.')
        def _relay():
            import time
            for _ in range(5):
                if self._wiz_audio_stop:
                    break
                self.midi.send(HUI.led_on(0x1D, 1))
                time.sleep(0.25)
                self.midi.send(HUI.led_off(0x1D, 1))
                time.sleep(0.25)
            self.midi.send(HUI.led_off(0x1D, 1))   # ensure off
            self._relay2_on = False
        threading.Thread(target=_relay, daemon=True).start()

    def _tab_goodies(self, nb):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tab = tk.Frame(nb, bg=bg, padx=10, pady=10)
        nb.add(tab, text='  Goodies  ')

        # ── Vegas Mode ────────────────────────────────────────
        veg_f = self._lframe(tab, 'Vegas Mode  —  animates faders, LEDs, meters, V-pots and VFD simultaneously')
        veg_f.pack(fill='x', pady=(0, 8))
        veg_i = tk.Frame(veg_f, bg=bg, padx=10, pady=10)
        veg_i.pack(fill='x')

        self._vegas_btn = tk.Button(veg_i,
            text='  \u25b6  Start Vegas Mode  ',
            bg=fg, fg=bg, font=self.F10B, relief='flat', cursor='hand2',
            command=self._toggle_vegas)
        self._vegas_btn.grid(row=0, column=0, rowspan=2, padx=(0, 20), sticky='ns')

        tk.Label(veg_i,
                 text='Animates: faders (sine wave) \u00b7 LEDs (Christmas Lights) \u00b7'
                      ' VU meters \u00b7 V-pots \u00b7 VFD scrolling text\n'
                      'On stop: zeros and blanks everything (same as on connect).\n'
                      '\u26a0  Due to MIDI bandwidth, it may take up to 5 seconds after'
                      ' deactivating for the HUI to fully zero itself.',
                 bg=bg, fg=dim, font=self.F9, justify='left').grid(
            row=0, column=1, sticky='w')

    def _toggle_vegas(self):
        bg, fg = self.cfg.bg_color, self.cfg.fg_color
        if self._vegas_running:
            self._vegas_running = False
            self._vegas_btn.configure(text='  \u25b6  Start Vegas Mode  ', bg=fg, fg=bg)
            # Zero everything when Vegas stops
            self.root.after(200, self._reset_hardware)
        else:
            self._stop_demo()          # stop any other running animations first
            self._vegas_running = True
            self._vegas_btn.configure(text='  \u25a0  Stop Vegas Mode  ',
                                       bg='#ff5555', fg='#ffffff')
            threading.Thread(target=self._vegas_loop, daemon=True).start()

    def _vegas_loop(self):
        import random
        pairs     = self._all_led_pairs()
        led_cdown = {led: random.uniform(1.0, 4.0) for led in pairs}   # slow: 1–4 s per LED
        led_state = {led: False for led in pairs}

        # VFD scrolling banner
        vfd_msg  = '  * * *  HUI-Test Vegas Mode  * * *  '
        vfd_len  = len(vfd_msg)
        vfd_scroll = 0

        # Scribble marquee — scrolls across all 9 × 4 = 36 chars as one display
        scr_msg  = '    *  VEGAS  MODE  *    '
        scr_pos  = 0

        # Timecode — random numbers each update
        # (tc_ state variables removed; randint called inline)

        tick = 0
        t    = 0.0
        dt   = 0.05   # 20 Hz

        while self._vegas_running:
            # ── Faders + VU every tick (20 Hz) — keeps hardware motion smooth ──
            for i in range(8):
                ph  = i * math.pi / 4
                pct = int(50 + 46 * math.sin(t * 1.3 + ph))
                self.midi.send(HUI.fader(i, int(pct * 16383 / 100)))
            for i in range(8):
                ph    = i * math.pi / 4
                level = int(6 + 5.5 * math.sin(t * 2.1 + ph))
                self.midi.send(HUI.vu(i, 0, max(0, min(12, level))))
                self.midi.send(HUI.vu(i, 1, max(0, min(12, 12 - level))))

            # ── V-pots every 4th tick (5 Hz) ─────────────────────
            if tick % 4 == 0:
                for i in range(8):
                    ph  = i * math.pi / 4
                    pos = max(1, min(11, int(6 + 5 * math.sin(t * 0.9 + ph))))
                    self.midi.send(HUI.vpot(i, 0x20 | pos))

            # ── LEDs: slow random blink, max ONE toggle per tick ──
            # Countdown range 1.0–4.0 s keeps LED traffic minimal.
            for led in pairs:
                led_cdown[led] -= dt
                if led_cdown[led] <= 0:
                    led_state[led] = not led_state[led]
                    z, p = led
                    self.led_state[z][p] = led_state[led]
                    self.midi.send(
                        HUI.led_on(z, p) if led_state[led] else HUI.led_off(z, p))
                    led_cdown[led] = random.uniform(1.0, 4.0)
                    break   # one LED change maximum per tick

            # ── LEDs: Christmas-style blinking ────────────────────
            for led in pairs:
                led_cdown[led] -= dt
                if led_cdown[led] <= 0:
                    led_state[led] = not led_state[led]
                    z, p = led
                    self.led_state[z][p] = led_state[led]
                    self.midi.send(
                        HUI.led_on(z, p) if led_state[led] else HUI.led_off(z, p))
                    led_cdown[led] = random.uniform(0.05, 1.0)

            # ── VFD: scrolling banner — every 12th tick (~1.7 fps) ──
            if tick % 12 == 0:
                src = vfd_msg * 5
                banner  = src[vfd_scroll:vfd_scroll + 40]
                banner2 = src[(vfd_scroll + 20) % vfd_len:
                               (vfd_scroll + 20) % vfd_len + 40]
                for z in range(4):
                    self._vfd_send_zone(z,     banner [z*10:(z+1)*10])
                    self._vfd_send_zone(z + 4, banner2[z*10:(z+1)*10])
                vfd_scroll = (vfd_scroll + 1) % vfd_len

            # ── Scribble strips: unified marquee — every 10th tick (2 fps) ──
            if tick % 10 == 0:
                src36 = (scr_msg * 10)[scr_pos:scr_pos + 36]
                for ch in range(9):
                    self.midi.send(HUI.scribble(ch, src36[ch*4:(ch+1)*4]))
                scr_pos = (scr_pos + 1) % len(scr_msg)

            # ── Timecode: random numbers — every 6th tick (~3.3 fps) ──
            if tick % 6 == 0:
                self.midi.send(HUI.timecode(
                    random.randint(0, 23),
                    random.randint(0, 59),
                    random.randint(0, 59),
                    random.randint(0, 99),
                ))

            t    += dt
            tick += 1
            time.sleep(dt)

    def _demo_loop(self):
        """
        Runs in a background thread while the demo is active.
        All wave functions output -1.0 to +1.0.
        Final fader % = centre ± (amplitude/2) * wave_value, clamped 0-100.
        """
        _WAVES = {
            'Sine':     lambda t, ph: math.sin(t + ph),
            'Square':   lambda t, ph: 1.0 if math.sin(t + ph) >= 0 else -1.0,
            'Triangle': lambda t, ph: (2.0 / math.pi) * math.asin(math.sin(t + ph)),
            'Sawtooth': lambda t, ph: 2.0 * (((t + ph) / (2 * math.pi)) % 1.0) - 1.0,
            'Bounce':   lambda t, ph: 2.0 * abs(math.sin((t + ph) * 0.5)) - 1.0,
        }
        BASE_FREQ = math.pi   # radians/s → one cycle every ~2 s at speed 1.0
        dt = 0.02             # 50 Hz update rate
        t  = 0.0

        while self._demo_running:
            speed   = self._demo_speed.get()
            amp     = self._demo_amp.get()      # 0-100
            centre  = self._demo_centre.get()   # 0-100
            wave    = self._demo_wave.get()
            spread  = self._demo_phase.get()
            fn      = _WAVES.get(wave, _WAVES['Sine'])

            for i in range(8):
                phase = (i * 2 * math.pi / 8) if spread else 0.0
                val   = fn(t * speed * BASE_FREQ, phase)   # -1 to 1
                pct   = max(0, min(100, int(centre + val * amp / 2.0)))
                self.midi.send(HUI.fader(i, int(pct * 16383 / 100)))
                self.root.after(0, self._fader_vars[i].set, pct)
                self.root.after(0, self._fader_labels[i].configure, {'text': f'{pct:3d}%'})

            t  += dt
            time.sleep(dt)

        # Return all faders to zero when demo ends
        for i in range(8):
            self.root.after(0, self._fader_vars[i].set, 0)
            self.root.after(0, self._fader_labels[i].configure, {'text': '  0%'})

    # -- Audio --
    def _toggle_relay1(self):
        self._relay1_on = not self._relay1_on
        self.midi.send(HUI.led_on(0x1D, 0) if self._relay1_on else HUI.led_off(0x1D, 0))
        self._r1_btn.configure(
            text=f'Relay 1  {"ON " if self._relay1_on else "OFF"}',
            bg=self.cfg.fg_color if self._relay1_on else self.cfg.dim_color,
            fg=self.cfg.bg_color if self._relay1_on else self.cfg.fg_color)

    def _toggle_relay2(self):
        self._relay2_on = not self._relay2_on
        self.midi.send(HUI.led_on(0x1D, 1) if self._relay2_on else HUI.led_off(0x1D, 1))
        self._r2_btn.configure(
            text=f'Relay 2  {"ON " if self._relay2_on else "OFF"}',
            bg=self.cfg.fg_color if self._relay2_on else self.cfg.dim_color,
            fg=self.cfg.bg_color if self._relay2_on else self.cfg.fg_color)

    def _send_click(self):
        # Click (port 2) just needs to be switched on; no off required per spec.
        self.midi.send([
            mido.Message('control_change', channel=0, control=0x0C, value=0x1D),
            mido.Message('control_change', channel=0, control=0x2C, value=0x42),
        ])

    def _click_n_times(self):
        n = self._click_n.get()
        def _run():
            for _ in range(n):
                self._send_click()
                time.sleep(0.15)
        threading.Thread(target=_run, daemon=True).start()

    def _toggle_beep(self):
        self._beep_on = not self._beep_on
        self.midi.send([
            mido.Message('control_change', channel=0, control=0x0C, value=0x1D),
            mido.Message('control_change', channel=0, control=0x2C,
                         value=0x43 if self._beep_on else 0x03),
        ])
        self._beep_btn.configure(
            text=f'Beeper  {"ON " if self._beep_on else "OFF"}',
            bg=self.cfg.fg_color if self._beep_on else self.cfg.dim_color,
            fg=self.cfg.bg_color if self._beep_on else self.cfg.fg_color)

    # ════════════════════════════════════════════════════════
    # CONNECTION
    # ════════════════════════════════════════════════════════
    def _toggle_connect(self):
        bg, fg = self.cfg.bg_color, self.cfg.fg_color
        if self.midi.connected:
            self._stop_demo()
            self._vegas_running = False
            if self._ping_timeout_id:
                self.root.after_cancel(self._ping_timeout_id)
                self._ping_timeout_id = None
            self.midi.disconnect()
            self._conn_btn.configure(text='  \u25b6  Connect  ', bg=fg, fg=bg)
        else:
            ok = self.midi.connect()
            if ok:
                self._hui_state.reset()
                self._live_log_shown = 0
                self._ping_received  = False
                # Cancel any stale timeout from a previous connect attempt
                if self._ping_timeout_id:
                    self.root.after_cancel(self._ping_timeout_id)
                self._ping_timeout_id = self.root.after(5000, self._check_ping_timeout)
                self._conn_btn.configure(text='  \u25a0  Disconnect  ', bg='#ff5555', fg='#ffffff')
                if self._autopng_var.get():
                    self.midi.start_autopng()
                self.root.after(300, self._reset_hardware)

    def _check_ping_timeout(self):
        """Called 5 s after connect. Shows an error if no ping reply has arrived."""
        self._ping_timeout_id = None
        if self.midi.connected and not self._ping_received:
            self._show_no_ping_error()

    def _show_no_ping_error(self):
        """Modal error dialog shown when the HUI doesn't reply to pings."""
        bg  = self.cfg.bg_color
        fg  = self.cfg.fg_color
        dim = self.cfg.dim_color
        RED = '#ff5555'

        dlg = tk.Toplevel(self.root)
        dlg.title('Connection Error')
        dlg.configure(bg=bg)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        outer = tk.Frame(dlg, bg=bg, padx=28, pady=24)
        outer.pack()

        # Icon + heading row
        top = tk.Frame(outer, bg=bg)
        top.pack(anchor='w', pady=(0, 16))
        tk.Label(top, text='\u2716', font=('Segoe UI', 36, 'bold'),
                 bg=bg, fg=RED).pack(side='left', padx=(0, 18))
        tk.Label(top,
                 text='HUI-Test could not establish a\nconnection with your HUI.',
                 font=('Segoe UI', 12, 'bold'), bg=bg, fg=fg,
                 justify='left').pack(side='left')

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(0, 14))

        # Bullet points
        bullets = [
            'Confirm your MIDI settings in the Configuration menu are correct.',
            'Check the connection between your computer and HUI.',
            'Try restarting the program.',
        ]
        for b in bullets:
            row = tk.Frame(outer, bg=bg)
            row.pack(anchor='w', pady=2)
            tk.Label(row, text='\u2022', font=('Segoe UI', 10),
                     bg=bg, fg=dim).pack(side='left', padx=(0, 8))
            tk.Label(row, text=b, font=('Segoe UI', 10),
                     bg=bg, fg=dim, justify='left').pack(side='left')

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(16, 10))

        tk.Button(outer, text='  Close  ',
                  bg=RED, fg='#ffffff',
                  activebackground='#cc3333', activeforeground='#ffffff',
                  font=('Segoe UI', 10, 'bold'), relief='flat', cursor='hand2',
                  command=dlg.destroy).pack()

        # Centre over main window
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - dlg.winfo_width())  // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f'+{x}+{y}')

    def _send_welcome(self):
        """Write a brief greeting to the HUI VFD on successful connect."""
        row1 = 'Welcome to HUI-Test v1.9'.center(40)
        row2 = 'Part of the HUI Tools Suite'.center(40)
        for z in range(4):
            self._vfd_send_zone(z,     row1[z*10:(z+1)*10])
            self._vfd_send_zone(z + 4, row2[z*10:(z+1)*10])

    def _reset_hardware(self):
        """
        Zero and blank the entire HUI surface after connecting.
        Called automatically on connect; also used by Vegas Mode on stop.
        Welcome message is still shown on the VFD.
        """
        if not self.midi.connected:
            return

        # ── Faders to bottom ─────────────────────────────────
        for i in range(8):
            self.midi.send(HUI.fader(i, 0))
            self.root.after(0, self._fader_vars[i].set, 0)
            self.root.after(0, self._fader_labels[i].configure, {'text': '  0%'})

        # ── All LEDs off ──────────────────────────────────────
        for z in self._led_zones:
            ports = _LED_ONLY_PORTS.get(z, PORT_NAMES.get(z, {}))
            if z in _LED_ONLY_PORTS:
                for p in ports:
                    self.led_state[z][p] = False
                    self.midi.send(HUI.led_off(z, p))
            else:
                for p in ports:
                    self.led_state[z][p] = False
                self.midi.send(HUI.led_zone_all(z, False))
        self.root.after(0, self._refresh_port_buttons)

        # ── VU meters silent ─────────────────────────────────
        for i in range(8):
            self.midi.send(HUI.vu(i, 0, 0))
            self.midi.send(HUI.vu(i, 1, 0))
            self.root.after(0, self._vu_vars[i][0].set, 0)
            self.root.after(0, self._vu_vars[i][1].set, 0)

        # ── V-pots blank ─────────────────────────────────────
        for i in range(8):
            self.midi.send(HUI.vpot(i, 0x00))
            self.root.after(0, self._vp_pos[i].set, 0)

        # ── Scribble strips blank ─────────────────────────────
        for i in range(9):
            self.midi.send(HUI.scribble(i, '    '))

        # ── Timecode 00:00:00:00 ─────────────────────────────
        self.midi.send(HUI.timecode(0, 0, 0, 0))

        # ── VFD welcome ───────────────────────────────────────
        self.root.after(50, self._send_welcome)



    def _autopng_changed(self):
        if self._autopng_var.get() and self.midi.connected:
            self.midi.start_autopng()
        else:
            self.midi._stop_autopng()

    # ════════════════════════════════════════════════════════
    # INCOMING MIDI
    # ════════════════════════════════════════════════════════
    def _on_rx(self, msg: mido.Message):
        # Route through HUIState: updates live state + decoded log
        self._hui_state.process(msg)
        # Also feed the raw Connection tab log
        self.root.after(0, self._append_log, str(msg))
        # Ping reply: note_on ch=0 note=0 vel=127
        if (msg.type == 'note_on' and msg.channel == 0
                and msg.note == 0 and msg.velocity == 0x7F):
            self._ping_received = True
            self.root.after(0, self._ping_var.set, '\u2713  Reply  (90 00 7F)')

    def _append_log(self, text: str):
        self._log.configure(state='normal')
        self._log.insert('end', text + '\n')
        lines = int(self._log.index('end').split('.')[0])
        if lines > 300:
            self._log.delete('1.0', f'{lines - 300}.0')
        self._log.see('end')
        self._log.configure(state='disabled')

    def _clear_log(self):
        self._log.configure(state='normal')
        self._log.delete('1.0', 'end')
        self._log.configure(state='disabled')

    # ════════════════════════════════════════════════════════
    # STATUS BAR
    # ════════════════════════════════════════════════════════
    def _set_status(self, text: str, error: bool = False):
        def _do():
            self.status_var.set(text)
            self.status_lbl.configure(fg='#ff5555' if error else self.cfg.dim_color)
        self.root.after(0, _do)

    # ════════════════════════════════════════════════════════
    # CONFIGURE DIALOG
    # ════════════════════════════════════════════════════════
    def _open_config(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        dlg = tk.Toplevel(self.root)
        dlg.title('Configure — HUI-Test')
        dlg.configure(bg=bg)
        dlg.resizable(False, True)
        dlg.transient(self.root)
        dlg.grab_set()

        # Style notebook to match dark theme
        st = ttk.Style(dlg)
        st.theme_use('default')
        st.configure('C.TNotebook', background=bg, borderwidth=0, tabmargins=0)
        st.configure('C.TNotebook.Tab', background=dim, foreground=fg,
                     padding=[14, 5], font=self.F9)
        st.map('C.TNotebook.Tab',
               background=[('selected', bg)], foreground=[('selected', fg)])
        st.configure('C.TCombobox', fieldbackground=dim, background=bg,
                     foreground=fg, selectbackground=dim, arrowcolor=fg)
        st.configure('C.TSpinbox', fieldbackground=dim, foreground=fg,
                     background=bg, arrowcolor=fg)

        dlg.grid_rowconfigure(0, weight=1)
        dlg.grid_columnconfigure(0, weight=1)

        nb = ttk.Notebook(dlg, style='C.TNotebook')
        nb.grid(row=0, column=0, padx=12, pady=(12, 4), sticky='nsew')

        # ── Tab 1: MIDI Ports ────────────────────────────────
        midi_tab = tk.Frame(nb, bg=bg, padx=14, pady=12)
        nb.add(midi_tab, text='  MIDI Ports  ')
        midi_tab.columnconfigure(0, weight=1)

        tk.Label(midi_tab,
                 text='HUI-Test talks DIRECTLY to HUI hardware (no loopMIDI needed).\n'
                      'Do NOT run simultaneously with HUI-Display + Pro Tools.',
                 font=self.F9, bg=bg, fg=dim, justify='left').pack(anchor='w', pady=(0, 10))

        ins  = mido.get_input_names()
        outs = mido.get_output_names()
        port_frame = tk.Frame(midi_tab, bg=bg)
        port_frame.pack(fill='x')
        port_frame.grid_columnconfigure(1, weight=1)

        pairs = [
            ('HUI Hardware Output:', 'hui_out_port', 'out'),
            ('HUI Hardware Input:',  'hui_in_port',  'in'),
        ]
        _port_vars = {}
        _port_combos = {}
        for i, (lbl, key, dir_) in enumerate(pairs):
            tk.Label(port_frame, text=lbl, font=self.F9, bg=bg, fg=dim,
                     anchor='w').grid(row=i*2, column=0, columnspan=2, sticky='w',
                                      pady=(8 if i > 0 else 0, 2))
            choices = outs if dir_ == 'out' else ins
            v = tk.StringVar(value=getattr(self.cfg, key))
            cb = ttk.Combobox(port_frame, textvariable=v, values=choices,
                              width=36, style='C.TCombobox')
            cb.grid(row=i*2+1, column=0, columnspan=2, sticky='ew')
            _port_vars[key]   = v
            _port_combos[key] = (cb, dir_)

        def _refresh_ports():
            new_ins  = mido.get_input_names()
            new_outs = mido.get_output_names()
            for key, (cb, d) in _port_combos.items():
                cb['values'] = new_outs if d == 'out' else new_ins

        tk.Button(port_frame, text='\u21ba  Refresh port list',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=self.F9, relief='flat', cursor='hand2',
                  command=_refresh_ports).grid(
            row=len(pairs)*2, column=0, sticky='w', pady=(10, 0))

        # ── Tab 2: Appearance ────────────────────────────────
        app_tab = tk.Frame(nb, bg=bg, padx=14, pady=12)
        nb.add(app_tab, text='  Appearance  ')
        app_tab.columnconfigure(0, weight=0)
        app_tab.columnconfigure(1, weight=0)
        app_tab.columnconfigure(2, weight=1)

        _colors = {
            'fg_color':  self.cfg.fg_color,
            'bg_color':  self.cfg.bg_color,
            'dim_color': self.cfg.dim_color,
        }
        colour_rows = [
            ('Display text colour:', 'fg_color'),
            ('Background colour:',   'bg_color'),
            ('Labels & borders:',    'dim_color'),
        ]
        _swatches = {}

        def _pick_colour(key):
            result = colorchooser.askcolor(
                color=_colors[key], title='Choose colour', parent=dlg)
            if result[1]:
                _colors[key] = result[1]
                _swatches[key].configure(bg=result[1])

        for i, (label, key) in enumerate(colour_rows):
            tk.Label(app_tab, text=label, bg=bg, fg=dim, font=self.F9,
                     anchor='w', width=22).grid(row=i, column=0, sticky='w', pady=5)
            sw = tk.Label(app_tab, bg=_colors[key], width=4,
                          relief='groove', cursor='hand2')
            sw.grid(row=i, column=1, padx=(0, 8), sticky='w')
            sw.bind('<Button-1>', lambda e, k=key: _pick_colour(k))
            _swatches[key] = sw
            tk.Button(app_tab, text='Choose\u2026', bg=bg, fg=fg,
                      activebackground=dim, activeforeground=fg,
                      font=self.F9, relief='flat', cursor='hand2',
                      command=lambda k=key: _pick_colour(k)).grid(
                row=i, column=2, sticky='w')

        r = len(colour_rows)
        tk.Frame(app_tab, bg=dim, height=1).grid(
            row=r, column=0, columnspan=3, sticky='ew', pady=(12, 8))

        tk.Label(app_tab, text='VFD pixel size:', bg=bg, fg=dim,
                 font=self.F9, anchor='w').grid(row=r+1, column=0, sticky='w')
        _pxsz_var = tk.IntVar(value=self.cfg.pixel_size)
        ttk.Spinbox(app_tab, from_=1, to=16, textvariable=_pxsz_var,
                    width=5, style='C.TSpinbox').grid(row=r+1, column=1, sticky='w')
        tk.Label(app_tab, text='px   (1 = pixel-perfect  |  5 = default  |  10 = 4K)',
                 bg=bg, fg=dim, font=self.F9).grid(row=r+1, column=2, sticky='w')

        tk.Frame(app_tab, bg=dim, height=1).grid(
            row=r+2, column=0, columnspan=3, sticky='ew', pady=(12, 8))

        def _reset_visual():
            for k in ('fg_color', 'bg_color', 'dim_color'):
                _colors[k] = _DEFAULTS[k]
                _swatches[k].configure(bg=_DEFAULTS[k])
            _pxsz_var.set(_DEFAULTS['pixel_size'])

        tk.Button(app_tab, text='Reset visual settings to defaults',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=self.F9, relief='flat', cursor='hand2',
                  command=_reset_visual).grid(
            row=r+3, column=0, columnspan=3, sticky='w')

        tk.Label(app_tab,
                 text='Visual changes take full effect after restarting HUI-Test.',
                 font=self.F9, bg=bg, fg=dim, justify='left').grid(
            row=r+4, column=0, columnspan=3, sticky='w', pady=(10, 0))

        # ── Button row ───────────────────────────────────────
        tk.Frame(dlg, bg=dim, height=1).grid(row=1, column=0, sticky='ew')
        br = tk.Frame(dlg, bg=bg, padx=12, pady=10)
        br.grid(row=2, column=0, sticky='ew')

        def apply_():
            self.cfg.update(
                hui_out_port = _port_vars['hui_out_port'].get(),
                hui_in_port  = _port_vars['hui_in_port'].get(),
                fg_color     = _colors['fg_color'],
                bg_color     = _colors['bg_color'],
                dim_color    = _colors['dim_color'],
                pixel_size   = _pxsz_var.get(),
            )
            self._port_lbl.configure(
                text=f'Out: {self.cfg.hui_out_port}   In: {self.cfg.hui_in_port}')
            if self.midi.connected:
                self.midi.disconnect()
                self._conn_btn.configure(text='  \u25b6  Connect  ',
                                          bg=fg, fg=bg)
            dlg.destroy()

        tk.Button(br, text='Cancel', bg=bg, fg=dim, activebackground=dim,
                  activeforeground=fg, font=self.F10, relief='flat', cursor='hand2',
                  command=dlg.destroy).pack(side='right', padx=(6, 0))
        tk.Button(br, text='Apply', bg=fg, fg=bg, activebackground=fg,
                  activeforeground=bg, font=self.F10B, relief='flat', cursor='hand2',
                  command=apply_).pack(side='right')

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - dlg.winfo_width())  // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f'+{x}+{y}')

    # ════════════════════════════════════════════════════════
    # ABOUT DIALOG
    # ════════════════════════════════════════════════════════
    def _open_about(self):
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        dlg = tk.Toplevel(self.root)
        dlg.title('About HUI-Test')
        dlg.configure(bg=bg)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        outer = tk.Frame(dlg, bg=bg, padx=28, pady=22)
        outer.pack()
        tk.Label(outer, text='HUI-Test  v1.9',
                 font=('Segoe UI', 26, 'bold'), bg=bg, fg=fg).pack()
        tk.Label(outer, text='part of the HUI Tools suite',
                 font=self.F10, bg=bg, fg=dim).pack(pady=(3, 0))
        tk.Label(outer, text='\u00a9 2026 Richard Philip',
                 font=self.F10, bg=bg, fg=dim).pack(pady=(2, 16))
        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(0, 14))
        tk.Label(outer, text='HUI Protocol is Copyright \u00a9 1997\nMackie Designs and Digidesign (Avid)',
                 font=self.F9, bg=bg, fg=dim, justify='center').pack(pady=(0, 12))
        tk.Label(outer, text='This open-source software is licensed under',
                 font=self.F9, bg=bg, fg=dim).pack()
        link = tk.Label(outer, text='GNU General Public License v3.0',
                        font=('Segoe UI', 9, 'underline'), bg=bg, fg=fg, cursor='hand2')
        link.pack()
        link.bind('<Button-1>',
                  lambda e: webbrowser.open('https://www.gnu.org/licenses/gpl-3.0.html#license-text'))
        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(18, 10))
        tk.Button(outer, text='Close', bg=bg, fg=dim, activebackground=dim,
                  activeforeground=fg, font=self.F10, relief='flat', cursor='hand2',
                  command=dlg.destroy).pack()

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - dlg.winfo_width())  // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f'+{x}+{y}')

    # ════════════════════════════════════════════════════════
    # VFD DISPLAY WINDOW
    # ════════════════════════════════════════════════════════
    def _open_vfd_display(self):
        """Open (or bring to front) a floating dot-matrix VFD window."""
        if self._vfd_win and self._vfd_win.winfo_exists():
            self._vfd_win.lift()
            return

        bg  = self.cfg.bg_color
        fg  = self.cfg.fg_color
        dim = self.cfg.dim_color
        ps  = max(1, int(getattr(self.cfg, 'pixel_size', 5)))

        cw, ch  = VFDDotMatrix.required_size(ps)
        max_sw  = self.root.winfo_screenwidth() - 40
        frame_w = min(cw, max_sw)

        win = tk.Toplevel(self.root)
        win.title('HUI Main Display')
        win.configure(bg=bg)
        win.resizable(False, False)
        win.attributes('-topmost', True)
        self._vfd_win = win

        cv = tk.Canvas(win, width=frame_w, height=ch, bg=bg, highlightthickness=0)
        cv.pack(fill='x')

        if cw > max_sw:
            sb = tk.Scrollbar(win, orient='horizontal', command=cv.xview, bg=bg)
            sb.pack(fill='x')
            cv.configure(xscrollcommand=sb.set, scrollregion=(0, 0, cw, ch))

        matrix = VFDDotMatrix(cv, pixel_size=ps, fg_color=fg, bg_color=bg)
        matrix.blank()

        tk.Frame(win, bg=dim, height=1).pack(fill='x')
        tk.Label(win, text='Reflects data sent by HUI-Test  —  not live HUI hardware input',
                 font=('Segoe UI', 9), bg=bg, fg=dim,
                 anchor='w', padx=8).pack(fill='x', pady=(0, 2))

        # Drag-to-move
        _ox, _oy = [0], [0]
        win.bind('<ButtonPress-1>',
                 lambda e: (_ox.__setitem__(0, e.x), _oy.__setitem__(0, e.y)))
        win.bind('<B1-Motion>',
                 lambda e: win.geometry(
                     f'+{win.winfo_x()+e.x-_ox[0]}+{win.winfo_y()+e.y-_oy[0]}'))

        _cache = ['', '']   # last rendered content; skip redraw if unchanged

        def _refresh():
            if not win.winfo_exists():
                return
            top, bot = self._vfd_state.get_rows()
            if top != _cache[0] or bot != _cache[1]:
                matrix.update_display(top, bot)
                _cache[0] = top
                _cache[1] = bot
            win.after(50, _refresh)

        win.after(50, _refresh)

    # ════════════════════════════════════════════════════════
    # STARTUP WARNING
    # ════════════════════════════════════════════════════════
    def _show_warning(self):
        """Show a modal warning dialog unless the user has dismissed it permanently."""
        if self.cfg.skip_test_warning:
            return

        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        AMBER = '#ffaa00'

        dlg = tk.Toplevel(self.root)
        dlg.title('\u26a0  Important — Read Before Use')
        dlg.configure(bg=bg)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        outer = tk.Frame(dlg, bg=bg, padx=26, pady=20)
        outer.pack()

        tk.Label(outer,
                 text='\u26a0  Do Not Run Simultaneously With HUI-Display + Pro Tools',
                 font=('Segoe UI', 11, 'bold'), bg=bg, fg=AMBER).pack(pady=(0, 14))

        msg = (
            'HUI-Test connects DIRECTLY to your Mackie HUI hardware over MIDI.\n'
            'If HUI-Display is currently routing Pro Tools traffic through the\n'
            'same MIDI ports, both programs will fight over the connection.\n'
            '\n'
            'This can cause:\n'
            '  \u2022  Erratic or uncontrolled fader movement\n'
            '  \u2022  Garbled or corrupted display output\n'
            '  \u2022  Pro Tools losing its HUI connection entirely\n'
            '  \u2022  loopMIDI auto-muting the virtual ports\n'
            '\n'
            'Before using HUI-Test, please either:\n'
            '  1.  Close HUI-Display, or\n'
            '  2.  Disconnect the HUI in Pro Tools:\n'
            '       Setup  \u25b6  Peripherals  \u25b6  MIDI Controllers'
        )
        tk.Label(outer, text=msg, font=('Segoe UI', 9),
                 bg=bg, fg=dim, justify='left').pack(pady=(0, 14))

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(0, 10))

        skip_var = tk.BooleanVar(value=False)
        tk.Checkbutton(outer, text='Do not show this warning again',
                       variable=skip_var, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg,
                       font=('Segoe UI', 9)).pack(anchor='w', pady=(0, 10))

        def _ok():
            if skip_var.get():
                self.cfg.update(skip_test_warning=True)
            dlg.destroy()

        tk.Button(outer, text='  OK, I understand  ',
                  bg=fg, fg=bg, activebackground=fg, activeforeground=bg,
                  font=('Segoe UI', 10, 'bold'), relief='flat',
                  cursor='hand2', pady=5, command=_ok).pack()

        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - dlg.winfo_width())  // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f'+{x}+{y}')

    # ════════════════════════════════════════════════════════
    # HELPER WIDGETS
    # ════════════════════════════════════════════════════════
    def _btn(self, parent, text: str, cmd) -> tk.Button:
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg, fg=fg, activebackground=dim, activeforeground=fg,
                         font=self.F9, relief='flat', cursor='hand2', padx=6, pady=2)

    def _lframe(self, parent, title: str) -> tk.LabelFrame:
        bg, dim = self.cfg.bg_color, self.cfg.dim_color
        return tk.LabelFrame(parent, text=f' {title} ', bg=bg, fg=dim, font=self.F9)

    def _display_entry(self, parent, label: str, row: int, max_chars: int) -> tk.Entry:
        """Create a labelled entry box with a hard character limit and no initial content."""
        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color
        tk.Label(parent, text=label, font=self.F9, bg=bg, fg=dim, width=6, anchor='w').grid(
            row=row, column=0, sticky='w', pady=3)
        vcmd = (self.root.register(lambda P: len(P) <= max_chars), '%P')
        e = tk.Entry(parent, width=max_chars + 1, bg=dim, fg=fg,
                     font=('Courier New', 12, 'bold'), insertbackground=fg,
                     validate='key', validatecommand=vcmd)
        e.grid(row=row, column=1, sticky='ew', pady=3, padx=(0, 6))
        return e

    # ════════════════════════════════════════════════════════
    # MAIN LOOP
    # ════════════════════════════════════════════════════════
    def run(self):
        self.root.mainloop()


# ╔═══════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                              ║
# ╚═══════════════════════════════════════════════════════════╝

def main():
    cfg = Config()
    app = HUITestApp(cfg)
    app.run()


if __name__ == '__main__':
    try:
        main()
    except Exception as _e:
        import traceback
        # Write to a log file next to the script
        _log = os.path.join(_DIR, 'hui_test_error.txt')
        with open(_log, 'w') as _f:
            traceback.print_exc(file=_f)
        # Also try to show a message box
        try:
            import tkinter.messagebox as _mb
            _root = tk.Tk(); _root.withdraw()
            _mb.showerror('HUI-Test crashed',
                f'Error: {_e}\n\nFull details saved to:\n{_log}')
            _root.destroy()
        except Exception:
            pass
        raise
