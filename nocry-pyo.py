import json
import os
import glob
import random
import threading
import subprocess
import time
import sys
# import termios
import tty
import select
from pyo import *
from pythonosc.udp_client import SimpleUDPClient

os.environ['PYO_IGNORE_ALSA_WARNINGS'] = '1'
os.environ['PYO_IGNORE_PORTAUDIO_WARNINGS'] = '1'
os.environ['SDL_AUDIODRIVER'] = 'alsa'


# ---- LOAD CONFIG ----

#  config file path as argument or default to "sampler_config.json" in the same directory
CONFIG_FILE = sys.argv[1] if len(sys.argv) > 1 else None
if CONFIG_FILE is None or not os.path.exists(CONFIG_FILE):
    print("No config file provided or file does not exist. Using default 'sampler_config.json'.")
    CONFIG_FILE = os.path.join(os.path.dirname(__file__), "sampler_config.json")

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

POLYPHONY = config.get("POLYPHONY", 8)
LOOPS = config.get("LOOPS", {})
ONESHOTS = config.get("ONESHOTS", {})

LOOPS_PATH = LOOPS.get("path", "./loops/")
ONESHOTS_PATH = ONESHOTS.get("path", "./oneshots/")

OSC_PORT = config.get("OSC_PORT", 9000)
OSC_HOST = config.get("OSC_HOST", "127.0.0.1")
OSC_TARGET = SimpleUDPClient(OSC_HOST, OSC_PORT)

print(f"OSC configured to {OSC_HOST}:{OSC_PORT}")

MIDI_DEVICE_FILTER = config.get("MIDI_DEVICE_FILTER", "")
midi_connected = False
midi_device_index = -1

RUN = True

# ---- AUDIO SERVER ----

# Replace your current Server setup with this:
s = Server(
    sr=48000,       # Sample rate (try 48000 if 44100 fails)
    nchnls=2,       # Stereo output
    buffersize=32,  # Lower = lower latency but higher CPU
    duplex=0,       # Disable input (0 = output only)
)

# pa_list_devices()
s.setOutputDevice(1)

# ---- MIDI SERVER ----
midi = None

time.sleep(1)  # Give some time for the server to initialize

midi_device_index = -1
midi_device_name = None

print("Available MIDI devices:")
midilist = pm_get_input_devices()
for name, index in zip(*midilist):
    print(f"  {index}: {name}")
    if MIDI_DEVICE_FILTER.lower() in name.lower():
        midi_device_index = index
        midi_device_name = name
        
if midi_device_index < 0:  # No devices found
    print("No MIDI devices found. Exiting...")
    time.sleep(1)  # Wait a bit before retrying
    RUN = False
    sys.exit(1)

print("MIDI device found. Initializing...", midi_device_name)

# Set the MIDI input device
s.setMidiInputDevice(midi_device_index)
# Wait for the server to initialize
time.sleep(1)  # Give some time for the server to set up

# Dynamic midi devices list
def mididevices(filter=None):
    output = subprocess.check_output(['amidi', '-l'], text=True)
    lines = output.strip().split('\n')[1:]  # Skip header
    names = [line.split(None, 2)[2] for line in lines if len(line.split(None, 2)) > 2]
    if filter:
        names = [name for name in names if filter.lower() in name.lower()]
    return names
    
# Start watchdog thread to monitor MIDI connection
# quit if device is disconnected
def midi_watchdog():
    global midi_connected, midi_device_index, RUN
    while RUN:
        time.sleep(1)
        # list using amidi -l
        midilist = mididevices(MIDI_DEVICE_FILTER)
        if len(midilist) == 0:
            print("MIDI device disconnected. Exiting...")
            RUN = False
        
# Start the watchdog thread
watchdog_thread = threading.Thread(target=midi_watchdog, daemon=True)
watchdog_thread.start()
    

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

looper_volume = 1.0  # Default volume for loops
active_loopers = {}  # Replace active_looper/active_looper_key with dict

def stop_looper():
    global active_loopers
    for key in list(active_loopers.keys()):
        player = active_loopers[key]
        if player.isPlaying():
            player.stop()
        del active_loopers[key]

def play_loop(files, key, rewind_on_retrigger=False):
    global active_loopers, looper_volume
    if not files:
        return
    
    filename = files[0]  # Get first file from list
    
    # Handle retrigger logic
    if key in active_loopers:
        player = active_loopers[key]
        if rewind_on_retrigger:
            player.stop()
            player.out()
        else:
            player.stop()
            del active_loopers[key]
        return
    
    # Handle exclusivity
    if LOOPS.get("exclusive", True):
        stop_looper()
    
    # Start new player
    player = SfPlayer(filename, speed=1, loop=True, mul=looper_volume).out()
    player.filename = filename  # Attach filename for comparison
    active_loopers[key] = player
    
    
# ---- ONESHOTS ----

oneshots_volume = 1.0  # Default volume for oneshots
active_oneshots = {}      # key -> SfPlayer (monophonic)
active_oneshot_poly = []  # list of polyphonic oneshots

def play_oneshot(files, key, poly=False):
    global active_oneshots, active_oneshot_poly
    if not files:
        return
    filename = random.choice(files)
    exclusive = ONESHOTS.get("exclusive", False)

    if exclusive:
        if poly:
            # Stop all oneshots except already playing instances of this file
            # Remove finished voices first
            active_oneshot_poly = [p for p in active_oneshot_poly if p.isPlaying()]
            # Stop all poly voices not matching this file
            for p in active_oneshot_poly[:]:
                if getattr(p, "filename", None) != filename:
                    p.stop()
                    active_oneshot_poly.remove(p)
            # Stop all monophonic oneshots not matching this file
            for k in list(active_oneshots.keys()):
                p = active_oneshots[k]
                if getattr(p, "filename", None) != filename:
                    p.stop()
                    del active_oneshots[k]
            # Voice stealing if needed
            if len([p for p in active_oneshot_poly if getattr(p, "filename", None) == filename]) >= POLYPHONY:
                oldest = next(p for p in active_oneshot_poly if getattr(p, "filename", None) == filename)
                oldest.stop()
                active_oneshot_poly.remove(oldest)
            # Start new voice
            p = SfPlayer(filename, speed=1, loop=False, mul=oneshots_volume).out()
            p.filename = filename  # Attach filename for comparison
            active_oneshot_poly.append(p)
        else:
            # Stop all oneshots (poly and mono)
            for p in active_oneshot_poly:
                p.stop()
            active_oneshot_poly.clear()
            for k in list(active_oneshots.keys()):
                p = active_oneshots[k]
                p.stop()
                del active_oneshots[k]
            # Start new monophonic oneshot
            p = SfPlayer(filename, speed=1, loop=False, mul=oneshots_volume).out()
            p.filename = filename
            active_oneshots[key] = p
    else:
        if poly:
            # Polyphonic: just add a new instance, no stopping
            active_oneshot_poly = [p for p in active_oneshot_poly if p.isPlaying()]
            if len(active_oneshot_poly) >= POLYPHONY:
                oldest = active_oneshot_poly.pop(0)
                oldest.stop()
            p = SfPlayer(filename, speed=1, loop=False, mul=oneshots_volume).out()
            p.filename = filename
            active_oneshot_poly.append(p)
        else:
            # Monophonic: stop only other instances of this file
            # Stop all mono oneshots with the same filename
            for k in list(active_oneshots.keys()):
                p = active_oneshots[k]
                if getattr(p, "filename", None) == filename:
                    p.stop()
                    del active_oneshots[k]
            # Stop all poly oneshots with the same filename
            for p in active_oneshot_poly[:]:
                if getattr(p, "filename", None) == filename:
                    p.stop()
                    active_oneshot_poly.remove(p)
            # Start new monophonic oneshot
            p = SfPlayer(filename, speed=1, loop=False, mul=oneshots_volume).out()
            p.filename = filename
            active_oneshots[key] = p
            

def stop_all_oneshots():
    global active_oneshots, active_oneshot_poly
    for key, player in active_oneshots.items():
        if player.isPlaying():
            player.stop()
    active_oneshots.clear()
    
    for player in active_oneshot_poly:
        if player.isPlaying():
            player.stop()
    active_oneshot_poly.clear()
    
    print("All oneshots stopped.")        

# ---- PLAYER HANDLERS ----

def handle_loop_event(event_type, num, info):
    
    if not info or "file" not in info: return
    
    pattern = info["file"]
    if pattern == 'stop':
        stop_looper()
        print("Loop stopped by stop event")
        return
    
    if pattern == 'volume':
        global looper_volume
        looper_volume = info.get("value", 127) / 127.0
        for key in list(active_loopers.keys()):
            active_loopers[key].setMul(looper_volume)
        return
    
    files = resolve_files(LOOPS_PATH, pattern)
    key = f"{event_type}:{num}"
    retrigger = info.get("retrigger", False)
    play_loop(files, key, retrigger)


def handle_oneshot_event(event_type, num, info):
    pattern = info["file"]
    if pattern == 'stop':
        stop_all_oneshots()
        return
    
    if pattern == 'volume':
        global oneshots_volume
        oneshots_volume = info.get("value", 127) / 127.0
        for key, player in active_oneshots.items():
            player.setMul(oneshots_volume)
        for player in active_oneshot_poly:
            player.setMul(oneshots_volume)
        return
    
    files = resolve_files(ONESHOTS_PATH, pattern)
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
                info['value'] = val  # Store velocity for loops
                handle_loop_event("note", key, info)
            elif key in ONESHOTS.get("note", {}):
                info = ONESHOTS["note"][key]
                info['value'] = val  # Store velocity for oneshots
                handle_oneshot_event("note", key, info)
                           
    elif event_type == "cc":
        if data2 > 0:  # Only handle cc value > 0
            if key in LOOPS.get("cc", {}):
                info = LOOPS["cc"][key]
                info['value'] = val
                handle_loop_event("cc", key, info)
            elif key in ONESHOTS.get("cc", {}):
                info = ONESHOTS["cc"][key]
                info['value'] = val
                handle_oneshot_event("cc", key, info)
                
    elif event_type == "pc":
        if key in LOOPS.get("program", {}):
            info = LOOPS["program"][key]
            handle_loop_event("program", key, info)
        elif key in ONESHOTS.get("program", {}):
            info = ONESHOTS["program"][key]
            handle_oneshot_event("program", key, info)
            
    # handle OSC
    osc = info['osc']
    if osc:
        osc = osc.strip().split(' ')
        path = osc[0]
        arg = int(osc[1]) if len(osc) > 1 else 0
        OSC_TARGET.send_message(path, arg)
        print(f"Sending OSC: {path} {arg} to {OSC_HOST}:{OSC_PORT}")
            
midi = RawMidi(handle_midi_event)


# ---- KEYBOARD EVENTS ----

# def handle_key_event(key_str):
#     # Loops
#     if key_str in LOOPS.get("key", {}):
#         info = LOOPS["key"][key_str]
#         handle_loop_event("key", key_str, info)
#     # Oneshots
#     if key_str in ONESHOTS.get("key", {}):
#         info = ONESHOTS["key"][key_str]
#         handle_oneshot_event("key", key_str, info)

# def keyboard_poll():
#     def key_loop():
#         fd = sys.stdin.fileno()
#         old_settings = termios.tcgetattr(fd)
#         try:
#             tty.setcbreak(fd)
#             while RUN:
#                 dr, dw, de = select.select([sys.stdin], [], [], 0)
#                 if dr:
#                     c = sys.stdin.read(1)
#                     if c:
#                         handle_key_event(c)
#                 time.sleep(0.01)
#         except Exception:
#             pass
#         finally:
#             termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
#     threading.Thread(target=key_loop, daemon=True).start()

# keyboard_poll()


# ---- RUN

print("Sampler running. Press Ctrl+C to exit.")
try:
    while RUN:
        time.sleep(1)
except KeyboardInterrupt:
    RUN = False
    
s.stop()
s.shutdown()


