#!/usr/bin/env python3
"""
╔════════════════════════════════════════════╗
║          HUI-Test  v1.0                    ║
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
    F9  = ('Courier New',  9)
    F10 = ('Courier New', 10)
    F10B= ('Courier New', 10, 'bold')
    F11 = ('Courier New', 11, 'bold')
    F13 = ('Courier New', 13, 'bold')

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
            fill=dim, font=('Courier New', max(4, fs)),
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
            font=('Courier New', max(5, fs), 'bold'), anchor='w',
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
            font=('Courier New', max(4, fs-1)), tags=f'c{ch}_fpct')

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
            font=('Courier New', max(4, fs-1), 'bold'), anchor='center')
        cv.create_oval(JCX-JR, JCY-JR, JCX+JR, JCY+JR,
            outline=dim, fill=bg, width=lw)
        cv.create_line(JCX, JCY, JCX, JCY-JR+lw,
            fill=dim, width=lw, tags='jog_line')
        cv.create_text(JCX, JCY + JR + max(5, int(h*0.02)),
            text='Total: 0', fill=dim,
            font=('Courier New', max(4, fs-1)), tags='jog_txt')

        # Ping indicator
        PY = int(h * 0.920)
        PR = max(5, int(h * 0.022))
        cv.create_text(JCX, PY - PR - max(4, int(h*0.015)),
            text='PING', fill=dim,
            font=('Courier New', max(4, fs-1), 'bold'), anchor='center')
        cv.create_oval(JCX-PR*2, PY-PR, JCX+PR*2, PY+PR,
            fill=bg, outline=dim, width=1, tags='ping_dot')
        cv.create_text(JCX, PY + PR + max(4, int(h*0.015)),
            text='\u2014', fill=dim,
            font=('Courier New', max(4, fs-1)), tags='ping_txt')

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
        ]
        n      = len(groups)
        grp_h  = h / n
        for gi, (name, zone, ports) in enumerate(groups):
            gy0  = gi * grp_h
            # Group label
            cv.create_text(x0 + w/2, gy0 + grp_h*0.18,
                text=name, fill=dim,
                font=('Courier New', max(4, fs-1), 'bold'), anchor='center')
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
            if pf:
                s.ping_flag = False

        # ── All zone/port buttons (update by tag; missing tags are silently skipped)
        ALL_ZONES = list(range(8)) + [
            0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
            0x0E, 0x0F, 0x10, 0x11, 0x12,
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
        self._demo_running = False
        bg, fg = self.cfg.bg_color, self.cfg.fg_color
        if hasattr(self, '_demo_btn'):
            self._demo_btn.configure(text='  \u25b6  Start Demo  ',
                                      bg=fg, fg=bg)

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
            self.midi.disconnect()
            self._conn_btn.configure(text='  \u25b6  Connect  ', bg=fg, fg=bg)
        else:
            ok = self.midi.connect()
            if ok:
                self._hui_state.reset()
                self._live_log_shown = 0
                self._conn_btn.configure(text='  \u25a0  Disconnect  ', bg='#ff5555', fg='#ffffff')
                if self._autopng_var.get():
                    self.midi.start_autopng()
                # Send welcome message to the VFD
                self.root.after(200, self._send_welcome)

    def _send_welcome(self):
        """Write a brief greeting to the HUI VFD on successful connect."""
        row1 = '    Welcome to HUI-Test v1.0    '[:40]
        row2 = '     part of the HUI Tools suite'[:40]
        for z in range(4):
            self._vfd_send_zone(z,     row1[z*10:(z+1)*10])
            self._vfd_send_zone(z + 4, row2[z*10:(z+1)*10])

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
        # Ping reply indicator in Connection tab
        if (msg.type == 'note_on' and msg.channel == 0
                and msg.note == 0 and msg.velocity == 0x7F):
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
        dlg.resizable(False, False)
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

        tk.Label(app_tab, text='VFD font size:', bg=bg, fg=dim,
                 font=self.F9, anchor='w').grid(row=r+1, column=0, sticky='w')
        _font_var = tk.IntVar(value=self.cfg.font_size)
        ttk.Spinbox(app_tab, from_=10, to=28, textvariable=_font_var,
                    width=5, style='C.TSpinbox').grid(row=r+1, column=1, sticky='w')
        tk.Label(app_tab, text='pt', bg=bg, fg=dim,
                 font=self.F9).grid(row=r+1, column=2, sticky='w')

        tk.Frame(app_tab, bg=dim, height=1).grid(
            row=r+2, column=0, columnspan=3, sticky='ew', pady=(12, 8))

        def _reset_visual():
            for k in ('fg_color', 'bg_color', 'dim_color'):
                _colors[k] = _DEFAULTS[k]
                _swatches[k].configure(bg=_DEFAULTS[k])
            _font_var.set(_DEFAULTS['font_size'])

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
                font_size    = _font_var.get(),
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
        tk.Label(outer, text='HUI-Test',
                 font=('Courier New', 26, 'bold'), bg=bg, fg=fg).pack()
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
                        font=('Courier New', 9, 'underline'), bg=bg, fg=fg, cursor='hand2')
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
        """Open (or bring to front) a floating window showing the live VFD state."""
        if self._vfd_win and self._vfd_win.winfo_exists():
            self._vfd_win.lift()
            return

        bg, fg, dim = self.cfg.bg_color, self.cfg.fg_color, self.cfg.dim_color

        win = tk.Toplevel(self.root)
        win.title('HUI Main Display')
        win.configure(bg=bg)
        win.resizable(False, False)
        win.attributes('-topmost', True)
        self._vfd_win = win

        outer = tk.Frame(win, bg=bg,
                         highlightbackground='#003d35', highlightthickness=2,
                         padx=14, pady=10)
        outer.pack()

        tk.Label(outer, text='  MACKIE HUI  -  MAIN DISPLAY  ',
                 font=('Courier New', 9), bg=bg, fg=dim).pack(pady=(0, 6))

        panel = tk.Frame(outer, bg=bg,
                         highlightbackground=dim, highlightthickness=1,
                         padx=10, pady=8)
        panel.pack()

        row_vars = []
        for _ in range(2):
            v = tk.StringVar(value=' ' * 40)
            row_vars.append(v)
            tk.Label(panel, textvariable=v,
                     font=('Courier New', 15, 'bold'),
                     bg=bg, fg=fg, anchor='w', justify='left', width=40).pack(anchor='w')

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(8, 2))
        status_var = tk.StringVar(value='Reflects data sent by HUI-Test  (not live HUI input)')
        tk.Label(outer, textvariable=status_var,
                 font=('Courier New', 9), bg=bg, fg=dim).pack()

        # Drag to move
        _ox, _oy = [0], [0]
        win.bind('<ButtonPress-1>',
                 lambda e: (_ox.__setitem__(0, e.x), _oy.__setitem__(0, e.y)))
        win.bind('<B1-Motion>',
                 lambda e: win.geometry(
                     f'+{win.winfo_x()+e.x-_ox[0]}+{win.winfo_y()+e.y-_oy[0]}'))

        def _refresh():
            if not win.winfo_exists():
                return
            top, bot = self._vfd_state.get_rows()
            row_vars[0].set(top)
            row_vars[1].set(bot)
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
                 font=('Courier New', 11, 'bold'), bg=bg, fg=AMBER).pack(pady=(0, 14))

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
        tk.Label(outer, text=msg, font=('Courier New', 9),
                 bg=bg, fg=dim, justify='left').pack(pady=(0, 14))

        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(0, 10))

        skip_var = tk.BooleanVar(value=False)
        tk.Checkbutton(outer, text='Do not show this warning again',
                       variable=skip_var, bg=bg, fg=dim,
                       selectcolor=bg, activebackground=bg,
                       font=('Courier New', 9)).pack(anchor='w', pady=(0, 10))

        def _ok():
            if skip_var.get():
                self.cfg.update(skip_test_warning=True)
            dlg.destroy()

        tk.Button(outer, text='  OK, I understand  ',
                  bg=fg, fg=bg, activebackground=fg, activeforeground=bg,
                  font=('Courier New', 10, 'bold'), relief='flat',
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
