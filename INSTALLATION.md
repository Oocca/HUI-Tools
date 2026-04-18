# Installation

You only need to complete this setup once. After that, running either program is just a double-click.

> **Note:** HUI-Test does not require loopMIDI. If you only intend to use HUI-Test, you can skip Step 2.

---

## Step 1 — Install Python

Both programs are written in Python and require it to be installed on your computer.

1. Go to **https://www.python.org/downloads/** and click the large **Download Python** button.
2. Run the installer.
3. > ⚠️ **CRITICAL:** On the first screen of the installer, tick the checkbox labelled **"Add Python to PATH"** before clicking Install Now. If you skip this, the programs will not run.
4. Click **Install Now** and wait for it to complete.

---

## Step 2 — Install loopMIDI *(HUI-Display only)*

HUI-Display needs virtual MIDI ports to sit between Pro Tools and the HUI hardware. loopMIDI is a free Windows utility that creates these ports.

1. Go to **https://www.tobias-erichsen.de/software/loopmidi.html** and download loopMIDI.
2. Run the installer.
3. Launch loopMIDI. It will appear as a small icon in the Windows system tray (bottom-right corner of the screen).
4. In the text box at the bottom of the loopMIDI window, type `HUI In` and click the **+** button. A new port will appear in the list.
5. Repeat: type `HUI Out` and click **+** again. You should now see both ports in the list.

> 💡 **Tip:** In loopMIDI's Options menu, enable **"Autostart with Windows"** so the virtual ports are available every time you start your computer without needing to open loopMIDI manually.

---

## Step 3 — Install Python Libraries

Both programs rely on two Python libraries: `mido` (MIDI handling) and `python-rtmidi` (communication with your MIDI hardware). Install them using Command Prompt.

### Quick method

1. Open Command Prompt — press the Windows key, type `cmd`, and press Enter.
2. Run the following command to update the package installer:
   ```
   python -m pip install --upgrade pip
   ```
3. Then run:
   ```
   pip install mido python-rtmidi
   ```
4. Wait for the installation to complete. You may see a message about "scripts not on PATH" — this is harmless and can be ignored.

### If the installation fails

If you see an error message containing the words **"Unknown compiler"** or **"Visual Studio"**, `python-rtmidi` needs a C++ compiler that is not installed on your system. Follow these steps:

1. Go to **https://visualstudio.microsoft.com/visual-cpp-build-tools/** and click **Download Build Tools**.
2. Run the installer. When it asks what to install, tick **"Desktop development with C++"** and click Install. The download is approximately 1–2 GB.
3. Restart your computer.
4. Open Command Prompt again and re-run:
   ```
   pip install mido python-rtmidi
   ```

---

## Step 4 — Find Your HUI Port Names

Windows appends a number to every MIDI port name (for example, a Roland UM-ONE interface might appear as `UM-ONE 2` in the input list and `UM-ONE 3` in the output list). You need these exact names to configure the programs.

To see all available MIDI ports on your system, run the helper script:

```
python list_midi_ports.py
```

This will display two lists — input ports and output ports — with the full name of each device including the trailing number. Write down the names of your HUI hardware's input and output ports; you will need them when configuring either program.

> **Note:** Port numbers can change if you plug or unplug USB devices, or change the order in which they are powered on. If a program stops connecting after you have changed your USB setup, run `list_midi_ports.py` again to find the new numbers.

---

## Files in This Package

| File | Description |
|------|-------------|
| `hui_display.py` | HUI-Display — run this during Pro Tools sessions |
| `hui_test.py` | HUI-Test — run this for hardware diagnostics |
| `list_midi_ports.py` | Helper utility that lists all MIDI devices on your system |
| `config.hui` | Shared settings file, created automatically on first launch |
| `SETUP_INSTRUCTIONS.txt` | Quick-reference setup guide |

---

## Quick-Start After Installation

### HUI-Display
1. Start loopMIDI (or let it start automatically with Windows).
2. Turn on your HUI hardware.
3. Double-click `hui_display.py`.
4. Click **Menu → Configure** and set your MIDI ports.
5. In Pro Tools: **Setup → Peripherals → MIDI Controllers** — set Send To: `HUI In`, Receive From: `HUI Out`.

### HUI-Test
1. Turn on your HUI hardware.
2. Double-click `hui_test.py`.
3. Click **Menu → Configure** and set your HUI hardware MIDI ports.
4. Click **Connect**.

> ⚠️ **Do not run HUI-Display and HUI-Test at the same time** while Pro Tools is connected. See the user manual for details.

---

*HUI Tools © 2026 Richard Philip — released under the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html#license-text)*
