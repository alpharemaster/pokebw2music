#!/usr/bin/env python3
"""The text form of Nitro sound sequences (SSEQ), which the sound archive's sequences are written in.

    sseq.py disassemble SEQUENCE.sseq OUT.txt
    sseq.py assemble IN.txt OUT.sseq

A sequence is a stream of commands that the sound driver runs on up to 16 tracks. Track 0 starts at the top, and
`open_track` starts the others at their labels. The text has one command per line, `Label:` lines, and `;` comments:

    alloc_tracks 0x0003      ; tracks 0 and 1
    open_track 1, Track_1
    tempo 120
    end
Track_1:
    program 5
    note C4, 100, 48         ; key, velocity, length in ticks (48 per quarter note)
    wait 48
    jump Track_1

Keys are numbers or names from Cm1 (0) to G9 (127), with s for sharp: C4 is 60, Cs4 61. A command can carry the
prefixes that the driver allows: `random` replaces its last argument with a minimum and maximum, `variable` replaces it
with the number of a sequence variable, and `if` runs it only when the last comparison was true. The assembler pads
the data to four bytes with zeros, as the original tool did.
"""
import re
import struct
import sys
from pathlib import Path

# The arguments of each command, by code: B unsigned byte, b signed byte, H unsigned halfword, h signed halfword,
# V variable-length number, T offset of a label in the sequence data (three bytes)
COMMANDS = {
    0x80: ("wait", "V"),
    0x81: ("program", "V"),
    0x93: ("open_track", "BT"),
    0x94: ("jump", "T"),
    0x95: ("call", "T"),
    0xB0: ("setvar", "Bh"),
    0xB1: ("addvar", "Bh"),
    0xB2: ("subvar", "Bh"),
    0xB3: ("mulvar", "Bh"),
    0xB4: ("divvar", "Bh"),
    0xB5: ("shiftvar", "Bh"),
    0xB6: ("randvar", "Bh"),
    0xB8: ("cmp_eq", "Bh"),
    0xB9: ("cmp_ge", "Bh"),
    0xBA: ("cmp_gt", "Bh"),
    0xBB: ("cmp_le", "Bh"),
    0xBC: ("cmp_lt", "Bh"),
    0xBD: ("cmp_ne", "Bh"),
    0xC0: ("pan", "B"),
    0xC1: ("volume", "B"),
    0xC2: ("main_volume", "B"),
    0xC3: ("transpose", "b"),
    0xC4: ("pitch_bend", "b"),
    0xC5: ("bend_range", "B"),
    0xC6: ("priority", "B"),
    0xC7: ("note_wait", "B"),
    0xC8: ("tie", "B"),
    0xC9: ("portamento", "B"),
    0xCA: ("mod_depth", "B"),
    0xCB: ("mod_speed", "B"),
    0xCC: ("mod_type", "B"),
    0xCD: ("mod_range", "B"),
    0xCE: ("portamento_on", "B"),
    0xCF: ("portamento_time", "B"),
    0xD0: ("attack", "B"),
    0xD1: ("decay", "B"),
    0xD2: ("sustain", "B"),
    0xD3: ("release", "B"),
    0xD4: ("loop_start", "B"),
    0xD5: ("expression", "B"),
    0xD6: ("print_var", "B"),
    0xE0: ("mod_delay", "H"),
    0xE1: ("tempo", "H"),
    0xE3: ("sweep_pitch", "h"),
    0xFC: ("loop_end", ""),
    0xFD: ("return", ""),
    0xFE: ("alloc_tracks", "H"),
    0xFF: ("end", ""),
}
NOTE = ("note", "BV")  # Codes 0x00-0x7f, whose code is the key: the velocity and the length
PREFIXES = {0xA0: "random", 0xA1: "variable", 0xA2: "if"}
CODES = {name: code for code, (name, _) in COMMANDS.items()}
PREFIX_CODES = {name: code for code, name in PREFIXES.items()}
KEY_NAMES = ["C", "Cs", "D", "Ds", "E", "F", "Fs", "G", "Gs", "A", "As", "B"]
KEY = re.compile(r"([A-G]s?)(m1|[0-9])$")
HEADER = struct.Struct("<4sHHIHH4sII")


def key_name(key: int) -> str:
    octave = key // 12 - 1
    return KEY_NAMES[key % 12] + ("m1" if octave < 0 else str(octave))


def parse_key(text: str) -> int:
    match = KEY.match(text)
    if not match:
        return int(text, 0)
    octave = -1 if match[2] == "m1" else int(match[2])
    return (octave + 1) * 12 + KEY_NAMES.index(match[1])


class Command:
    """One command: its prefixes, its code (a key for a note), and its arguments as numbers, a label's offset for T."""
    def __init__(self, offset, prefixes, code, args):
        self.offset = offset
        self.prefixes = prefixes
        self.code = code
        self.args = args

    def format(self):
        return NOTE[1] if self.code < 0x80 else COMMANDS[self.code][1]

    def ends_flow(self):
        """Whether the track never runs the next command after this one: the end, a return or an unconditional
        jump."""
        return self.code in (0xFF, 0xFD, 0x94) and "if" not in self.prefixes


def read_varlen(data, pos):
    value = 0
    while True:
        byte = data[pos]
        pos += 1
        value = value << 7 | byte & 0x7F
        if not byte & 0x80:
            return value, pos


def write_varlen(value):
    if not 0 <= value < 1 << 28:
        raise ValueError(f"{value} is out of range for a variable-length number")
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    return bytes(reversed(out))


def decode(data: bytes) -> list[Command]:
    """Decodes a sequence's data into its commands, in order."""
    commands = []
    pos = 0
    while pos < len(data):
        start = pos
        prefixes = []
        while data[pos] in PREFIXES:
            prefixes.append(PREFIXES[data[pos]])
            pos += 1
        code = data[pos]
        pos += 1
        if code >= 0x80 and code not in COMMANDS:
            raise ValueError(f"unknown sequence command {code:#x} at {start:#x}")
        command = Command(start, prefixes, code, [])
        if code < 0x80:
            command.args.append(code)
        fmt = command.format()
        for i, kind in enumerate(fmt):
            last = i == len(fmt) - 1
            if last and prefixes and prefixes[-1] == "random":
                command.args += struct.unpack_from("<hh", data, pos)
                pos += 4
            elif last and prefixes and prefixes[-1] == "variable":
                command.args.append(data[pos])
                pos += 1
            elif kind == "V":
                value, pos = read_varlen(data, pos)
                command.args.append(value)
            elif kind == "T":
                command.args.append(data[pos] | data[pos + 1] << 8 | data[pos + 2] << 16)
                pos += 3
            else:
                size = struct.calcsize("<" + kind)
                command.args.append(struct.unpack_from("<" + kind, data, pos)[0])
                pos += size
        commands.append(command)
    return commands


def strip_padding(data: bytes) -> bytes:
    """Returns the data without the zeros that pad it to four bytes after its last command that ends a track."""
    for size in range(min(3, len(data)), 0, -1):
        if any(data[-size:]):
            continue
        try:
            commands = decode(data[:-size])
        except (ValueError, IndexError, struct.error):
            continue
        if commands and commands[-1].ends_flow():
            return data[:-size]
    return data


def disassemble(sseq: bytes) -> str:
    """Returns the text of an SSEQ file."""
    magic, bom, version, size, header_size, blocks, block, block_size, data_offset = HEADER.unpack_from(sseq)
    if magic != b"SSEQ" or block != b"DATA" or size != len(sseq) or data_offset != 0x1C:
        raise ValueError("expected a Nitro sound sequence")
    data = strip_padding(sseq[data_offset:])
    if (len(data) + 3) & ~3 != len(sseq) - data_offset:
        raise ValueError("unexpected sequence padding")
    commands = decode(data)
    starts = {command.offset for command in commands}
    labels = {}
    # Tracks first, then subroutines, then the targets of jumps, each named once by its first use
    for code, prefix in ((0x93, "Track_"), (0x95, "Sub_"), (0x94, "Loop_")):
        for command in commands:
            if command.code == code:
                target = command.args[-1]
                if target not in starts:
                    raise ValueError(f"{prefix} target {target:#x} is not a command")
                if target not in labels:
                    labels[target] = f"Track_{command.args[0]}" if code == 0x93 else f"{prefix}{target:04X}"
    used = set()
    for offset, label in sorted(labels.items()):
        while label in used:
            label += "_"
        labels[offset] = label
        used.add(label)
    lines = []
    for command in commands:
        if command.offset in labels:
            lines.append(f"{labels[command.offset]}:")
        name = NOTE[0] if command.code < 0x80 else COMMANDS[command.code][0]
        args = list(command.args)
        fmt = ("B" if command.code < 0x80 else "") + command.format()
        texts = []
        for i, value in enumerate(args):
            kind = fmt[i] if i < len(fmt) else None
            if command.code < 0x80 and i == 0:
                texts.append(key_name(value))
            elif kind == "T" and not command.prefixes:
                texts.append(labels[value])
            elif kind == "H" and command.code == 0xFE:
                texts.append(f"0x{value:04X}")
            else:
                texts.append(str(value))
        lines.append("\t" + " ".join([*command.prefixes, name]) + (" " + ", ".join(texts) if texts else ""))
    return "\n".join(lines) + "\n"


def assemble(text: str, path="<sequence>") -> bytes:
    """Returns the SSEQ file of a sequence's text."""
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append((number, line))
    # The first pass sizes each command to place the labels; T arguments are always three bytes
    labels = {}
    items = []
    for number, line in lines:
        if line.endswith(":"):
            name = line[:-1].strip()
            if not name.isidentifier():
                raise ValueError(f"{path}:{number}: {name!r} is not a valid label")
            if name in labels:
                raise ValueError(f"{path}:{number}: label {name} is defined twice")
            labels[name] = None
            items.append((number, name, None, None))
            continue
        words, _, rest = line.partition(" ")
        head = [words]
        rest = rest.strip()
        while head[-1] in PREFIX_CODES:
            word, _, rest = rest.partition(" ")
            head.append(word)
            rest = rest.strip()
        args = [arg.strip() for arg in rest.split(",")] if rest else []
        items.append((number, None, head, args))

    def encode(number, head, args, resolve):
        *prefixes, name = head
        out = bytearray(PREFIX_CODES[p] for p in prefixes)
        if name == "note":
            fmt = "B" + NOTE[1]
        elif name in CODES:
            fmt = COMMANDS[CODES[name]][1]
        else:
            raise ValueError(f"{path}:{number}: unknown command {name!r}")
        expected = len(fmt) + (1 if prefixes and prefixes[-1] == "random" else 0)
        if len(args) != expected:
            raise ValueError(f"{path}:{number}: {name} takes {expected} arguments, not {len(args)}")
        values = []
        for i, arg in enumerate(args):
            kind = fmt[min(i, len(fmt) - 1)]
            try:
                if name == "note" and i == 0:
                    values.append(parse_key(arg))
                elif kind == "T" and arg.isidentifier():
                    if arg not in labels:
                        raise ValueError(f"{path}:{number}: unknown label {arg}")
                    values.append(resolve(arg))
                else:
                    values.append(int(arg, 0))
            except ValueError as error:
                if str(error).startswith(path):
                    raise
                raise ValueError(f"{path}:{number}: {arg!r} is not a number") from None
        if name == "note":
            if not 0 <= values[0] < 0x80:
                raise ValueError(f"{path}:{number}: key {values[0]} is out of range")
            out.append(values[0])
            values = values[1:]
            fmt = fmt[1:]
        else:
            out.append(CODES[name])
        for i, kind in enumerate(fmt):
            last = i == len(fmt) - 1
            value = values[i]
            try:
                if last and prefixes and prefixes[-1] == "random":
                    out += struct.pack("<hh", value, values[i + 1])
                elif last and prefixes and prefixes[-1] == "variable":
                    out += struct.pack("<B", value)
                elif kind == "V":
                    out += write_varlen(value)
                elif kind == "T":
                    if not 0 <= value < 1 << 24:
                        raise ValueError
                    out += bytes((value & 0xFF, value >> 8 & 0xFF, value >> 16))
                else:
                    out += struct.pack("<" + kind, value)
            except (struct.error, ValueError):
                raise ValueError(f"{path}:{number}: argument {value} of {name} is out of range") from None
        return bytes(out)

    for _ in range(2):
        offset = 0
        data = bytearray()
        for number, label, head, args in items:
            if label is not None:
                labels[label] = offset
                continue
            chunk = encode(number, head, args, lambda name: labels[name] or 0)
            data += chunk
            offset += len(chunk)
    data += b"\0" * (-len(data) % 4)
    size = 0x1C + len(data)
    return HEADER.pack(b"SSEQ", 0xFEFF, 0x0100, size, 0x10, 1, b"DATA", size - 0x10, 0x1C) + bytes(data)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "disassemble":
        Path(sys.argv[3]).write_text(disassemble(Path(sys.argv[2]).read_bytes()))
    elif len(sys.argv) == 4 and sys.argv[1] == "assemble":
        Path(sys.argv[3]).write_bytes(assemble(Path(sys.argv[2]).read_text(), sys.argv[2]))
    else:
        raise SystemExit(__doc__)
