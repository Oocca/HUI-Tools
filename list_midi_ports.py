#!/usr/bin/env python3
"""
list_midi_ports.py
──────────────────
Run this script to see the names of all MIDI devices
connected to your computer.

You will need these names to fill in the CONFIGURATION
section at the top of hui_display.py.

To run:
  1. Open Command Prompt (search for "cmd" in Start Menu)
  2. Type:  python list_midi_ports.py
  3. Press Enter
"""

try:
    import mido
except ImportError:
    print("\nERROR: The 'mido' library is not installed.")
    print("Please open Command Prompt and run:")
    print("    pip install mido python-rtmidi\n")
    input("Press Enter to close...")
    raise SystemExit

print()
print("=" * 45)
print("  MIDI INPUT PORTS (things you can READ from)")
print("=" * 45)
inputs = mido.get_input_names()
if inputs:
    for i, name in enumerate(inputs, 1):
        print(f"  {i}. {name}")
else:
    print("  (none found)")

print()
print("=" * 45)
print("  MIDI OUTPUT PORTS (things you can WRITE to)")
print("=" * 45)
outputs = mido.get_output_names()
if outputs:
    for i, name in enumerate(outputs, 1):
        print(f"  {i}. {name}")
else:
    print("  (none found)")

print()
print("─" * 45)
print("  TIP: Look for your HUI hardware port and")
print("  the 'HUI In' / 'HUI Out' loopMIDI ports.")
print("─" * 45)
print()
input("Press Enter to close...")
