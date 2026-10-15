#!/usr/bin/env python3
"""Export a Nitro sound sequence (the text of tools/data/sseq.py) as a MIDI file, which midi_sseq.py converts back.

    sseq_midi.py IN.seq [OUT.mid]

The export plays the sequence as the sound driver does: it follows calls, starts each track where it is opened, and
renders each track up to the song's loop, which it marks with the markers "[" and "]". A track that loops on its own
part repeats it to fill the song's loop. Track N becomes channel N, at the sequence's 48 ticks per quarter note, and
the commands without a MIDI event of their own become the controllers of midi_sseq.CONTROLLERS, so that converting
the file back plays the same.

The file starts with a text event that gives the hashes of the sequence it was exported from and of the MIDI itself.
The packer (sound_data.py) uses the .seq file of a sequence while its .mid is that export, and the .mid once it was
edited or replaced (midi_state()).
"""
import hashlib
import math
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data import sseq  # noqa: E402
from tools.data.datajson import DataError  # noqa: E402
from tools.data.midi_sseq import COMMAND_CONTROLLERS, PROGRAM_CC, SWEEP_PITCH, TICKS  # noqa: E402

# The sound driver's settings of a new track, which the export writes where the track doesn't set its own
TRACK_DEFAULTS = {0xC1: 127, 0xC0: 64, 0xD5: 127, 0xC5: 2, 0xC6: 64}
STAMP = re.compile(rb"pokebw2 sseq=([0-9a-f]{40}) midi=([0-9a-f]{40})")
NO_HASH = b"0" * 40
# The longest song loop the export unrolls the tracks' own loops to, in ticks
MAX_LOOP = TICKS * 4 * 512


class Track:
    def __init__(self, number, start, tick):
        self.number = number
        self.pc = start
        self.start = tick        # The tick it was opened at
        self.tick = tick
        self.events = []         # (tick, order, kind, values...)
        self.loop = None         # The track's own loop: (start tick, end tick)
        self.ended = None        # The tick of its end, if it ends


def render(commands: list[sseq.Command]) -> tuple[list[Track], list]:
    """Plays a sequence's commands: returns its tracks with their events, and the tempo events."""
    index = {command.offset: i for i, command in enumerate(commands)}
    tracks = [Track(0, 0, 0)]
    tempos = []
    order = 0
    pending = list(tracks)
    while pending:
        track = pending.pop(0)
        i = index[track.pc]
        stack = []
        first = {}  # The tick each command outside a call first ran at
        note_wait = True
        transpose = 0
        steps = 0
        while True:
            steps += 1
            if steps > 1_000_000:
                raise DataError(f"track {track.number} runs too long without a loop")
            command = commands[i]
            if command.prefixes:
                raise DataError(f"track {track.number}: the {' '.join(command.prefixes)} prefix has no MIDI form")
            code = command.code
            if not stack:
                first.setdefault(command.offset, track.tick)
            order += 1
            if code < 0x80:
                key, velocity, length = command.args
                track.events.append((track.tick, order, "note", key + transpose, velocity, length))
                if note_wait:
                    track.tick += length
            elif code == 0x80:
                track.tick += command.args[0]
            elif code == 0x81:
                track.events.append((track.tick, order, "program", command.args[0]))
            elif code == 0x93:
                number, target = command.args
                new = Track(number, target, track.tick)
                tracks.append(new)
                pending.append(new)
            elif code == 0x94:
                target = command.args[0]
                if target in first:
                    track.loop = (first[target], track.tick)
                    break
                i = index[target]
                continue
            elif code == 0x95:
                stack.append(i + 1)
                i = index[command.args[0]]
                continue
            elif code == 0xFD:
                if not stack:
                    track.ended = track.tick
                    break
                i = stack.pop()
                continue
            elif code == 0xFF:
                track.ended = track.tick
                break
            elif code == 0xE1:
                tempos.append((track.tick, order, command.args[0]))
            elif code == 0xC7:
                note_wait = bool(command.args[0])
            elif code == 0xC3:
                transpose = command.args[0]
            elif code == 0xC4:
                track.events.append((track.tick, order, "bend", command.args[0]))
            elif code == 0xE3:
                track.events.append((track.tick, order, "sweep", command.args[0]))
            elif code in (0xFE, 0xC8, 0xC2, 0xD6):
                pass  # The track allocation, tie, main volume and debug print change nothing that MIDI plays
            elif code in COMMAND_CONTROLLERS:
                track.events.append((track.tick, order, "control", code, command.args[0]))
            else:
                raise DataError(f"track {track.number}: {sseq.COMMANDS[code][0]} has no MIDI form")
            i += 1
    numbers = [track.number for track in tracks]
    if len(set(numbers)) != len(numbers):
        raise DataError("a track is opened twice")
    return tracks, tempos


def song_loop(tracks: list[Track]) -> tuple[int, int] | None:
    """Returns the song's loop, from the latest start of a track's loop over a length that every track's loop
    divides, or None if no track loops."""
    loops = [track.loop for track in tracks if track.loop]
    if not loops:
        return None
    start = max(s for s, _ in loops)
    length = 1
    for s, e in loops:
        length = math.lcm(length, max(1, e - s))
    if length > MAX_LOOP:
        raise DataError(f"the tracks' loops only meet after {length} ticks")
    return start, start + length


def unrolled(track: Track, end: int) -> list:
    """Returns a track's events up to a tick, repeating its own loop to reach it."""
    events = [e for e in track.events if e[0] < end]
    if track.loop:
        start, loop_end = track.loop
        body = [e for e in track.events if start <= e[0] < loop_end]
        events = [e for e in events if e[0] < loop_end]
        shift = loop_end - start
        while body and body[0][0] + shift < end:
            body = [(e[0] + shift, *e[1:]) for e in body]
            events += [e for e in body if e[0] < end]
            shift = loop_end - start
    return events


def varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    return bytes(reversed(out))


def midi_track(events: list[tuple[int, bytes]]) -> bytes:
    data = bytearray()
    now = 0
    for tick, body in sorted(events, key=lambda e: e[0]):
        data += varlen(tick - now) + body
        now = tick
    data += b"\0\xff\x2f\0"
    return b"MTrk" + struct.pack(">I", len(data)) + data


def export(text: str, path="<sequence>") -> bytes:
    """Returns the MIDI file of a sequence's text."""
    try:
        data = sseq.assemble(text, path)
        tracks, tempos = render(sseq.decode(sseq.strip_padding(data[0x1C:])))
        loop = song_loop(tracks)
    except DataError as error:
        raise DataError(f"{path}: {error}") from None
    except ValueError as error:
        raise DataError(str(error)) from None
    ends = [t.ended for t in tracks if t.ended is not None] + [e[0] + (e[5] if e[2] == "note" else 0)
                                                                 for t in tracks for e in t.events]
    end = loop[1] if loop else max(ends, default=0)

    # An empty text event ends each track at the song's end
    finish = (end, b"\xff\x01\0")
    conductor = [(0, b"\xff\x03\x09Conductor")]
    for tick, _, bpm in sorted(tempos):
        if tick < end or (not loop and tick <= end):
            conductor.append((tick, b"\xff\x51\x03" + (60_000_000 // max(1, bpm)).to_bytes(3, "big")))
    if loop:
        conductor += [(loop[0], b"\xff\x06\x01["), (loop[1], b"\xff\x06\x01]")]
    chunks = []
    for track in sorted(tracks, key=lambda t: t.number):
        channel = track.number
        name = f"Track {channel}".encode()
        events = [(0, b"\xff\x03" + varlen(len(name)) + name)]
        bank = 0
        played = sorted(unrolled(track, end) if loop else track.events, key=lambda e: (e[0], e[1]))
        start = track.start
        own = {e[3] for e in played if e[2] == "control" and e[0] == start}
        defaults = [(start, -1, "control", code, value) for code, value in TRACK_DEFAULTS.items() if code not in own]
        played = defaults + played
        for event in played:
            tick, _, kind = event[:3]
            if kind == "note":
                key, velocity, length = event[3:]
                if not 0 <= key < 0x80:
                    raise DataError(f"{path}: track {channel} plays key {key} after transposing")
                off = min(tick + max(1, length), end) if loop else tick + max(1, length)
                events.append((tick, bytes((0x90 | channel, key, max(1, velocity)))))
                events.append((off, bytes((0x80 | channel, key, 0))))
            elif kind == "program":
                program = event[3]
                if program // 128 != bank:
                    bank = program // 128
                    events.append((tick, bytes((0xB0 | channel, 0, bank))))
                events.append((tick, bytes((0xB0 | channel, PROGRAM_CC, program % 128))))
                events.append((tick, bytes((0xC0 | channel, program % 128))))
            elif kind == "bend":
                wheel = event[3] * 64 + 8192
                events.append((tick, bytes((0xE0 | channel, wheel & 0x7F, wheel >> 7))))
            elif kind == "sweep":
                value = max(0, min(0x3FFF, event[3] + 8192))
                events.append((tick, bytes((0xB0 | channel, SWEEP_PITCH[0], value >> 7))))
                events.append((tick, bytes((0xB0 | channel, SWEEP_PITCH[1], value & 0x7F))))
            else:
                code, value = event[3:]
                number = COMMAND_CONTROLLERS[code]
                if code == 0xC5:  # The bend range, as RPN 0
                    events += [(tick, bytes((0xB0 | channel, 101, 0))), (tick, bytes((0xB0 | channel, 100, 0))),
                               (tick, bytes((0xB0 | channel, 6, min(127, value))))]
                    continue
                if code == 0xCE:
                    value = 127 if value else 0
                events.append((tick, bytes((0xB0 | channel, number, min(127, value)))))
        # Within a tick: the settings, then the notes that end, then the notes that start
        events.sort(key=lambda e: (e[0], {0x80: 1, 0x90: 2}.get(e[1][0] & 0xF0, 0)))
        chunks.append(midi_track(events + [finish]))
    stamp = b"pokebw2 sseq=" + hashlib.sha1(text.encode()).hexdigest().encode() + b" midi=" + NO_HASH
    conductor = [(0, b"\xff\x01" + varlen(len(stamp)) + stamp)] + conductor + [finish]
    smf = (b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks) + 1, TICKS) + midi_track(conductor)
           + b"".join(chunks))
    return smf.replace(NO_HASH, hashlib.sha1(smf).hexdigest().encode(), 1)


def midi_state(midi: bytes, text: str) -> str:
    """Returns how a sequence's MIDI file stands to its text: "export" if it is the text's export, "edited" if it was
    edited or replaced since, "stale" if the text changed since it was exported, or "conflict" if both did."""
    match = STAMP.search(midi)
    if not match:
        return "edited"
    unchanged = hashlib.sha1(midi.replace(match[2], NO_HASH, 1)).hexdigest().encode() == match[2]
    same_text = hashlib.sha1(text.encode()).hexdigest().encode() == match[1]
    if unchanged:
        return "export" if same_text else "stale"
    return "edited" if same_text else "conflict"


if __name__ == "__main__":
    if len(sys.argv) in (2, 3):
        source = Path(sys.argv[1])
        try:
            midi = export(source.read_text(), sys.argv[1])
        except DataError as error:
            sys.exit(f"error: {error}")
        Path(sys.argv[2] if len(sys.argv) == 3 else source.with_suffix(".mid")).write_bytes(midi)
    else:
        raise SystemExit(__doc__)
