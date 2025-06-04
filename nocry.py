import json
import os
import glob
import random
import threading
import time
import sys
import termios
import tty
import select
from pyo import *

os.environ['PYO_IGNORE_ALSA_WARNINGS'] = '1'
os.environ['PYO_IGNORE_PORTAUDIO_WARNINGS'] = '1'


# ---- LOAD CONFIG ----

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "sampler_config.json")
with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

POLYPHONY = config.get("POLYPHONY", 8)
LOOPS = config.get("LOOPS", {})
ONESHOTS = config.get("ONESHOTS", {})

MIDI_DEVICE_FILTER = config.get("MIDI_DEVICE_FILTER", "")
midi_connected = False
midi_device_index = -1

# ---- AUDIO SERVER ----

# Replace your current Server setup with this:
s = Server(
    sr=48000,       # Sample rate (try 48000 if 44100 fails)
    nchnls=2,       # Stereo output
    buffersize=256, # Lower = lower latency but higher CPU
    duplex=0,       # Disable input (0 = output only)
    audio='alsa'   # Force ALSA backend
)

pa_list_devices()
s.setOutputDevice(0)

# ---- MIDI SERVER ----
midi = None

while True:
    try:
        midilist = pm_get_input_devices()
        
        midi_device_index = -1
        midi_device_name = None
        
        print("Available MIDI devices:")
        for name, index in zip(*midilist):
            print(f"  {index}: {name}")
            if MIDI_DEVICE_FILTER.lower() in name.lower():
                midi_device_index = index
                midi_device_name = name
                
        if midi_device_index < 0:  # No devices found
            print("No MIDI devices found. Waiting for connection...")
            time.sleep(1)
            continue
        else:
            print("MIDI device found. Initializing...", midi_device_name)
            
        # Set the MIDI input device (1 is the first device)
        s.setMidiInputDevice(midi_device_index)
        # Wait for the server to initialize
        time.sleep(1)  # Give some time for the server to set up
        
        break
    except Exception as e:
        print(f"Error initializing MIDI: {e}")
        time.sleep(1)  # Retry after a short delay


s.boot()
s.start()

time.sleep(1)  # Allow time for server to start
print("PYO server started.")

# ---- FILE RESOLUTION ----

def resolve_files(folder, pattern):
    search_path = os.path.join(folder, pattern)
    files = glob.glob(search_path)
    if not files:
        print(f"WARNING: No files found for pattern {search_path}")
    return files


# ---- LOOPS ----

active_looper = None
active_looper_key = None

def stop_looper():
    global active_looper, active_looper_key
    if active_looper is not None and active_looper.isPlaying():
        active_looper.stop()
    active_looper = None
    active_looper_key = None

def rewind_looper():
    global active_looper
    if active_looper is not None and active_looper.isPlaying():
        active_looper.stop()
        active_looper.out()

def play_loop(files, key, rewind_on_retrigger=False):
    global active_looper, active_looper_key
    if not files:
        return
    filename = files  # Deterministic: pick first file
    if active_looper_key == key:
        if rewind_on_retrigger:
            rewind_looper()
        else:
            stop_looper()
        return
    stop_looper()
    print(f"Starting loop: {filename}")
    p = SfPlayer(filename, speed=1, loop=True, mul=0.8).out()
    active_looper = p
    active_looper_key = key

def stop_loop_event():
    stop_looper()
    print("Loop stopped by stop event")
    
    
# ---- ONESHOTS ----

active_oneshots = {}      # key -> SfPlayer (monophonic)
active_oneshot_poly = []  # list of polyphonic oneshots

def play_oneshot(files, key, poly=False):
    global active_oneshots, active_oneshot_poly
    if not files:
        return
    filename = random.choice(files)
    
    if poly:
        # Remove finished voices first
        active_oneshot_poly = [p for p in active_oneshot_poly if p.isPlaying()]
        
        # Voice stealing: remove oldest if polyphony limit reached
        if len(active_oneshot_poly) >= POLYPHONY:
            oldest = active_oneshot_poly.pop(0)  # FIFO: first in is first out
            oldest.stop()
            print("Voice stolen for polyphonic oneshot")
        
        # Start new voice
        print(f"Polyphonic oneshot: {filename}")
        p = SfPlayer(filename, speed=1, loop=False, mul=0.8).out()
        active_oneshot_poly.append(p)  # Add to end of list
        
    else:
        # Monophonic handling (existing code)
        prev = active_oneshots.get(key)
        if prev is not None and prev.isPlaying():
            prev.stop()
        print(f"Monophonic oneshot: {filename}")
        p = SfPlayer(filename, speed=1, loop=False, mul=0.8).out()
        active_oneshots[key] = p
        

# ---- PLAYER HANDLERS ----

def handle_loop_event(event_type, num, info):
    folder = LOOPS.get("path", "./loops/")
    pattern = info["file"]
    files = resolve_files(folder, pattern)
    key = f"{event_type}:{num}"
    retrigger = info.get("retrigger", False)
    play_loop(files, key, retrigger)

def handle_oneshot_event(event_type, num, info):
    folder = ONESHOTS.get("path", "./oneshots/")
    pattern = info["file"]
    files = resolve_files(folder, pattern)
    poly = info.get("poly", False)
    key = f"{event_type}:{num}"
    play_oneshot(files, key, poly=poly)


# ---- MIDI EVENTS ----

def handle_midi_event(status, data1, data2):
    
    event_code = status & 0xF0
    event_type = None
    event_channel = (status & 0x0F) + 1  # MIDI channels are 1-16
    
    if event_code == 0x90:    event_type = "noteon"
    elif event_code == 0x80:  event_type = "noteoff"
    elif event_code == 0xB0:  event_type = "cc"
    elif event_code == 0xC0:  event_type = "pc"
    else:
        print(f"Unhandled MIDI event: {status} {data1} {data2}")
        return

    print(event_type, data1, data2, "on channel", event_channel)
    
    key = str(data1)
    val = int(data2)
    
    if event_type == "noteon":
        if data2 > 0:  # Only handle Note On with velocity > 0
            if key in LOOPS.get("note", {}):
                info = LOOPS["note"][key]
                handle_loop_event("note", key, info)
            elif key in ONESHOTS.get("note", {}):
                info = ONESHOTS["note"][key]
                handle_oneshot_event("note", key, info)
                           
    elif event_type == "cc":
        if data2 > 0:  # Only handle cc value > 0
            if key in LOOPS.get("cc", {}):
                info = LOOPS["cc"][key]
                handle_loop_event("cc", key, info)
            elif key in ONESHOTS.get("cc", {}):
                info = ONESHOTS["cc"][key]
                handle_oneshot_event("cc", key, info)
                
    elif event_type == "pc":
        if key in LOOPS.get("program", {}):
            info = LOOPS["program"][key]
            handle_loop_event("program", key, info)
        elif key in ONESHOTS.get("program", {}):
            info = ONESHOTS["program"][key]
            handle_oneshot_event("program", key, info)
            
midi = RawMidi(handle_midi_event)


# ---- KEYBOARD EVENTS ----

def handle_key_event(key_str):
    # Loops
    if key_str in LOOPS.get("key", {}):
        info = LOOPS["key"][key_str]
        handle_loop_event("key", key_str, info)
    # Oneshots
    if key_str in ONESHOTS.get("key", {}):
        info = ONESHOTS["key"][key_str]
        handle_oneshot_event("key", key_str, info)

def keyboard_poll():
    def key_loop():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                dr, dw, de = select.select([sys.stdin], [], [], 0)
                if dr:
                    c = sys.stdin.read(1)
                    if c:
                        handle_key_event(c)
                time.sleep(0.01)
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    threading.Thread(target=key_loop, daemon=True).start()

keyboard_poll()


# ---- RUN

print("Sampler running. Press Ctrl+C to exit.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    s.stop()
    s.shutdown()


