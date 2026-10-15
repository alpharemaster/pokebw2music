#!/usr/bin/env python3
"""Render a Nitro sound sequence as a MIDI file that plays as the game does, for a synthesizer with the bank's DLS
file (sound_dls.py writes both).

A sequence's own MIDI export (sseq_midi.py) keeps the driver's portamento, pitch sweep, modulation and envelope
commands as controllers, which no synthesizer plays as the DS does. The preview instead runs the sequence as the
ARM7 sound driver does, frame by frame (2728 * 64 cycles), and writes what each channel does as plain MIDI:
- the pitch, with the bend, the portamento and pitch sweep (0x03802620: the sweep, plus (portamento key - key) * 64
  with portamento on, falls to 0 over (time ** 2 * |sweep|) >> 11 frames, or over the note's ticks for a time of 0)
  and the pitch modulation, as pitch bends at the frames' times, with a bend range wide enough for them;
- the volume and pan modulation, as expression (11) and pan (10). The modulation (0x03801418, 0x03801484) starts
  again with each note, waits its delay in frames, and moves through a 128-step sine by speed * 64 / 256 steps a
  frame; its sine * depth * range is a pitch of (value * 64) >> 14 in 1/64 semitones, a volume of (value * 60) >> 14
  in 0.1 dB or a pan of (value * 64) >> 14;
- the volume, as the track's volume (7) with the player's volume and the sequence's own folded in, on the driver's
  curve of 40 * log10(value / 127) dB, which is DLS's too;
- the attack, decay, sustain and release that a track sets, as instruments of their own, which the DLS file has as
  the variant programs that render() returns.
Where a track plays several notes at once, its channel follows the newest one's pitch and modulation, as a MIDI
channel has one pitch bend.
"""
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data import sseq  # noqa: E402
from tools.data.datajson import DataError  # noqa: E402
from tools.data.sseq_midi import render as render_tracks, song_loop, varlen, midi_track  # noqa: E402

FRAME = 2728 * 64 / 33513982
PPQN = 480                      # The preview's ticks per quarter note: ten for each of the sequence's
SCALE = PPQN // 48
SINE = [0, 6, 12, 19, 25, 31, 37, 43, 49, 54, 60, 65, 71, 76, 81, 85, 90, 94, 98, 102, 106, 109, 112, 115, 117, 120,
        122, 123, 125, 126, 126, 127, 127]
NO_ENVELOPE = 255
MAX_SECONDS = 20 * 60


def decibels(value: int) -> int:
    """The driver's decibel table (0x03808e48): 0.1 dB of a 0-127 volume."""
    return -32768 if value <= 0 else max(-722, round(400 * math.log10(min(127, value) / 127)))


def midi_volume(tenths: float) -> int:
    """The 0-127 value whose 40 * log10 curve gives an attenuation in 0.1 dB."""
    return max(0, min(127, round(127 * 10 ** (tenths / 400))))


def sine(index: int) -> int:
    if index < 32:
        return SINE[index]
    if index < 64:
        return SINE[64 - index]
    if index < 96:
        return -SINE[index - 64]
    return -SINE[128 - index]


class Note:
    def __init__(self, key, velocity, length, variant, order):
        self.key = key
        self.velocity = velocity
        self.length = length
        self.variant = variant
        self.order = order
        self.sweep = 0
        self.sweep_length = 0
        self.sweep_count = 0
        self.sweep_by_tick = False
        self.lfo_delay_count = 0
        self.lfo_count = 0
        self.lfo = None


class Track:
    def __init__(self, number, pc):
        self.number = number
        self.pc = pc
        self.stack = []
        self.wait = 0
        self.active = True
        self.note_wait = True
        self.portamento = False
        self.portamento_key = 60
        self.portamento_time = 0
        self.sweep = 0
        self.transpose = 0
        self.volume = 127
        self.expression = 127
        self.pan = 0
        self.bend = 0
        self.bend_range = 2
        self.lfo = [0, 16, 0, 1, 0]  # Type, speed, depth, range, delay
        self.envelope = [NO_ENVELOPE] * 4
        self.program = 0
        self.notes = []


def render(text: str, volume: int, path="<sequence>"):
    """Plays a sequence as the driver does. Returns its tempo events, markers and channels' events, each
    (preview tick, kind, values), the instruments as (program, attack, decay, sustain, release) with 255 for the
    bank's own, and the preview's length."""
    data = sseq.assemble(text, path)
    commands = sseq.decode(sseq.strip_padding(data[0x1C:]))
    index = {command.offset: i for i, command in enumerate(commands)}
    try:
        loop = song_loop(render_tracks(commands)[0])
    except DataError as error:
        raise DataError(f"{path}: {error}") from None
    end_tick = loop[1] if loop else None
    tracks = {0: Track(0, 0)}
    main_volume = 127
    tempo = 120
    counter = 240           # The driver's tempo counter starts full, so the first frame plays a tick
    ticks = 0
    order = 0
    events = []             # (time, sort, kind, track, values...)
    conductor = []

    def note_on(track, key, velocity, length):
        nonlocal order
        order += 1
        if track.envelope == [NO_ENVELOPE] * 4:
            variant = (track.program,) + (NO_ENVELOPE,) * 4
        else:
            variant = (track.program, *track.envelope)
        note = Note(key, velocity, length, variant, order)
        note.sweep = track.sweep
        if track.portamento:
            note.sweep += (track.portamento_key - key) * 64
        if track.portamento_time == 0:
            note.sweep_length = length
            note.sweep_by_tick = True
        else:
            note.sweep_length = (track.portamento_time ** 2 * abs(note.sweep)) >> 11
        track.portamento_key = key
        note.lfo = list(track.lfo)
        track.notes.append(note)
        events.append((ticks * SCALE, 2, "on", track.number, note))

    def step(track):
        """Runs a track's commands until it waits."""
        while track.active and track.wait == 0:
            command = commands[index[track.pc]]
            if command.prefixes:
                raise DataError(f"{path}: the {' '.join(command.prefixes)} prefix has no preview")
            nxt = commands[index[track.pc] + 1].offset if index[track.pc] + 1 < len(commands) else None
            code, args = command.code, command.args
            track.pc = nxt
            if code < 0x80:
                key = args[0] + track.transpose
                if 0 <= key < 0x80:
                    note_on(track, key, args[1], max(1, args[2]))
                if track.note_wait:
                    track.wait = args[2]
            elif code == 0x80:
                track.wait = args[0]
            elif code == 0x81:
                track.program = args[0]
            elif code == 0x93:
                tracks[args[0]] = Track(args[0], args[1])
            elif code == 0x94:
                track.pc = args[0]
            elif code == 0x95:
                track.stack.append(track.pc)
                track.pc = args[0]
            elif code == 0xFD:
                if track.stack:
                    track.pc = track.stack.pop()
                else:
                    track.active = False
            elif code == 0xFF:
                track.active = False
            elif code == 0xE1:
                nonlocal tempo
                tempo = args[0]
                conductor.append((ticks * SCALE, "tempo", tempo))
            elif code == 0xC0:
                track.pan = args[0] - 64
            elif code == 0xC1:
                track.volume = args[0]
            elif code == 0xC2:
                nonlocal main_volume
                main_volume = args[0]
            elif code == 0xC3:
                track.transpose = args[0]
            elif code == 0xC4:
                track.bend = args[0]
            elif code == 0xC5:
                track.bend_range = args[0]
            elif code == 0xC7:
                track.note_wait = bool(args[0])
            elif code == 0xC9:
                track.portamento_key = args[0] + track.transpose
                track.portamento = True
            elif code == 0xCE:
                track.portamento = bool(args[0] & 1)
            elif code == 0xCF:
                track.portamento_time = args[0]
            elif code in (0xCA, 0xCB, 0xCC, 0xCD):
                track.lfo[{0xCC: 0, 0xCB: 1, 0xCA: 2, 0xCD: 3}[code]] = args[0]
            elif code == 0xE0:
                track.lfo[4] = args[0]
            elif code == 0xE3:
                track.sweep = args[0]
            elif code in (0xD0, 0xD1, 0xD2, 0xD3):
                track.envelope[code - 0xD0] = args[0]
            elif code in (0xD5,):
                track.expression = args[0]
            # The track allocation, priority, tie, loops and debug print change nothing the preview plays

    def tick():
        nonlocal ticks
        # The driver runs the tracks in order, so a track runs in the tick that a lower one opens it
        for number in range(16):
            track = tracks.get(number)
            if track is None:
                continue
            for note in list(track.notes):
                if note.sweep_by_tick and note.sweep_count < note.sweep_length:
                    note.sweep_count += 1
                note.length -= 1
                if note.length <= 0:
                    track.notes.remove(note)
                    events.append((ticks * SCALE, 1, "off", track.number, note))
            if track.wait > 0:
                track.wait -= 1
            step(track)
            for note in track.notes:
                note.lfo = list(track.lfo)
        ticks += 1

    seq_db = decibels(volume)
    frames = 0
    states = {}
    while frames * FRAME < MAX_SECONDS:
        # The frame's time: that of its first tick if it plays one, so that a note's pitch comes with it
        start_ticks, start_counter = ticks, counter
        time = start_ticks * SCALE if start_counter >= 240 else round((start_ticks + (start_counter - 240) / 240)
                                                                       * SCALE)
        while counter >= 240:
            counter -= 240
            if end_tick is not None and ticks >= end_tick:
                break
            tick()
        if end_tick is not None and ticks >= end_tick:
            break
        if end_tick is None and not any(t.active for t in tracks.values()) and not any(
                t.notes for t in tracks.values()):
            break
        for track in tracks.values():
            volume_db = decibels(track.volume) + decibels(main_volume) + seq_db
            expression_db = decibels(track.expression)
            pan = track.pan
            pitch = (track.bend * track.bend_range * 64) >> 7
            newest = max(track.notes, key=lambda n: n.order) if track.notes else None
            for note in track.notes:
                offset = 0
                if note.sweep and note.sweep_count < note.sweep_length:
                    # The driver's division truncates toward 0
                    offset = abs(note.sweep) * (note.sweep_length - note.sweep_count) // note.sweep_length
                    offset = offset if note.sweep > 0 else -offset
                if not note.sweep_by_tick and note.sweep_count < note.sweep_length:
                    note.sweep_count += 1
                kind, speed, depth, scope, delay = note.lfo
                value = 0
                if depth and note.lfo_delay_count >= delay:
                    value = sine(note.lfo_count >> 8) * depth * scope
                if note.lfo_delay_count < delay:
                    note.lfo_delay_count += 1
                else:
                    position = note.lfo_count + (speed << 6)
                    phase = position >> 8
                    while phase >= 128:
                        phase -= 128
                    note.lfo_count = (position & 0xFF) | phase << 8
                if note is newest:
                    if kind == 0:
                        pitch += offset + ((value << 6) >> 14)
                    else:
                        pitch += offset
                        if kind == 1:
                            expression_db += (value * 60) >> 14
                        elif kind == 2:
                            pan += (value << 6) >> 14
            state = (pitch, midi_volume(volume_db), midi_volume(expression_db), max(0, min(127, pan + 64)))
            if states.get(track.number) != state:
                states[track.number] = state
                events.append((time, 0, "state", track.number, *state))
        counter += tempo
        frames += 1
    end = ticks * SCALE
    for track in tracks.values():
        for note in track.notes:
            events.append((end, 1, "off", track.number, note))
    if loop:
        conductor += [(loop[0] * SCALE, "marker", "["), (loop[1] * SCALE, "marker", "]")]
    variants = sorted({e[4].variant for e in events if e[2] == "on"})
    return conductor, events, variants, end


def preview_midi(conductor, events, programs: dict, end: int) -> bytes:
    """Writes a render as a MIDI file, with the (bank, program) of each instrument of the render."""
    channels = {}
    for event in sorted(events, key=lambda e: (e[0], e[1])):
        channels.setdefault(event[3], []).append(event)
    chunks = []
    meta = [(0, b"\xff\x03\x07Preview")]
    for time, kind, value in sorted(conductor, key=lambda c: c[0]):
        if kind == "tempo":
            meta.append((time, b"\xff\x51\x03" + round(48 * 240 * FRAME * 1e6 / max(1, value)).to_bytes(3, "big")))
        else:
            meta.append((time, b"\xff\x06" + varlen(len(value)) + value.encode()))
    chunks.append(midi_track(meta + [(end, b"\xff\x01\0")]))
    for number, items in sorted(channels.items()):
        channel = number & 0xF
        reach = max([abs(e[4]) for e in items if e[2] == "state"] + [64])
        semitones = min(127, max(2, math.ceil(reach / 64)))
        name = f"Track {number}".encode()
        out = [(0, b"\xff\x03" + varlen(len(name)) + name)]
        out += [(0, bytes((0xB0 | channel, number_, value))) for number_, value in
                ((101, 0), (100, 0), (6, semitones), (38, 0))]
        last = {}
        program = None
        for event in items:
            time, _, kind = event[:3]
            if kind == "state":
                _, _, _, _, pitch, volume, expression, pan = event
                wheel = max(0, min(0x3FFF, 8192 + round(pitch / 64 / semitones * 8192)))
                for key, body in (("bend", bytes((0xE0 | channel, wheel & 0x7F, wheel >> 7))),
                                  ("volume", bytes((0xB0 | channel, 7, volume))),
                                  ("expression", bytes((0xB0 | channel, 11, expression))),
                                  ("pan", bytes((0xB0 | channel, 10, pan)))):
                    if last.get(key) != body:
                        last[key] = body
                        out.append((time, body))
            elif kind == "on":
                note = event[4]
                bank, number_ = programs[note.variant]
                if program != (bank, number_):
                    program = (bank, number_)
                    out += [(time, bytes((0xB0 | channel, 0, bank))), (time, bytes((0xC0 | channel, number_)))]
                out.append((time, bytes((0x90 | channel, note.key, max(1, note.velocity)))))
            else:
                out.append((time, bytes((0x80 | channel, event[4].key, 0))))
        chunks.append(midi_track(out + [(end, b"\xff\x01\0")]))
    return b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), PPQN) + b"".join(chunks)
