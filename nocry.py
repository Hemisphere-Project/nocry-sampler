import json
import os
import glob
import random
import threading
import time
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
s.boot()
s.start()


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
        

# ---- MIDI HANDLERS ----

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

def notein_callback(midinote, velocity):
    note_str = str(midinote)
    # Loops
    if note_str in LOOPS.get("note", {}):
        info = LOOPS["note"][note_str]
        handle_loop_event("note", note_str, info)
    # Oneshots
    if note_str in ONESHOTS.get("note", {}):
        info = ONESHOTS["note"][note_str]
        handle_oneshot_event("note", note_str, info)

def cc_callback():
    # Loops
    for cc_num, info in LOOPS.get("cc", {}).items():
        cc_val = midi_cc.get(cc_num, 0)
        threshold = info.get("threshold", 1)
        if cc_val >= threshold:
            handle_loop_event("cc", cc_num, info)
    # Oneshots
    for cc_num, info in ONESHOTS.get("cc", {}).items():
        cc_val = midi_cc.get(cc_num, 0)
        threshold = info.get("threshold", 1)
        if cc_val >= threshold:
            handle_oneshot_event("cc", cc_num, info)

def pc_callback(pc_value):
    pc_str = str(pc_value)
    # Loops
    if pc_str in LOOPS.get("pc", {}):
        info = LOOPS["pc"][pc_str]
        handle_loop_event("pc", pc_str, info)
    # Oneshots
    if pc_str in ONESHOTS.get("pc", {}):
        info = ONESHOTS["pc"][pc_str]
        handle_oneshot_event("pc", pc_str, info)

# ---- MIDI INPUT ----

midi = Notein(poly=POLYPHONY)

# Note trigger
trig_note = TrigFunc(midi['trigon'], lambda: notein_callback(int(midi['pitch'].get()), int(midi['velocity'].get())))

# CC trigger (polling)
midi_cc = {}
def poll_cc():
    for cc_num in set(list(LOOPS.get("cc", {}).keys()) + list(ONESHOTS.get("cc", {}).keys())):
        cc = Midictl(int(cc_num), minscale=0, maxscale=127)
        midi_cc[cc_num] = int(cc.get())
    cc_callback()
    threading.Timer(0.01, poll_cc).start()
poll_cc()

# PC trigger (polling)
def poll_pc():
    pc = Programin()
    pc_value = int(pc.get())
    pc_callback(pc_value)
    threading.Timer(0.05, poll_pc).start()
poll_pc()

print("Sampler running. Press Ctrl+C to exit.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    s.stop()
    s.shutdown()
