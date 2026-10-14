#!/usr/bin/env python3
"""Convert a MIDI file to the text of a Nitro sound sequence (tools/data/sseq.py), as pokeemerald's mid2agb does for
the GBA.

    midi_sseq.py IN.mid OUT.seq [SEQUENCE.json]

The packer (sound_data.py) converts a sequence's <name>.mid with the options of its JSON's "midi" field. Each MIDI
channel that plays becomes a track, in channel order, and the program numbers pick the instruments of the sequence's
bank, a bank select (controller 0) adding 128 for each step. The conversion keeps:

- notes, with their velocities and lengths, and the tempo, at the sequence's 48 ticks per quarter note;
- program changes, volume (7), pan (10), expression (11), modulation (1), portamento time (5) and switch (65),
  pitch bend and its range (RPN 0);
- the loop: from a marker "[" (or "loopStart", or controller 111) to a marker "]" (or "loopEnd"), or else the whole
  song, unless the options turn looping off. The tracks jump back to the start of the loop at its end, and set again
  there whatever changes inside it.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data.datajson import DataError, label  # noqa: E402
from tools.data.sseq import key_name  # noqa: E402

TICKS = 48  # A sequence's ticks per quarter note
LOOP_START = {"[", "loopstart", "loop_start", "loop start"}
LOOP_END = {"]", "loopend", "loop_end", "loop end"}
# The controllers kept, and the commands they become
CONTROLLERS = {1: "mod_depth", 5: "portamento_time", 7: "volume", 10: "pan", 11: "expression", 65: "portamento_on"}
# The state that a loop sets again at its start, in order
STATE = ["tempo", "program", "volume", "pan", "expression", "mod_depth", "portamento_time", "portamento_on",
         "bend_range", "pitch_bend"]


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
    loop = options.get("loop", True)
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
    rpn = {}
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
        elif kind == "program":
            program = banks.get(channel, 0) * 128 + event[4]
            commands.append((at, order, f"program {program}", "program", program))
        elif kind == "cc":
            number, value = event[4], event[5]
            if number == 0:
                banks[channel] = value
            elif number in (101, 100):
                rpn[channel, number] = value
            elif number == 6 and rpn.get((channel, 101)) == 0 and rpn.get((channel, 100)) == 0:
                commands.append((at, order, f"bend_range {value}", "bend_range", value))
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

    used = sorted(c for c, commands in channels.items() if any(isinstance(t[2], tuple) for t in commands))
    if not used:
        raise DataError(f"{where}: the MIDI file has no notes")
    if len(used) > 16:
        raise DataError(f"{where}: a sequence has at most 16 tracks")
    lines = [f"\talloc_tracks 0x{(1 << len(used)) - 1:04X}"]
    lines += [f"\topen_track {track}, Track_{track}" for track in range(1, len(used))]
    for track, channel in enumerate(used):
        commands = channels[channel] + (tempos if track == 0 else [])
        commands.sort(key=lambda command: (command[0], isinstance(command[2], tuple), command[1]))
        if track:
            lines.append(f"Track_{track}:")
        lines += ["\tnote_wait 0", f"\tpriority {options.get('priority', 64)}",
                  f"\tbend_range {options.get('bend_range', 2)}", "\tvolume 100", "\tpan 64", "\texpression 127"]
        state = {}
        now = 0
        looped = False

        def wait_until(tick):
            nonlocal now
            if tick > now:
                lines.append(f"\twait {tick - now}")
                now = tick

        def start_loop():
            nonlocal looped
            wait_until(loop_start)
            lines.append(f"Loop_{track}:")
            changed = {key for tick, _, _, key, _ in commands if key and loop_start <= tick < end}
            for key in STATE:
                if key in changed and key in state:
                    lines.append(f"\t{key} {state[key]}")
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
                if key:
                    state[key] = value
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
