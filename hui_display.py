#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════╗
║        HUI Virtual Display  v3.0          ║
║  Shows the 2×40 character main VFD of a   ║
║  Mackie HUI on-screen, and passes all     ║
║  MIDI through to the hardware unchanged.  ║
╚═══════════════════════════════════════════╝

© 2026 Richard Philip
Released under the GNU General Public License v3.0
https://www.gnu.org/licenses/gpl-3.0.html

Protocol reference: HUI_MIDI_protocol.pdf (theageman, 2010)
  - Main display command:  0x12
  - 8 zones (0-7), 10 characters each
  - Zones 0-3 -> top row (chars 0-39)
  - Zones 4-7 -> bottom row (chars 0-39)

Configuration is stored in config.hui (same folder as this file).
On first run, config.hui is created with default values.
Use Menu > Configure to change settings from within the program.
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
# ║  CONFIGURATION FILE                                       ║
# ╚═══════════════════════════════════════════════════════════╝

_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, 'config.hui')

_DEFAULTS = {
    'pt_sends_to':  'HUI In',     # loopMIDI port Pro Tools sends to
    'hui_out_port': 'HUI',        # Physical HUI hardware (output)
    'hui_in_port':  'HUI',        # Physical HUI hardware (input)
    'pt_recv_from': 'HUI Out',    # loopMIDI port Pro Tools receives from
    'fg_color':     '#00eed5',    # Display text colour (bright teal)
    'bg_color':     '#06100e',    # Background colour (near-black)
    'dim_color':    '#005549',    # Labels and borders colour (dim teal)
    'font_size':    15,           # Display font size in points
}


class Config:
    """
    Loads settings from config.hui on startup.
    Saves settings back to config.hui whenever they change.
    Access settings as attributes: cfg.fg_color, cfg.font_size, etc.
    """

    def __init__(self):
        self._d = dict(_DEFAULTS)
        self._load()

    def __getattr__(self, key):
        # Called when normal attribute lookup fails (i.e. for config keys)
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
            # First run — write a default config.hui so the user can inspect it
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
        """Update one or more settings and immediately save to disk."""
        for k, v in kwargs.items():
            if k in _DEFAULTS:
                self._d[k] = v
        self._save()


# ╔═══════════════════════════════════════════════════════════╗
# ║  HUI PROTOCOL                                             ║
# ╚═══════════════════════════════════════════════════════════╝

_HUI_HEADER     = bytes([0x00, 0x00, 0x66, 0x05, 0x00])
_CMD_MAIN_DISP  = 0x12   # 2x40 main display  <- we decode this
# 0x10 (small channel strips) and 0x11 (timecode) are passed through untouched

_ROWS = 2
_COLS = 40

# Large display (HUI-2) character translation table.
# Standard ASCII occupies 0x20-0x7D; everything else is below.
_HUI2: dict = {
    0x10: '\u258f', 0x11: '\u258e', 0x12: '\u258d', 0x13: '\u258c',
    0x14: '\u2588', 0x15: '\u2590', 0x16: '\u2595', 0x17: '\u258f',
    0x18: ' ',      0x19: '\u266a',
    0x1A: '\u00b0', 0x1B: '\u00b0',
    0x1C: '\u25bc', 0x1D: '\u25ba', 0x1E: '\u25c4', 0x1F: '\u25b2',
    0x5C: '\u00a5',
    0x7E: '\u2192', 0x7F: '\u2190',
}

def _decode(b: int) -> str:
    if b in _HUI2:         return _HUI2[b]
    if 0x20 <= b <= 0x7D:  return chr(b)
    return ' '


class HUIMainDisplay:
    """
    Thread-safe 2x40 character buffer.

    Zone layout:
        Row 0:  zone 0 (cols  0-9)  zone 1 (cols 10-19)
                zone 2 (cols 20-29) zone 3 (cols 30-39)
        Row 1:  zone 4 (cols  0-9)  zone 5 (cols 10-19)
                zone 6 (cols 20-29) zone 7 (cols 30-39)
    """

    def __init__(self):
        self.lock          = threading.Lock()
        self._buf          = [' '] * (_ROWS * _COLS)
        self.sysex_received = 0   # incremented each time a display SysEx is processed

    def process_sysex(self, data) -> None:
        d = bytes(data)
        if len(d) < 6 or d[:5] != _HUI_HEADER or d[5] != _CMD_MAIN_DISP:
            return
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
                    self._buf[row * _COLS + col + j] = _decode(b)
            self.sysex_received += 1

    def get_rows(self):
        with self.lock:
            return (
                ''.join(self._buf[:_COLS]),
                ''.join(self._buf[_COLS:]),
            )


# ╔═══════════════════════════════════════════════════════════╗
# ║  MIDI ROUTING                                             ║
# ╚═══════════════════════════════════════════════════════════╝

def _resolve(name: str, available: list) -> str:
    """
    Find a port by name, tolerating the trailing number that
    Windows appends (e.g. 'HUI In' matches 'HUI In 3').
    """
    if name in available:
        return name
    matches = [p for p in available if p.startswith(name)]
    if matches:
        return sorted(matches)[0]
    return name   # return as-is; mido will give a clear error


class MIDIRouter:
    """
    Manages the four MIDI ports and message routing.
    Can be stopped and restarted (e.g. after config changes).
    """

    def __init__(self, cfg: Config, display: HUIMainDisplay, on_status):
        self.cfg        = cfg
        self.display    = display
        self.on_status  = on_status
        self._stop      = threading.Event()
        self._ports     = []

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        """Signal the worker threads to stop and close all ports."""
        self._stop.set()
        for p in self._ports:
            try:
                p.close()
            except Exception:
                pass
        self._ports.clear()

    def _run(self):
        cfg = self.cfg
        try:
            ins  = mido.get_input_names()
            outs = mido.get_output_names()
            pti_name = _resolve(cfg.pt_sends_to,  ins)
            huo_name = _resolve(cfg.hui_out_port, outs)
            hui_name = _resolve(cfg.hui_in_port,  ins)
            pto_name = _resolve(cfg.pt_recv_from, outs)

            pti = mido.open_input(pti_name)
            huo = mido.open_output(huo_name)
            hui = mido.open_input(hui_name)
            pto = mido.open_output(pto_name)
            self._ports = [pti, huo, hui, pto]
        except Exception as e:
            self.on_status(f'MIDI ERROR: {e}', error=True)
            print(f'\nMIDI Error: {e}')
            print('Input ports:',  mido.get_input_names())
            print('Output ports:', mido.get_output_names())
            return

        self.on_status(f'Connected  -  {pti_name}  -  {huo_name}')

        # Background thread: HUI hardware -> Pro Tools
        def _hw_to_pt():
            try:
                for msg in hui:
                    if self._stop.is_set():
                        break
                    try:
                        pto.send(msg)
                    except Exception:
                        pass
            except Exception:
                pass

        threading.Thread(target=_hw_to_pt, daemon=True).start()

        # Main routing: Pro Tools -> HUI hardware, decode display SysEx
        try:
            n = 0
            for msg in pti:
                if self._stop.is_set():
                    break
                try:
                    huo.send(msg)
                except Exception:
                    pass
                if msg.type == 'sysex':
                    self.display.process_sysex(msg.data)
                n += 1
                if n % 100 == 0:
                    sx = self.display.sysex_received
                    self.on_status(
                        f'Running  -  {n:,} msgs  -  {sx} display SysEx received'
                    )
        except Exception:
            pass


# ╔═══════════════════════════════════════════════════════════╗
# ║  CONFIGURE DIALOG                                         ║
# ╚═══════════════════════════════════════════════════════════╝

class ConfigDialog(tk.Toplevel):
    """
    Modal configuration window with two tabs:
      - MIDI Ports:  four drop-down menus for the MIDI port names
      - Appearance:  colour pickers, font size, and reset to defaults
    """

    def __init__(self, parent_root, cfg: Config, on_apply):
        super().__init__(parent_root)
        self.cfg      = cfg
        self.on_apply = on_apply

        # Working copies of colours — updated by picker, committed on Apply
        self._colors = {
            'fg_color':  cfg.fg_color,
            'bg_color':  cfg.bg_color,
            'dim_color': cfg.dim_color,
        }

        bg  = cfg.bg_color
        fg  = cfg.fg_color
        dim = cfg.dim_color

        self.title('Configure — HUI Display')
        self.resizable(False, False)
        self.transient(parent_root)
        self.grab_set()
        self.configure(bg=bg)

        # Style ttk widgets to match the dark theme
        st = ttk.Style(self)
        st.theme_use('default')
        st.configure('D.TNotebook', background=bg, borderwidth=0, tabmargins=0)
        st.configure('D.TNotebook.Tab', background=dim, foreground=fg,
                     padding=[14, 5], font=('Courier New', 9))
        st.map('D.TNotebook.Tab',
               background=[('selected', bg)],
               foreground=[('selected', fg)])
        st.configure('D.TCombobox', fieldbackground=dim, background=bg,
                     foreground=fg, selectbackground=dim, arrowcolor=fg)
        st.configure('D.TSpinbox', fieldbackground=dim, foreground=fg,
                     background=bg, arrowcolor=fg)

        # ── Top-level layout using grid (reliable, no pack/side tricks) ──
        # Row 0: notebook  (expands to fill space)
        # Row 1: separator
        # Row 2: button row
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        nb = ttk.Notebook(self, style='D.TNotebook')
        nb.grid(row=0, column=0, padx=12, pady=(12, 4), sticky='nsew')

        self._build_midi_tab(nb, bg, fg, dim)
        self._build_appearance_tab(nb, bg, fg, dim)

        tk.Frame(self, bg=dim, height=1).grid(
            row=1, column=0, sticky='ew')

        btn_row = tk.Frame(self, bg=bg, padx=12, pady=10)
        btn_row.grid(row=2, column=0, sticky='ew')

        tk.Button(btn_row, text='Cancel',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=('Courier New', 10), relief='flat', cursor='hand2',
                  command=self.destroy).pack(side='right', padx=(6, 0))

        tk.Button(btn_row, text='Apply & Reconnect',
                  bg=fg, fg=bg, activebackground=fg, activeforeground=bg,
                  font=('Courier New', 10, 'bold'), relief='flat', cursor='hand2',
                  command=self._apply).pack(side='right')

        # Centre over the parent window
        self.update_idletasks()
        x = parent_root.winfo_x() + (parent_root.winfo_width()  - self.winfo_width())  // 2
        y = parent_root.winfo_y() + (parent_root.winfo_height() - self.winfo_height()) // 2
        self.geometry(f'+{x}+{y}')

    # ── Tab 1: MIDI Ports ─────────────────────────────────────
    def _build_midi_tab(self, nb, bg, fg, dim):
        tab = tk.Frame(nb, bg=bg, padx=14, pady=12)
        nb.add(tab, text='  MIDI Ports  ')

        ins  = mido.get_input_names()
        outs = mido.get_output_names()

        self._port_vars   = {}
        self._port_combos = {}

        port_rows = [
            ('Pro Tools \u2192 Script  (input):',    'pt_sends_to',  'in',  self.cfg.pt_sends_to),
            ('Script \u2192 HUI hardware  (output):', 'hui_out_port', 'out', self.cfg.hui_out_port),
            ('HUI hardware \u2192 Script  (input):',  'hui_in_port',  'in',  self.cfg.hui_in_port),
            ('Script \u2192 Pro Tools  (output):',    'pt_recv_from', 'out', self.cfg.pt_recv_from),
        ]

        for i, (label, key, direction, current) in enumerate(port_rows):
            tk.Label(tab, text=label, bg=bg, fg=dim,
                     font=('Courier New', 9), anchor='w').grid(
                row=i * 2, column=0, sticky='w',
                pady=(10 if i > 0 else 0, 2))

            var     = tk.StringVar(value=current)
            choices = ins if direction == 'in' else outs
            cb      = ttk.Combobox(tab, textvariable=var, values=choices,
                                   width=38, style='D.TCombobox')
            cb.grid(row=i * 2 + 1, column=0, sticky='ew')

            self._port_vars[key]   = var
            self._port_combos[key] = (cb, direction)

        tk.Button(tab, text='\u21ba  Refresh port list',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=('Courier New', 9), relief='flat', cursor='hand2',
                  command=self._refresh_ports).grid(
            row=len(port_rows) * 2, column=0, sticky='w', pady=(12, 0))

    # ── Tab 2: Appearance ─────────────────────────────────────
    def _build_appearance_tab(self, nb, bg, fg, dim):
        tab = tk.Frame(nb, bg=bg, padx=14, pady=12)
        nb.add(tab, text='  Appearance  ')

        colour_rows = [
            ('Display text colour:', 'fg_color'),
            ('Background colour:',   'bg_color'),
            ('Labels & borders:',    'dim_color'),
        ]

        self._swatches = {}
        for i, (label, key) in enumerate(colour_rows):
            tk.Label(tab, text=label, bg=bg, fg=dim,
                     font=('Courier New', 9), anchor='w',
                     width=22).grid(row=i, column=0, sticky='w', pady=5)

            swatch = tk.Label(tab, bg=self._colors[key],
                              width=4, relief='groove', cursor='hand2')
            swatch.grid(row=i, column=1, padx=(0, 8), sticky='w')
            swatch.bind('<Button-1>', lambda e, k=key: self._pick_colour(k))
            self._swatches[key] = swatch

            tk.Button(tab, text='Choose\u2026',
                      bg=bg, fg=fg, activebackground=dim, activeforeground=fg,
                      font=('Courier New', 9), relief='flat', cursor='hand2',
                      command=lambda k=key: self._pick_colour(k)).grid(
                row=i, column=2, sticky='w')

        # Separator
        r = len(colour_rows)
        tk.Frame(tab, bg=dim, height=1).grid(
            row=r, column=0, columnspan=3, sticky='ew', pady=(12, 8))

        # Font size
        tk.Label(tab, text='Display font size:', bg=bg, fg=dim,
                 font=('Courier New', 9), anchor='w').grid(
            row=r + 1, column=0, sticky='w')

        self._font_size_var = tk.IntVar(value=self.cfg.font_size)
        ttk.Spinbox(tab, from_=10, to=28, textvariable=self._font_size_var,
                    width=5, style='D.TSpinbox').grid(
            row=r + 1, column=1, sticky='w')

        tk.Label(tab, text='pt', bg=bg, fg=dim,
                 font=('Courier New', 9)).grid(row=r + 1, column=2, sticky='w')

        # Separator before reset button
        tk.Frame(tab, bg=dim, height=1).grid(
            row=r + 2, column=0, columnspan=3, sticky='ew', pady=(12, 8))

        # Reset visual to defaults
        tk.Button(tab, text='Reset visual settings to defaults',
                  bg=bg, fg=dim, activebackground=dim, activeforeground=fg,
                  font=('Courier New', 9), relief='flat', cursor='hand2',
                  command=self._reset_visual).grid(
            row=r + 3, column=0, columnspan=3, sticky='w')

    # ── Actions ───────────────────────────────────────────────
    def _reset_visual(self):
        """Restore colour swatches and font size to factory defaults."""
        for key in ('fg_color', 'bg_color', 'dim_color'):
            self._colors[key] = _DEFAULTS[key]
            self._swatches[key].configure(bg=_DEFAULTS[key])
        self._font_size_var.set(_DEFAULTS['font_size'])

    def _refresh_ports(self):
        ins  = mido.get_input_names()
        outs = mido.get_output_names()
        for key, (cb, direction) in self._port_combos.items():
            cb['values'] = ins if direction == 'in' else outs

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
            font_size    = self._font_size_var.get(),
        )
        self.on_apply()
        self.destroy()


# ╔═══════════════════════════════════════════════════════════╗
# ║  ABOUT DIALOG                                             ║
# ╚═══════════════════════════════════════════════════════════╝

class AboutDialog(tk.Toplevel):
    """Simple modal About window."""

    _GPL_URL = 'https://www.gnu.org/licenses/gpl-3.0.html#license-text'

    def __init__(self, parent_root, cfg: Config):
        super().__init__(parent_root)
        bg  = cfg.bg_color
        fg  = cfg.fg_color
        dim = cfg.dim_color

        self.title('About HUI-Display')
        self.resizable(False, False)
        self.transient(parent_root)
        self.grab_set()
        self.configure(bg=bg)

        outer = tk.Frame(self, bg=bg, padx=28, pady=22)
        outer.pack()

        # ── Large bold title ──────────────────────────────────
        tk.Label(outer,
                 text='HUI-Display',
                 font=('Courier New', 26, 'bold'),
                 bg=bg, fg=fg).pack()

        tk.Label(outer,
                 text='part of the HUI Tools suite',
                 font=('Courier New', 10),
                 bg=bg, fg=dim).pack(pady=(3, 0))

        tk.Label(outer,
                 text='\u00a9 2026 Richard Philip',
                 font=('Courier New', 10),
                 bg=bg, fg=dim).pack(pady=(2, 16))

        # ── Separator ─────────────────────────────────────────
        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(0, 14))

        # ── Copyright notice ──────────────────────────────────
        tk.Label(outer,
                 text='HUI Protocol is Copyright \u00a9 1997\n'
                      'Mackie Designs and Digidesign (Avid)',
                 font=('Courier New', 9),
                 bg=bg, fg=dim, justify='center').pack(pady=(0, 12))

        # ── Licence line with clickable hyperlink ─────────────
        lic_frame = tk.Frame(outer, bg=bg)
        lic_frame.pack()

        tk.Label(lic_frame,
                 text='This open-source software is licensed under',
                 font=('Courier New', 9),
                 bg=bg, fg=dim).pack()

        link = tk.Label(lic_frame,
                        text='GNU General Public License v3.0',
                        font=('Courier New', 9, 'underline'),
                        bg=bg, fg=fg, cursor='hand2')
        link.pack()
        link.bind('<Button-1>',
                  lambda e: webbrowser.open(self._GPL_URL))

        # ── Close button ──────────────────────────────────────
        tk.Frame(outer, bg=dim, height=1).pack(fill='x', pady=(18, 10))

        tk.Button(outer, text='Close',
                  bg=bg, fg=dim,
                  activebackground=dim, activeforeground=fg,
                  font=('Courier New', 10), relief='flat', cursor='hand2',
                  command=self.destroy).pack()

        # Centre over parent
        self.update_idletasks()
        x = parent_root.winfo_x() + (parent_root.winfo_width()  - self.winfo_width())  // 2
        y = parent_root.winfo_y() + (parent_root.winfo_height() - self.winfo_height()) // 2
        self.geometry(f'+{x}+{y}')


# ╔═══════════════════════════════════════════════════════════╗
# ║  MAIN WINDOW                                              ║
# ╚═══════════════════════════════════════════════════════════╝

class VFDWindow:
    """
    The main floating display window.
    Styled to resemble a real vacuum-fluorescent display.
    Drag anywhere to move. Always stays on top.
    """

    F_LABEL = ('Courier New', 9)

    def __init__(self, display: HUIMainDisplay, cfg: Config, router: MIDIRouter):
        self.display = display
        self.cfg     = cfg
        self.router  = router

        self.root = tk.Tk()
        self.root.title('HUI Main Display')
        self.root.resizable(False, False)
        self.root.attributes('-topmost', True)

        # Collect widget references for re-theming
        self._bg_widgets      = []
        self._fg_widgets      = []
        self._dim_widgets     = []
        self._display_labels  = []
        self._sep_widgets     = []
        self._border_widgets  = []

        self._build_menu()
        self._build_ui()
        self._bind_drag()
        self._schedule_tick()

    # ── Menu bar ──────────────────────────────────────────────
    def _build_menu(self):
        cfg = self.cfg
        bg  = cfg.bg_color
        fg  = cfg.fg_color
        dim = cfg.dim_color

        self._menubar = tk.Menu(
            self.root,
            bg=bg, fg=fg,
            activebackground=dim, activeforeground=fg,
            relief='flat', borderwidth=0
        )
        self.root.config(menu=self._menubar)

        self._app_menu = tk.Menu(
            self._menubar, tearoff=0,
            bg=bg, fg=fg,
            activebackground=dim, activeforeground=fg,
        )
        self._menubar.add_cascade(label='Menu', menu=self._app_menu)
        self._app_menu.add_command(label='Configure\u2026', command=self._open_config)
        self._app_menu.add_separator()
        self._app_menu.add_command(label='Exit', command=self.root.quit)

        self._menubar.add_command(label='About', command=self._open_about)

    # ── Display UI ────────────────────────────────────────────
    def _build_ui(self):
        cfg = self.cfg
        bg  = cfg.bg_color
        fg  = cfg.fg_color
        dim = cfg.dim_color

        self.root.configure(bg=bg)

        outer = tk.Frame(self.root, bg=bg,
                         highlightbackground='#003d35', highlightthickness=2,
                         padx=14, pady=10)
        outer.pack()
        self._outer = outer
        self._bg_widgets.append(outer)
        self._border_widgets.append(outer)

        title = tk.Label(outer,
                         text='  MACKIE HUI  -  MAIN DISPLAY  ',
                         font=self.F_LABEL, bg=bg, fg=dim)
        title.pack(pady=(0, 6))
        self._bg_widgets.append(title)
        self._dim_widgets.append(title)

        self._panel = tk.Frame(outer, bg=bg,
                               highlightbackground=dim, highlightthickness=1,
                               padx=10, pady=8)
        self._panel.pack()
        self._bg_widgets.append(self._panel)
        self._border_widgets.append(self._panel)

        self._row_vars = []
        for _ in range(_ROWS):
            v = tk.StringVar(value=' ' * _COLS)
            self._row_vars.append(v)
            lbl = tk.Label(self._panel, textvariable=v,
                           font=('Courier New', cfg.font_size, 'bold'),
                           bg=bg, fg=fg,
                           anchor='w', justify='left', width=_COLS)
            lbl.pack(anchor='w')
            self._bg_widgets.append(lbl)
            self._fg_widgets.append(lbl)
            self._display_labels.append(lbl)

        self._sep = tk.Frame(outer, bg=dim, height=1)
        self._sep.pack(fill='x', pady=(8, 2))
        self._sep_widgets.append(self._sep)

        self.status_var = tk.StringVar(value='Connecting...')
        self.status_lbl = tk.Label(outer, textvariable=self.status_var,
                                   font=self.F_LABEL, bg=bg, fg=dim)
        self.status_lbl.pack()
        self._bg_widgets.append(self.status_lbl)
        self._dim_widgets.append(self.status_lbl)

    # ── Drag to move ──────────────────────────────────────────
    def _bind_drag(self):
        self._ox = self._oy = 0
        self.root.bind('<ButtonPress-1>',
            lambda e: (setattr(self, '_ox', e.x), setattr(self, '_oy', e.y)))
        self.root.bind('<B1-Motion>',
            lambda e: self.root.geometry(
                f'+{self.root.winfo_x() + e.x - self._ox}'
                f'+{self.root.winfo_y() + e.y - self._oy}'))

    # ── Display refresh (~20 fps) ─────────────────────────────
    def _schedule_tick(self):
        self._tick()
        self.root.after(50, self._schedule_tick)

    def _tick(self):
        top, bot = self.display.get_rows()
        self._row_vars[0].set(top)
        self._row_vars[1].set(bot)

    # ── Status bar (thread-safe) ──────────────────────────────
    def set_status(self, text: str, error: bool = False):
        def _do():
            self.status_var.set(text)
            colour = '#ff5555' if error else self.cfg.dim_color
            self.status_lbl.configure(fg=colour)
        self.root.after(0, _do)

    # ── Config menu action ────────────────────────────────────
    def _open_config(self):
        ConfigDialog(self.root, self.cfg, self._on_config_applied)

    def _open_about(self):
        AboutDialog(self.root, self.cfg)

    def _on_config_applied(self):
        """Called by ConfigDialog after the user clicks Apply & Reconnect."""
        self.router.stop()
        self._apply_theme()
        self.router.start()

    # ── Re-theme all widgets after appearance change ──────────
    def _apply_theme(self):
        cfg  = self.cfg
        bg   = cfg.bg_color
        fg   = cfg.fg_color
        dim  = cfg.dim_color
        font = ('Courier New', cfg.font_size, 'bold')

        self.root.configure(bg=bg)
        for w in self._bg_widgets:
            w.configure(bg=bg)
        for w in self._fg_widgets:
            w.configure(fg=fg)
        for w in self._dim_widgets:
            w.configure(fg=dim)
        for w in self._sep_widgets:
            w.configure(bg=dim)
        for lbl in self._display_labels:
            lbl.configure(font=font)

        self._panel.configure(highlightbackground=dim)
        self._menubar.configure(bg=bg, fg=fg, activebackground=dim)
        self._app_menu.configure(bg=bg, fg=fg, activebackground=dim)

    def run(self):
        self.root.mainloop()


# ╔═══════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                              ║
# ╚═══════════════════════════════════════════════════════════╝

def main():
    if '--list-ports' in sys.argv:
        print('\n=== MIDI Input Ports ===')
        for n in mido.get_input_names():
            print(f'  {n}')
        print('\n=== MIDI Output Ports ===')
        for n in mido.get_output_names():
            print(f'  {n}')
        print()
        sys.exit(0)

    cfg     = Config()
    display = HUIMainDisplay()

    # Use an indirection cell so the router can call window.set_status
    # before the window object is fully constructed.
    _cb = {}
    router = MIDIRouter(cfg, display,
                        lambda t, error=False: _cb.get('fn', lambda *a, **k: None)(t, error=error))

    window = VFDWindow(display, cfg, router)
    _cb['fn'] = window.set_status

    router.start()
    window.run()


if __name__ == '__main__':
    main()
