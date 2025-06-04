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

import pygame
import pygame.midi

# ---- LOAD CONFIG ----

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

MIDI_DEVICE_FILTER = config.get("MIDI_DEVICE_FILTER", "")

# ---- AUDIO SERVER ----

pygame.mixer.pre_init(48000, -16, 2, 64)  # low latency
pygame.init()
pygame.mixer.init(devicename="USB PnP Audio Device, USB Audio")
print("PyGame mixer initialized.")

# ---- MIDI SERVER ----

pygame.midi.init()
midi_input_id = None
for i in range(pygame.midi.get_count()):
    interf, name, is_input, is_output, opened = pygame.midi.get_device_info(i)
    if is_input and (MIDI_DEVICE_FILTER.lower() in name.decode().lower()):
        midi_input_id = i
        print(f"Using MIDI input device: {name.decode()}")
        break
if midi_input_id is None:
    print("No suitable MIDI input device found.")
    sys.exit(1)
midi_in = pygame.midi.Input(midi_input_id)

# ---- FILE RESOLUTION ----

def resolve_files(folder, pattern):
    search_path = os.path.join(folder, pattern)
    files = glob.glob(search_path)
    if not files:
        print(f"WARNING: No files found for pattern {search_path}")
    return files

# ---- LOOPS ----


active_looper = None
active_looper_sound = None
active_looper_key = None

def stop_looper():
    global active_looper, active_looper_key
    if active_looper is not None:
        active_looper.stop()
    active_looper = None
    active_looper_key = None
    
def retrigger_loop():
    global active_looper
    if active_looper is not None and active_looper.get_busy():
        active_looper.stop()  # Stop immediately
    if active_looper_sound is not None:
        print("Retriggering loop from beginning")
        active_looper = active_looper_sound.play(loops=-1)  # Start from beginning

def play_loop(files, key, rewind_on_retrigger=False):
    global active_looper, active_looper_key, active_looper_sound
    if not files:
        return
    filename = files[0]  # Deterministic: pick first file
    if active_looper_key == key:
        if rewind_on_retrigger:
            retrigger_loop()
        else:
            stop_looper()
        return
    stop_looper()
    print(f"Starting loop: {filename}")
    active_looper_sound = pygame.mixer.Sound(filename)
    active_looper = active_looper_sound.play(loops=-1)
    active_looper_key = key

def stop_loop_event():
    stop_looper()
    print("Loop stopped by stop event")

# ---- ONESHOTS ----

active_oneshots = {}      # key -> Channel (monophonic)
active_oneshot_poly = []  # list of (Channel, Sound) tuples

def play_oneshot(files, key, poly=False):
    global active_oneshots, active_oneshot_poly
    if not files:
        return
    filename = random.choice(files)
    sound = pygame.mixer.Sound(filename)
    if poly:
        # Remove finished voices first
        active_oneshot_poly = [(ch, snd) for ch, snd in active_oneshot_poly if ch.get_busy()]
        # Voice stealing: remove oldest if polyphony limit reached
        if len(active_oneshot_poly) >= POLYPHONY:
            oldest_ch, oldest_snd = active_oneshot_poly.pop(0)
            oldest_ch.stop()
            print("Voice stolen for polyphonic oneshot")
        print(f"Polyphonic oneshot: {filename}")
        channel = sound.play()
        active_oneshot_poly.append((channel, sound))
    else:
        prev = active_oneshots.get(key)
        if prev is not None and prev.get_busy():
            prev.stop()
        print(f"Monophonic oneshot: {filename}")
        channel = sound.play()
        active_oneshots[key] = channel

# ---- PLAYER HANDLERS ----

def handle_loop_event(event_type, num, info):
    pattern = info["file"]
    files = resolve_files(LOOPS_PATH, pattern)
    key = f"{event_type}:{num}"
    retrigger = info.get("retrigger", False)
    play_loop(files, key, retrigger)

def handle_oneshot_event(event_type, num, info):
    pattern = info["file"]
    files = resolve_files(ONESHOTS_PATH, pattern)
    poly = info.get("poly", False)
    key = f"{event_type}:{num}"
    play_oneshot(files, key, poly=poly)

# ---- MIDI EVENTS ----

def handle_midi_event(status, data1, data2, data3=None):
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
        if data2 > 0:
            if key in LOOPS.get("note", {}):
                info = LOOPS["note"][key]
                handle_loop_event("note", key, info)
            elif key in ONESHOTS.get("note", {}):
                info = ONESHOTS["note"][key]
                handle_oneshot_event("note", key, info)
    elif event_type == "cc":
        if data2 > 0:
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

# ---- KEYBOARD EVENTS ----

def handle_key_event(key_str):
    if key_str in LOOPS.get("key", {}):
        info = LOOPS["key"][key_str]
        handle_loop_event("key", key_str, info)
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
        # MIDI polling
        if midi_in.poll():
            midi_events = midi_in.read(10)
            for event in midi_events:
                data = event[0]
                handle_midi_event(*data)
        time.sleep(0.01)
except KeyboardInterrupt:
    pygame.mixer.quit()
    pygame.midi.quit()
    print("Shutting down.")

