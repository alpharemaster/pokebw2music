#!/usr/bin/env python3
"""Convert a MIDI file to the text of a Nitro sound sequence (tools/data/sseq.py), as pokeemerald's mid2agb does for
the GBA.

    midi_sseq.py IN.mid OUT.seq [SEQUENCE.json]

The packer (sound_data.py) converts a sequence's <name>.mid with the options of its JSON's "midi" field. Each MIDI
channel with events becomes the track of its number (channel 1 track 0, and so on; track 0 is always there to open
the others), and the program numbers pick the instruments of the sequence's bank, a bank select (controller 0)
adding 128 for each step. The conversion keeps:

- notes, with their velocities and lengths, and the tempo, at the sequence's 48 ticks per quarter note;
- program changes, or controller 85 for one (the export writes both, for hosts that don't pass a VST3 plugin
  program changes);
- program changes, pitch bend and its range (RPN 0), and the controllers of CONTROLLERS: volume (7), pan (10),
  expression (11), modulation (1), portamento time (5), switch (65) and key (84), and for the sound driver's own
  settings, which sseq_midi.py exports, track priority (14), modulation type (3), speed (76), range (77) and delay
  (78), the envelope's attack (73), decay (75), sustain (79) and release (72), and the pitch sweep (102 and 103, the
  high and low 7 bits of the sweep plus 8192);
- the loop: from a marker "[" (or "loopStart", or controller 111) to a marker "]" (or "loopEnd"), the start or the
  end of the song where one is missing. A song without markers plays once, unless the option "loop" loops all of it,
  and "loop": false turns the markers off. The tracks jump back to the start of the loop at its end, and set again
  there whatever changes inside it.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data.datajson import DataError, label  # noqa: E402
from tools.data.sseq import CODES, key_name  # noqa: E402

TICKS = 48  # A sequence's ticks per quarter note
LOOP_START = {"[", "loopstart", "loop_start", "loop start"}
LOOP_END = {"]", "loopend", "loop_end", "loop end"}
# The controllers kept, and the commands they become
CONTROLLERS = {1: "mod_depth", 3: "mod_type", 5: "portamento_time", 7: "volume", 10: "pan", 11: "expression",
               14: "priority", 65: "portamento_on", 72: "release", 73: "attack", 75: "decay", 76: "mod_speed",
               77: "mod_range", 78: "mod_delay", 79: "sustain", 84: "portamento"}
# The controller of each of those commands, by code, and the bend range's, which is RPN 0's data entry
COMMAND_CONTROLLERS = {CODES[name]: number for number, name in CONTROLLERS.items()} | {CODES["bend_range"]: 6}
# The controllers of the high and low 7 bits of the pitch sweep, plus 8192
SWEEP_PITCH = (102, 103)
# A program change's controller, with the bank select: VST3 plugins get controllers, but a host may not pass them
# program changes, so the export writes both
PROGRAM_CC = 85
# The state that a loop sets again at its start, in order
STATE = ["tempo", "program", "volume", "pan", "expression", "priority", "mod_depth", "mod_type", "mod_speed",
         "mod_range", "mod_delay", "attack", "decay", "sustain", "release", "portamento_time", "portamento_on",
         "portamento", "bend_range", "pitch_bend", "sweep_pitch"]


def read_varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[pos]
        pos += 1
        value = value << 7 | byte & 0x7F
        if not byte & 0x80:
            return value, pos


def read_midi(path: Path) -> tuple[int, list[tuple]]:
    """Returns a MIDI file's ticks per quarter note and its events, each (tick, order, kind, channel, data...), in
    time order."""
    data = path.read_bytes()
    where = label(path)
    if data[:4] != b"MThd":
        raise DataError(f"{where}: not a MIDI file")
    length, form, tracks, division = struct.unpack_from(">IHHH", data, 4)
    if division & 0x8000:
        raise DataError(f"{where}: SMPTE time is not supported; save it with ticks per quarter note")
    pos = 8 + length
    events = []
    order = 0
    for _ in range(tracks):
        if data[pos:pos + 4] != b"MTrk":
            raise DataError(f"{where}: bad track header")
        size = struct.unpack_from(">I", data, pos + 4)[0]
        pos += 8
        end = pos + size
        tick = 0
        status = 0
        while pos < end:
            delta, pos = read_varlen(data, pos)
            tick += delta
            if data[pos] & 0x80:
                status = data[pos]
                pos += 1
            kind = status & 0xF0
            channel = status & 0x0F
            if status == 0xFF:
                meta = data[pos]
                size_, pos = read_varlen(data, pos + 1)
                body = data[pos:pos + size_]
                pos += size_
                if meta == 0x51:
                    events.append((tick, order, "tempo", 0, int.from_bytes(body, "big")))
                elif meta in (0x06, 0x01):
                    events.append((tick, order, "marker", 0, body.decode("latin-1").strip().lower()))
                elif meta == 0x2F:
                    events.append((tick, order, "end", 0))
                status = 0
            elif status in (0xF0, 0xF7):
                size_, pos = read_varlen(data, pos)
                pos += size_
                status = 0
            elif kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                a, b = data[pos], data[pos + 1]
                pos += 2
                if kind == 0x90 and b:
                    events.append((tick, order, "on", channel, a, b))
                elif kind in (0x80, 0x90):
                    events.append((tick, order, "off", channel, a))
                elif kind == 0xB0:
                    events.append((tick, order, "cc", channel, a, b))
                elif kind == 0xE0:
                    events.append((tick, order, "bend", channel, a | b << 7))
            elif kind in (0xC0, 0xD0):
                if kind == 0xC0:
                    events.append((tick, order, "program", channel, data[pos]))
                pos += 1
            else:
                raise DataError(f"{where}: bad MIDI event {status:#x}")
            order += 1
        pos = end
    events.sort(key=lambda event: (event[0], event[1]))
    return division, events


def convert(path: Path, info: dict) -> str:
    """Returns the sequence text of a MIDI file, with the options of its sequence's JSON."""
    options = info.get("midi", {})
    where = label(path)
    division, events = read_midi(path)
    scale = lambda tick: round(tick * TICKS / division)  # noqa: E731
    transpose = options.get("transpose", 0)
    curve = options.get("velocity_curve", 1)

    loop_start = loop_end = None
    song_end = 0
    for event in events:
        tick, _, kind = event[:3]
        if kind == "marker" and event[4] in LOOP_START and loop_start is None:
            loop_start = scale(tick)
        elif kind == "marker" and event[4] in LOOP_END and loop_end is None:
            loop_end = scale(tick)
        elif kind == "cc" and event[4] == 111 and loop_start is None:
            loop_start = scale(tick)
        song_end = max(song_end, scale(tick))
    loop = options.get("loop", loop_start is not None or loop_end is not None)
    if not loop:
        loop_start = loop_end = None
    else:
        loop_start = loop_start or 0
        loop_end = loop_end if loop_end is not None else song_end
        if loop_end <= loop_start:
            raise DataError(f"{where}: the loop ends at tick {loop_end}, not after its start at {loop_start}")
    end = loop_end if loop else song_end

    # Each channel's commands, as (tick, order, text, state key and value)
    channels: dict[int, list] = {}
    tempos = []
    pending: dict[tuple[int, int], list] = {}
    banks = {}
    last_program = {}
    rpn = {}
    sweeps = {}
    order = 0
    for event in events:
        tick, _, kind, channel = event[:4]
        at = scale(tick)
        order += 1
        if kind == "tempo":
            bpm = max(1, min(0xFFFF, round(60_000_000 / event[4])))
            tempos.append((at, order, f"tempo {bpm}", "tempo", bpm))
            continue
        commands = channels.setdefault(channel, []) if kind in ("on", "program", "cc", "bend") else None
        if kind == "on":
            pending.setdefault((channel, event[4]), []).append((at, order, event[5]))
        elif kind == "off":
            starts = pending.get((channel, event[4]))
            if starts:
                start, start_order, velocity = starts.pop(0)
                channels[channel].append((start, start_order, ("note", event[4], velocity, at), None, None))
        elif kind == "program" or (kind == "cc" and event[4] == PROGRAM_CC):
            program = banks.get(channel, 0) * 128 + (event[4] if kind == "program" else event[5])
            # The export writes each program change as both, and one at the same tick is the same change
            if last_program.get(channel) != (at, program):
                last_program[channel] = (at, program)
                commands.append((at, order, f"program {program}", "program", program))
        elif kind == "cc":
            number, value = event[4], event[5]
            if number == 0:
                banks[channel] = value
            elif number in (101, 100):
                rpn[channel, number] = value
            elif number == 6 and rpn.get((channel, 101)) == 0 and rpn.get((channel, 100)) == 0:
                commands.append((at, order, f"bend_range {value}", "bend_range", value))
            elif number == SWEEP_PITCH[0]:
                sweeps[channel] = value
            elif number == SWEEP_PITCH[1]:
                value = (sweeps.get(channel, 64) << 7 | value) - 8192
                commands.append((at, order, f"sweep_pitch {value}", "sweep_pitch", value))
            elif number in CONTROLLERS:
                name = CONTROLLERS[number]
                if name == "portamento_on":
                    value = 1 if value >= 64 else 0
                commands.append((at, order, f"{name} {value}", name, value))
        elif kind == "bend":
            value = max(-128, min(127, round((event[4] - 8192) / 64)))
            commands.append((at, order, f"pitch_bend {value}", "pitch_bend", value))
    for (channel, key), starts in pending.items():
        for start, start_order, velocity in starts:
            channels[channel].append((start, start_order, ("note", key, velocity, end), None, None))

    used = sorted({0} | {channel for channel, commands in channels.items() if commands})
    lines = [f"\talloc_tracks 0x{sum(1 << channel for channel in used):04X}"]
    lines += [f"\topen_track {track}, Track_{track}" for track in used[1:]]
    for track in used:
        channel = track
        commands = channels.get(channel, []) + (tempos if track == 0 else [])
        commands.sort(key=lambda command: (command[0], isinstance(command[2], tuple), command[1]))
        if track:
            lines.append(f"Track_{track}:")
        # MIDI's defaults, where the track doesn't set its own at the start
        lines.append("\tnote_wait 0")
        first = {key for tick, _, _, key, _ in commands if key and tick == 0}
        defaults = {"priority": options.get("priority", 64), "bend_range": options.get("bend_range", 2),
                    "volume": 100, "pan": 64, "expression": 127}
        lines += [f"\t{key} {value}" for key, value in defaults.items() if key not in first]
        now = 0
        looped = False

        def values_before(tick):
            values = dict(defaults)
            for at, _, _, key, value in commands:
                if key and at < tick:
                    values[key] = value
            return values

        def wait_until(tick):
            nonlocal now
            if tick > now:
                lines.append(f"\twait {tick - now}")
                now = tick

        def start_loop():
            nonlocal looped
            wait_until(loop_start)
            lines.append(f"Loop_{track}:")
            # What the loop leaves changed, which the commands at its start don't set again
            before, after = values_before(loop_start), values_before(end)
            again = {key for at, _, _, key, _ in commands if key and at == loop_start}
            for key in STATE:
                if key in before and after.get(key) != before[key] and key not in again:
                    lines.append(f"\t{key} {before[key]}")
            looped = True

        for tick, _, command, key, value in commands:
            if tick >= end and (loop or tick > end):
                break
            if loop and not looped and tick >= loop_start:
                start_loop()
            wait_until(tick)
            if isinstance(command, tuple):
                _, note, velocity, off = command
                note += transpose
                if not 0 <= note < 0x80:
                    raise DataError(f"{where}: a note of channel {channel + 1} is out of range after transposing")
                length = max(1, min(off, end if loop else off) - tick)
                velocity = max(1, min(127, round(127 * (velocity / 127) ** curve)))
                lines.append(f"\tnote {key_name(note)}, {velocity}, {length}")
            else:
                lines.append(f"\t{command}")
        if loop:
            if not looped:
                start_loop()
            wait_until(end)
            lines.append(f"\tjump Loop_{track}")
        else:
            wait_until(end)
            lines.append("\tend")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if len(sys.argv) in (3, 4):
        info = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) == 4 else {}
        try:
            Path(sys.argv[2]).write_text(convert(Path(sys.argv[1]), info))
        except DataError as error:
            sys.exit(f"error: {error}")
    else:
        raise SystemExit(__doc__)
