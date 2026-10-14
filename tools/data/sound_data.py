#!/usr/bin/env python3
"""Extract and pack the sound archive, files/swan_sound_data.sdat, from data/sound/.

    sound_data.py extract data/sound
    sound_data.py pack data/sound SDAT BGM_INFO

The archive is a Nitro sound archive (SDAT): its sequences, the instrument banks they play, the wave archives with
the banks' samples, its players, groups and stream. The sources:

- data/constants/sound.txt numbers the sequences, sound_banks.txt the banks and sound_wave_archives.txt the wave
  archives, so adding one is adding its constant and its files.
- data/sound/sequences/<name>.json gives a sequence's bank, volume, priorities and player, and <name>.seq is its
  text (tools/data/sseq.py), or <name>.mid a MIDI file converted to it (tools/data/midi_sseq.py).
- data/sound/banks/<name>.json gives a bank's wave archives and its instruments, in the order of their data.
- data/sound/wave_archives/<name>/ holds a wave archive's samples, 000 and up: .swav files, the original samples
  as they are, or .wav files, which the packer encodes (see wave_archive.schema.json for the options).
- data/sound/archive.json gives the players, stream players, streams and groups; data/sound/streams/<name>.strm
  is a stream's file.
- data/sound/interactive_music.json lists the BGMs that the field's interactive sound system changes, and how, which
  is packed into the BGM info archive (a/0/8/6, bgm_info.c) beside the sound archive.
- The Pokémon cries' wave archives, WAVE_ARC_PV001 to the forms', are packed from data/pokemon/'s cry.wav files
  (tools/data/pokemon_cries.py).

A sequence, bank or wave archive with "shares_data_with" uses another one's file, as the original tool stored an
entry that named the same source file once.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import tools.data.sseq as sseq  # noqa: E402
from tools.data.midi_sseq import convert  # noqa: E402
from tools.data.sound_waves import wav_wave  # noqa: E402
from tools.data.datajson import DataError, label, load, load_schema  # noqa: E402
from tools.data.gen_constants import LISTS_DIR, parse  # noqa: E402
from tools.data.narc import read_narc, write_narc  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))  # pokemon_cries.py imports its neighbors by their names
from tools.data.pokemon_cries import cry_folder, read_wav  # noqa: E402

# The archive's kinds of entry, in the order of its symbol and info tables
KINDS = ["SEQ", "SEQARC", "BANK", "WAVEARC", "PLAYER", "GROUP", "STRM_PLAYER", "STRM"]
LISTS = {"SEQ": ("sound", "SEQ_"), "BANK": ("sound_banks", "BANK_"), "WAVEARC": ("sound_wave_archives", "WAVE_")}
SEQ_INFO = struct.Struct("<IHBBBBH")      # File, bank, volume, channel priority, player priority, player
BANK_INFO = struct.Struct("<I4H")         # File, wave archives (0xffff for none)
WAVEARC_INFO = struct.Struct("<I")        # File in the low 24 bits, flags in the high 8
PLAYER_INFO = struct.Struct("<BBHI")      # Most sequences, padding, channels, heap size
GROUP_ITEM = struct.Struct("<BBHI")       # Kind, load flags, padding, number
STRM_PLAYER_INFO = struct.Struct("<B16B7x")  # Channel count, channels (0xff past the count)
STRM_INFO = struct.Struct("<IBBB5x")      # File, volume, player priority, stream player
NO_WAVE_ARCHIVE = 0xFFFF
# The kinds of a group's items, by the prefix of their names, and the parts of an item that the group loads
GROUP_KINDS = {"SEQ_": 0, "BANK_": 1, "WAVE_": 2}
LOAD_FLAGS = ["sequence", "bank", "wave_archive", "single_waves"]
WAVEARC_SINGLE_LOAD = 1 << 24
# Instrument types: the simple ones, which play one sample or PSG sound, and the drum set and key split, which pick one
# by key
SIMPLE = {1: "pcm", 2: "psg", 3: "noise", 4: "direct_pcm"}
SIMPLE_TYPES = {name: code for code, name in SIMPLE.items()}
DRUM_SET, KEY_SPLIT = 16, 17
# Wave (PSG duty), wave archive slot, key, attack, decay, sustain, release, pan
NOTE_PARAMS = struct.Struct("<HHBBBBBB")
# The wave archives of the Pokémon cries, one per cry ID (pokemon_cries.py), are packed from data/pokemon/
CRY_WAVE_ARCHIVES = range(1, 691)
# A wave's header, and the clock that its timer, from its sample rate, divides
WAVE = struct.Struct("<BBHHHI")           # Encoding, loops, rate, timer, loop start and length after it, in words
ARM7_CLOCK = 16756991
# The interactive sound subsystems a BGM can use (ISS_SUBSYSTEM_*, iss_sys.h); 1 and 6 do nothing
ISS_SUBSYSTEMS = {2: "road", 3: "city", 4: "3d_sound", 5: "dungeon", 7: "switch", 8: "zone"}


def nitro_header(magic: bytes, size: int, blocks: int) -> bytes:
    return struct.pack("<4sHHIHH", magic, 0xFEFF, 0x0100, size, 0x10, blocks)


def align(value: int, to: int) -> int:
    return (value + to - 1) & -to


def file_name(name: str, prefix: str) -> str:
    return name.removeprefix(prefix).lower()


def load_list(kind: str) -> dict[int, str]:
    """Returns the entries of a kind by number, from its list: the first name of each number."""
    list_name, prefix = LISTS[kind]
    entries: dict[int, str] = {}
    for name, number in parse(LISTS_DIR / f"{list_name}.txt").items():
        if not name.startswith(prefix):
            raise DataError(f"data/constants/{list_name}.txt: {name} should start with {prefix}")
        entries.setdefault(number, name)
    return entries


def write_json(path: Path, data, width: int = 120):
    """Writes JSON as datajson.write does, except that an object of numbers and strings that fits stays on one line,
    as an instrument does."""
    def dumps(item, indent):
        if isinstance(item, dict) and item and all(not isinstance(v, (dict, list)) for v in item.values()):
            fields = (f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in item.items())
            line = "{ " + ", ".join(fields) + " }"
            if indent + len(line) <= width:
                return line
        if isinstance(item, list) and item and all(not isinstance(v, (dict, list)) for v in item):
            line = "[ " + ", ".join(json.dumps(v, ensure_ascii=False) for v in item) + " ]"
            if indent + len(line) <= width:
                return line
        if not isinstance(item, (dict, list)) or not item:
            return json.dumps(item, ensure_ascii=False)
        inner = " " * (indent + 4)
        if isinstance(item, dict):
            lines = [f"{inner}{json.dumps(k)}: {dumps(v, indent + 4)}" for k, v in item.items()]
            return "{\n" + ",\n".join(lines) + "\n" + " " * indent + "}"
        return "[\n" + ",\n".join(inner + dumps(v, indent + 4) for v in item) + "\n" + " " * indent + "]"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(data, 0) + "\n")


# The archive


def read_sdat(data: bytes) -> dict:
    """Returns an archive's entries: for each kind, its names and info records by number (None where there is none),
    and its files."""
    if data[:4] != b"SDAT":
        raise ValueError("expected a Nitro sound archive")
    symb, _, info, _, fat, _, _, _ = struct.unpack_from("<8I", data, 0x10)
    entries = {}
    for i, kind in enumerate(KINDS):
        symbols = symb + struct.unpack_from("<I", data, symb + 8 + 4 * i)[0]
        table = info + struct.unpack_from("<I", data, info + 8 + 4 * i)[0]
        count = struct.unpack_from("<I", data, table)[0]
        names, records = [], []
        for number in range(count):
            record = struct.unpack_from("<I", data, table + 4 + 4 * number)[0]
            name = None
            if kind != "SEQARC":
                offset = struct.unpack_from("<I", data, symbols + 4 + 4 * number)[0]
                if offset:
                    name = data[symb + offset:data.index(b"\0", symb + offset)].decode()
            names.append(name)
            records.append(info + record if record else None)
        entries[kind] = (names, records)
    files = []
    for i in range(struct.unpack_from("<I", data, fat + 8)[0]):
        offset, size = struct.unpack_from("<II", data, fat + 12 + 16 * i)
        files.append(data[offset:offset + size])
    return {"entries": entries, "files": files}


def write_sdat(names: dict[str, list], records: dict[str, list], files: list[bytes]) -> bytes:
    """Returns the archive of the entries' names and info records, by kind and number, and its files, as the original
    tool laid them out: each block's tables, then its records or strings in order, and the files at 32 bytes."""
    symb = bytearray(0x40)
    strings = bytearray()
    tables = []
    for kind in KINDS:
        tables.append(len(symb))
        if kind == "SEQARC":
            symb += struct.pack("<I", 0)
            continue
        symb += struct.pack("<I", len(names[kind]))
        symb += bytes(4 * len(names[kind]))
    for i, kind in enumerate(KINDS):
        if kind == "SEQARC":
            continue
        for number, name in enumerate(names[kind]):
            if name is not None:
                struct.pack_into("<I", symb, tables[i] + 4 + 4 * number, len(symb) + len(strings))
                strings += name.encode() + b"\0"
    symb += strings
    # The block's own size counts its padding to four bytes, and the archive's header doesn't
    struct.pack_into("<4sI8I", symb, 0, b"SYMB", align(len(symb), 4), *tables)

    info = bytearray(0x40)
    tables = []
    for kind in KINDS:
        tables.append(len(info))
        table = len(info)
        info += struct.pack("<I", len(records[kind])) + bytes(4 * len(records[kind]))
        for number, record in enumerate(records[kind]):
            if record is not None:
                struct.pack_into("<I", info, table + 4 + 4 * number, len(info))
                info += record
    info += bytes(-len(info) % 4)
    struct.pack_into("<4sI8I", info, 0, b"INFO", len(info), *tables)

    symb_offset = 0x40
    info_offset = align(symb_offset + len(symb), 4)
    fat_offset = info_offset + len(info)
    fat_size = 12 + 16 * len(files)
    file_offset = fat_offset + fat_size
    fat = bytearray(struct.pack("<4sII", b"FAT ", fat_size, len(files)))
    body = bytearray()
    cursor = file_offset + 16
    for member in files:
        padding = -cursor % 32
        body += bytes(padding)
        cursor += padding
        fat += struct.pack("<II8x", cursor, len(member))
        body += member
        cursor += len(member)
    body += bytes(-cursor % 32)
    file_size = 16 + len(body)
    total = file_offset + file_size
    header = nitro_header(b"SDAT", total, 4)[:0xC] + struct.pack("<HH8I16x", 0x40, 4, symb_offset, len(symb),
                                                                 info_offset, len(info), fat_offset, fat_size,
                                                                 file_offset, file_size)
    return b"".join([header, symb, bytes(info_offset - symb_offset - len(symb)), info, fat,
                     struct.pack("<4sII4x", b"FILE", file_size, len(files)), body])


# Banks


def note_json(params: bytes, kind: int) -> dict:
    wave, archive, key, attack, decay, sustain, release, pan = NOTE_PARAMS.unpack(params)
    if kind not in SIMPLE:
        raise ValueError(f"unknown instrument type {kind}")
    return {"type": SIMPLE[kind], "wave": wave, "wave_archive": archive, "key": sseq.key_name(key), "attack": attack,
            "decay": decay, "sustain": sustain, "release": release, "pan": pan}


NOTE_FIELDS = ["type", "wave", "wave_archive", "key", "attack", "decay", "sustain", "release", "pan"]


def note_bytes(note: dict, where: str) -> tuple[int, bytes]:
    missing = [field for field in NOTE_FIELDS if field not in note]
    if missing:
        raise DataError(f"{where}: {', '.join(missing)} missing")
    if note["type"] not in SIMPLE_TYPES:
        raise DataError(f"{where}: a note's type is one of {', '.join(SIMPLE_TYPES)}")
    key = note["key"]
    try:
        key = sseq.parse_key(key) if isinstance(key, str) else key
    except ValueError:
        raise DataError(f"{where}: {key!r} is not a key") from None
    if not 0 <= key < 0x80:
        raise DataError(f"{where}: key {key} is out of range")
    return SIMPLE_TYPES[note["type"]], NOTE_PARAMS.pack(note["wave"], note["wave_archive"], key, note["attack"],
                                                         note["decay"], note["sustain"], note["release"], note["pan"])


def instrument_size(data: bytes, kind: int, offset: int) -> int:
    if kind in SIMPLE:
        return NOTE_PARAMS.size
    if kind == DRUM_SET:
        return 2 + (data[offset + 1] - data[offset] + 1) * 12
    if kind == KEY_SPLIT:
        regions = data[offset:offset + 8]
        return 8 + (regions.index(0) if 0 in regions else 8) * 12
    raise ValueError(f"unknown instrument type {kind}")


def instrument_json(data: bytes, kind: int, offset: int) -> dict:
    if kind in SIMPLE:
        return note_json(data[offset:offset + 10], kind)
    if kind == DRUM_SET:
        low, high = data[offset], data[offset + 1]
        drums = [note_json(data[offset + 4 + 12 * i:offset + 14 + 12 * i], data[offset + 2 + 12 * i])
                 for i in range(high - low + 1)]
        if any(data[offset + 3 + 12 * i] for i in range(high - low + 1)):
            raise ValueError("drum type above a byte")
        return {"type": "drum_set", "first_key": sseq.key_name(low), "drums": drums}
    regions = []
    for i in range(8):
        last = data[offset + i]
        if not last:
            break
        entry = offset + 8 + 12 * i
        if data[entry + 1]:
            raise ValueError("key split type above a byte")
        regions.append({"last_key": sseq.key_name(last), **note_json(data[entry + 2:entry + 12], data[entry])})
    if any(data[offset + len(regions):offset + 8]):
        raise ValueError("key split regions after an empty one")
    return {"type": "key_split", "regions": regions}


def bank_json(sbnk: bytes) -> list[dict]:
    """Returns a bank's instruments in the order of their data, each with its program numbers."""
    if sbnk[:4] != b"SBNK" or sbnk[0x10:0x14] != b"DATA" or any(sbnk[0x18:0x38]):
        raise ValueError("expected a Nitro sound bank")
    count = struct.unpack_from("<I", sbnk, 0x38)[0]
    programs: dict[int, list] = {}
    kinds = {}
    for program in range(count):
        kind, offset, reserved = struct.unpack_from("<BHB", sbnk, 0x3C + 4 * program)
        if reserved:
            raise ValueError("instrument record with a reserved byte")
        if kind:
            if offset in kinds and kinds[offset] != kind:
                raise ValueError("instruments of two types share data")
            kinds[offset] = kind
            programs.setdefault(offset, []).append(program)
        elif offset:
            raise ValueError("empty program with data")
    cursor = 0x3C + 4 * count
    instruments = []

    def unused(until):
        """Reads the data that no program uses up to an offset: key splits, as far as the archive has them."""
        nonlocal cursor
        while until - cursor >= 4:
            size = instrument_size(sbnk, KEY_SPLIT, cursor)
            if cursor + size > until or not sbnk[cursor]:
                raise ValueError(f"unknown instrument data at {cursor:#x}")
            instruments.append({"program": None, **instrument_json(sbnk, KEY_SPLIT, cursor)})
            cursor += size

    for offset in sorted(programs):
        unused(offset)
        if offset != cursor:
            raise ValueError(f"gap before instrument data at {offset:#x}")
        listed = programs[offset]
        instruments.append({"program": listed[0] if len(listed) == 1 else listed,
                            **instrument_json(sbnk, kinds[offset], offset)})
        cursor = offset + instrument_size(sbnk, kinds[offset], offset)
    end = len(sbnk)
    unused(end)
    if any(sbnk[cursor:]) or end != align(cursor, 4):
        raise ValueError("unexpected bank padding")
    if (count and not any(struct.unpack_from("<B", sbnk, 0x3C + 4 * (count - 1)))):
        raise ValueError("bank ends with an empty program")
    return instruments


def bank_bytes(instruments: list[dict], where: str) -> bytes:
    programs = {}
    data = bytearray()
    entries = []
    for i, instrument in enumerate(instruments):
        at = f"{where}: instruments[{i}]"
        listed = instrument["program"]
        listed = [] if listed is None else listed if isinstance(listed, list) else [listed]
        kind = instrument["type"]
        if kind in SIMPLE_TYPES:
            code, body = note_bytes(instrument, at)
        elif kind == "drum_set":
            code = DRUM_SET
            low = sseq.parse_key(instrument["first_key"]) if isinstance(instrument["first_key"], str) else \
                instrument["first_key"]
            drums = instrument["drums"]
            if not drums or not 0 <= low or low + len(drums) > 0x80:
                raise DataError(f"{at}: drums must cover keys from 0 to 127")
            body = bytearray((low, low + len(drums) - 1))
            for j, drum in enumerate(drums):
                drum_code, params = note_bytes(drum, f"{at}.drums[{j}]")
                body += struct.pack("<H", drum_code) + params
        elif kind == "key_split":
            code = KEY_SPLIT
            regions = instrument["regions"]
            keys = [sseq.parse_key(r["last_key"]) if isinstance(r["last_key"], str) else r["last_key"]
                    for r in regions]
            if not 1 <= len(regions) <= 8 or not all(0 < key < 0x80 for key in keys):
                raise DataError(f"{at}: a key split takes 1 to 8 regions, each with a last_key from 1 to 127")
            body = bytearray(bytes(keys) + bytes(8 - len(keys)))
            for j, region in enumerate(regions):
                region_code, params = note_bytes(region, f"{at}.regions[{j}]")
                body += struct.pack("<H", region_code) + params
        entries.append((listed, code, len(data)))
        for program in listed:
            if program in programs:
                raise DataError(f"{at}: program {program} is defined twice")
            programs[program] = (code, len(data))
        data += body
    count = max(programs) + 1 if programs else 0
    start = 0x3C + 4 * count
    table = bytearray()
    for program in range(count):
        code, offset = programs.get(program, (0, None))
        table += struct.pack("<BHB", code, 0 if offset is None else start + offset, 0)
    data += bytes(-(start + len(data)) % 4)
    size = start + len(data)
    if size > 0xFFFF and any(start + offset > 0xFFFF for _, _, offset in entries):
        raise DataError(f"{where}: the bank's instruments exceed 64 KB")
    return nitro_header(b"SBNK", size, 1) + struct.pack("<4sI32xI", b"DATA", size - 0x10, count) + table + data


# Wave archives


def swar_waves(swar: bytes) -> list[bytes]:
    """Returns the waves of a wave archive, each its header and data."""
    if swar[:4] != b"SWAR" or swar[0x10:0x14] != b"DATA" or any(swar[0x18:0x38]):
        raise ValueError("expected a Nitro wave archive")
    count = struct.unpack_from("<I", swar, 0x38)[0]
    offsets = [*struct.unpack_from(f"<{count}I", swar, 0x3C), len(swar)]
    if count and offsets[0] != 0x3C + 4 * count:
        raise ValueError("gap before the waves")
    waves = [swar[offsets[i]:offsets[i + 1]] for i in range(count)]
    for wave in waves:
        _, _, _, _, loop_start, loop_length = WAVE.unpack_from(wave)
        if len(wave) != WAVE.size + 4 * (loop_start + loop_length):
            raise ValueError("wave sizes do not match their headers")
    return waves


def swar_bytes(waves: list[bytes]) -> bytes:
    start = 0x3C + 4 * len(waves)
    offsets = []
    cursor = start
    for wave in waves:
        offsets.append(cursor)
        cursor += len(wave)
    return (nitro_header(b"SWAR", cursor, 1) + struct.pack("<4sI32xI", b"DATA", cursor - 0x10, len(waves))
            + struct.pack(f"<{len(waves)}I", *offsets) + b"".join(waves))


def swav_bytes(wave: bytes) -> bytes:
    return nitro_header(b"SWAV", 0x18 + len(wave), 1) + struct.pack("<4sI", b"DATA", 8 + len(wave)) + wave


def swav_wave(swav: bytes, where: str) -> bytes:
    if swav[:4] != b"SWAV" or swav[0x10:0x14] != b"DATA" or struct.unpack_from("<I", swav, 8)[0] != len(swav):
        raise DataError(f"{where}: not a Nitro wave file")
    wave = swav[0x18:]
    _, _, _, _, loop_start, loop_length = WAVE.unpack_from(wave)
    if len(wave) != WAVE.size + 4 * (loop_start + loop_length):
        raise DataError(f"{where}: the wave's size does not match its header")
    return wave


def cry_swar(path: Path) -> bytes:
    """Returns the wave archive of a cry: its one wave, unlooped PCM8."""
    try:
        rate, samples = read_wav(path)
    except (ValueError, OSError, EOFError) as error:
        raise DataError(f"{label(path)}: {error}") from None
    data = samples + bytes(-len(samples) % 4)
    if WAVE.size + len(data) > 0x5c30 - 0x40:
        raise DataError(f"{label(path)}: the cry exceeds the game's 0x5c30-byte playback buffer")
    wave = WAVE.pack(0, 0, rate, ARM7_CLOCK // rate, 0, len(data) // 4) + data
    return swar_bytes([wave])


def wave_archive_waves(folder: Path, options: dict) -> list[bytes]:
    """Returns the waves of a wave archive's folder, from its numbered .swav and .wav files."""
    files = {}
    for path in folder.iterdir():
        if path.suffix in (".swav", ".wav"):
            if not path.stem.isdigit():
                raise DataError(f"{label(path)}: a wave's file is named by its number, as 000{path.suffix}")
            if int(path.stem) in files:
                raise DataError(f"{label(path)}: wave {int(path.stem)} has two files")
            files[int(path.stem)] = path
    if sorted(files) != list(range(len(files))):
        raise DataError(f"{label(folder)}: the waves must be numbered from 000 without gaps")
    waves = []
    for number in range(len(files)):
        path = files[number]
        if path.suffix == ".swav":
            waves.append(swav_wave(path.read_bytes(), label(path)))
        else:
            waves.append(wav_wave(path, options.get("waves", {}).get(path.stem, {}), options, label(path)))
    return waves


# Extracting


def extract(root: Path):
    from tools.data.make_constants import write_list
    data = (LISTS_DIR.parents[1] / "extract/b2_us/files/swan_sound_data.sdat").read_bytes()
    archive = read_sdat(data)
    entries, files = archive["entries"], archive["files"]
    raw = lambda offset, struct_: struct_.unpack_from(data, offset)  # noqa: E731

    for kind, (list_name, prefix) in LISTS.items():
        names, _ = entries[kind]
        if kind == "SEQ":
            continue  # sound.txt is make_constants.py's
        write_list(LISTS_DIR / f"{list_name}.txt", {i: name for i, name in enumerate(names) if name},
                   {"BANK": "Sound banks", "WAVEARC": "Sound wave archives"}[kind]
                   + ", with the names that the sound archive's symbols give them", hexadecimal=True)

    player_names = entries["PLAYER"][0]
    strm_player_names = entries["STRM_PLAYER"][0]
    owners: dict[str, dict[int, str]] = {}

    def owner(kind, file_id, name):
        """The first entry of a kind with a file, which those after it share."""
        return owners.setdefault(kind, {}).setdefault(file_id, name)

    names, records = entries["SEQ"]
    for number, (name, record) in enumerate(zip(names, records)):
        if record is None:
            continue
        file_id, bank, volume, channel_priority, player_priority, player, reserved = raw(record, SEQ_INFO)
        if file_id >> 24 or reserved:
            raise ValueError(f"{name}: unexpected sequence flags")
        stem = root / "sequences" / file_name(name, "SEQ_")
        info = {"$schema": "sequence.schema.json", "bank": entries["BANK"][0][bank], "volume": volume,
                "channel_priority": channel_priority, "player_priority": player_priority,
                "player": player_names[player]}
        first = owner("SEQ", file_id, name)
        if first != name:
            info["shares_data_with"] = first
        else:
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_suffix(".seq").write_text(sseq.disassemble(files[file_id]))
        write_json(stem.with_suffix(".json"), info)
    print(f"{sum(r is not None for r in records)} sequences", flush=True)

    wave_names = entries["WAVEARC"][0]
    names, records = entries["BANK"]
    for name, record in zip(names, records):
        if record is None:
            continue
        file_id, *archives = raw(record, BANK_INFO)
        if file_id >> 24:
            raise ValueError(f"{name}: unexpected bank flags")
        while archives and archives[-1] == NO_WAVE_ARCHIVE:
            archives.pop()
        info = {"$schema": "bank.schema.json",
                "wave_archives": [None if a == NO_WAVE_ARCHIVE else wave_names[a] for a in archives]}
        first = owner("BANK", file_id, name)
        if first != name:
            info["shares_data_with"] = first
        else:
            info["instruments"] = bank_json(files[file_id])
        write_json(root / "banks" / f"{file_name(name, 'BANK_')}.json", info, width=160)
    print(f"{sum(r is not None for r in records)} banks", flush=True)

    names, records = entries["WAVEARC"]
    for number, (name, record) in enumerate(zip(names, records)):
        if record is None:
            continue
        (value,) = raw(record, WAVEARC_INFO)
        file_id = value & 0xFFFFFF
        if value & ~0xFFFFFF & ~WAVEARC_SINGLE_LOAD:
            raise ValueError(f"{name}: unexpected wave archive flags")
        first = owner("WAVEARC", file_id, name)
        if number in CRY_WAVE_ARCHIVES:
            if first != name or value & WAVEARC_SINGLE_LOAD:
                raise ValueError(f"{name}: unexpected cry wave archive")
            continue
        folder = root / "wave_archives" / file_name(name, "WAVE_")
        folder.mkdir(parents=True, exist_ok=True)
        options = {}
        if value & WAVEARC_SINGLE_LOAD:
            options["single_load"] = True
        if first != name:
            options["shares_data_with"] = first
        else:
            for i, wave in enumerate(swar_waves(files[file_id])):
                (folder / f"{i:03}.swav").write_bytes(swav_bytes(wave))
        if options:
            write_json(folder / "data.json", {"$schema": "../wave_archive.schema.json", **options})
    print(f"{sum(r is not None for r in records)} wave archives", flush=True)

    players = []
    for name, record in zip(*entries["PLAYER"]):
        most, padding, channels, heap = raw(record, PLAYER_INFO)
        if padding:
            raise ValueError(f"{name}: unexpected player padding")
        players.append({"name": name, "max_sequences": most,
                        "channels": [c for c in range(16) if channels >> c & 1], "heap_size": heap})
    strm_players = []
    for name, record in zip(*entries["STRM_PLAYER"]):
        count, *channels = raw(record, STRM_PLAYER_INFO)
        if any(c != 0xFF for c in channels[count:]):
            raise ValueError(f"{name}: unexpected stream player channels")
        strm_players.append({"name": name, "channels": channels[:count]})
    streams = []
    for name, record in zip(*entries["STRM"]):
        file_id, volume, priority, player = raw(record, STRM_INFO)
        if file_id >> 24:
            raise ValueError(f"{name}: unexpected stream flags")
        (root / "streams").mkdir(parents=True, exist_ok=True)
        (root / "streams" / f"{file_name(name, 'STRM_')}.strm").write_bytes(files[file_id])
        streams.append({"name": name, "volume": volume, "player_priority": priority,
                        "player": strm_player_names[player]})
    groups = []
    seq_names, bank_names = entries["SEQ"][0], entries["BANK"][0]
    for name, record in zip(*entries["GROUP"]):
        count = struct.unpack_from("<I", data, record)[0]
        items = []
        for i in range(count):
            kind, flags, padding, number = GROUP_ITEM.unpack_from(data, record + 4 + 8 * i)
            if padding or flags >> len(LOAD_FLAGS) or kind > 2:
                raise ValueError(f"{name}: unexpected group item")
            items.append({"name": (seq_names, bank_names, wave_names)[kind][number],
                          "load": [f for b, f in enumerate(LOAD_FLAGS) if flags >> b & 1]})
        groups.append({"name": name, "items": items})
    write_json(root / "archive.json", {"$schema": "archive.schema.json", "players": players,
                                       "stream_players": strm_players, "streams": streams, "groups": groups})

    (bgm_info,) = read_narc((LISTS_DIR.parents[1] / "extract/b2_us/files/a/0/8/6").read_bytes())
    interactive = []
    for i in range(bgm_info[0]):
        bgm, subsystem = struct.unpack_from("<HB", bgm_info, 1 + 3 * i)
        interactive.append({"bgm": seq_names[bgm], "system": ISS_SUBSYSTEMS[subsystem]})
    if len(bgm_info) != 1 + 3 * len(interactive):
        raise ValueError("unexpected BGM info size")
    write_json(root / "interactive_music.json", {"$schema": "interactive_music.schema.json", "bgms": interactive})


# Packing


def pack_bgm_info(root: Path) -> bytes:
    """Returns the BGM info archive: the count of BGMs with interactive sound, then each one's sequence and
    subsystem."""
    path = root / "interactive_music.json"
    bgms = load(path, load_schema(root / "interactive_music.schema.json"))["bgms"]
    if len(bgms) > 0xFF:
        raise DataError(f"{label(path)}: at most 255 BGMs can have interactive sound")
    systems = {name: number for number, name in ISS_SUBSYSTEMS.items()}
    sequences = {name: number for number, name in load_list("SEQ").items()}
    data = bytearray([len(bgms)])
    for i, entry in enumerate(bgms):
        if entry["bgm"] not in sequences:
            raise DataError(f"{label(path)}: bgms[{i}]: {entry['bgm']!r} is not in data/constants/sound.txt")
        data += struct.pack("<HB", sequences[entry["bgm"]], systems[entry["system"]])
    return write_narc([bytes(data)])


def pack(root: Path, output: Path, bgm_info_output: Path):
    schemas = {kind: load_schema(root / path) for kind, path in
               (("archive", "archive.schema.json"), ("SEQ", "sequences/sequence.schema.json"),
                ("BANK", "banks/bank.schema.json"))}
    archive = load(root / "archive.json", schemas["archive"])
    lists = {kind: load_list(kind) for kind in LISTS}
    numbers = {kind: {name: number for number, name in entries.items()} for kind, entries in lists.items()}
    for kind, entries in lists.items():
        if entries and min(entries) < 0:
            raise DataError(f"data/constants/{LISTS[kind][0]}.txt: numbers start from 0")
    players = {p["name"]: i for i, p in enumerate(archive["players"])}
    strm_players = {p["name"]: i for i, p in enumerate(archive["stream_players"])}

    def find(kind, name, where):
        if name not in numbers[kind]:
            raise DataError(f"{where}: {name!r} is not in data/constants/{LISTS[kind][0]}.txt")
        return numbers[kind][name]

    # Each kind's files, keyed by the entry that owns them, in the order of their first use
    contents: dict[str, dict[str, bytes]] = {kind: {} for kind in ("SEQ", "BANK", "WAVEARC", "STRM")}
    shares: dict[str, dict[str, str]] = {kind: {} for kind in contents}
    infos: dict[str, dict[int, tuple]] = {kind: {} for kind in LISTS}

    for number, name in sorted(lists["SEQ"].items()):
        stem = root / "sequences" / file_name(name, "SEQ_")
        path = stem.with_suffix(".json")
        if not path.exists():
            continue
        info = load(path, schemas["SEQ"])
        where = label(path)
        if info["player"] not in players:
            raise DataError(f"{where}: player {info['player']!r} is not in data/sound/archive.json")
        infos["SEQ"][number] = (find("BANK", info["bank"], where), info["volume"], info["channel_priority"],
                                info["player_priority"], players[info["player"]])
        sources = [p for p in (stem.with_suffix(".seq"), stem.with_suffix(".mid")) if p.exists()]
        if "shares_data_with" in info:
            if sources:
                raise DataError(f"{where}: a sequence that shares another's data has no {sources[0].suffix} file")
            find("SEQ", info["shares_data_with"], where)
            shares["SEQ"][name] = info["shares_data_with"]
        elif len(sources) != 1:
            raise DataError(f"{where}: needs one {stem.name}.seq or {stem.name}.mid beside it")
        elif sources[0].suffix == ".seq":
            try:
                contents["SEQ"][name] = sseq.assemble(sources[0].read_text(), label(sources[0]))
            except ValueError as error:
                raise DataError(str(error)) from None
        else:
            contents["SEQ"][name] = sseq.assemble(convert(sources[0], info), label(sources[0]))

    for number, name in sorted(lists["BANK"].items()):
        path = root / "banks" / f"{file_name(name, 'BANK_')}.json"
        if not path.exists():
            continue
        info = load(path, schemas["BANK"])
        where = label(path)
        archives = [NO_WAVE_ARCHIVE if a is None else find("WAVEARC", a, where) for a in info["wave_archives"]]
        infos["BANK"][number] = tuple(archives + [NO_WAVE_ARCHIVE] * (4 - len(archives)))
        if "shares_data_with" in info:
            find("BANK", info["shares_data_with"], where)
            shares["BANK"][name] = info["shares_data_with"]
        else:
            contents["BANK"][name] = bank_bytes(info["instruments"], where)

    for number, name in sorted(lists["WAVEARC"].items()):
        if number in CRY_WAVE_ARCHIVES:
            infos["WAVEARC"][number] = (0,)
            contents["WAVEARC"][name] = cry_swar(cry_folder(root.parent / "pokemon", number) / "cry.wav")
            continue
        folder = root / "wave_archives" / file_name(name, "WAVE_")
        if not folder.is_dir():
            continue
        options = {}
        if (folder / "data.json").exists():
            options = load(folder / "data.json", load_schema(root / "wave_archives/wave_archive.schema.json"))
        infos["WAVEARC"][number] = (WAVEARC_SINGLE_LOAD if options.get("single_load") else 0,)
        if "shares_data_with" in options:
            find("WAVEARC", options["shares_data_with"], label(folder))
            shares["WAVEARC"][name] = options["shares_data_with"]
        else:
            contents["WAVEARC"][name] = swar_bytes(wave_archive_waves(folder, options))

    for stream in archive["streams"]:
        path = root / "streams" / f"{file_name(stream['name'], 'STRM_')}.strm"
        contents["STRM"][stream["name"]] = path.read_bytes()

    # The files, in the order of the entries that first use them
    files: list[bytes] = []
    file_ids: dict[tuple[str, str], int] = {}

    def file_of(kind, name):
        seen = []
        while name in shares[kind]:
            seen.append(name)
            name = shares[kind][name]
            if name in seen:
                raise DataError(f"{name} shares data in a loop")
        if name not in contents[kind]:
            raise DataError(f"{seen[-1]} shares the data of {name}, which has none of its own")
        if (kind, name) not in file_ids:
            file_ids[kind, name] = len(files)
            files.append(contents[kind][name])
        return file_ids[kind, name]

    names = {kind: [] for kind in KINDS}
    records = {kind: [] for kind in KINDS}
    for kind, info_struct in (("SEQ", SEQ_INFO), ("BANK", BANK_INFO), ("WAVEARC", WAVEARC_INFO)):
        count = max(infos[kind], default=-1) + 1
        for number in range(count):
            name = lists[kind].get(number)
            if number not in infos[kind]:
                names[kind].append(None)
                records[kind].append(None)
                continue
            names[kind].append(name)
            fields = infos[kind][number]
            file_id = file_of(kind, name)
            if kind == "SEQ":
                records[kind].append(SEQ_INFO.pack(file_id, *fields, 0))
            elif kind == "BANK":
                records[kind].append(BANK_INFO.pack(file_id, *fields))
            else:
                records[kind].append(WAVEARC_INFO.pack(file_id | fields[0]))
    for player in archive["players"]:
        names["PLAYER"].append(player["name"])
        mask = sum(1 << c for c in set(player["channels"]))
        records["PLAYER"].append(PLAYER_INFO.pack(player["max_sequences"], 0, mask, player["heap_size"]))
    for group in archive["groups"]:
        names["GROUP"].append(group["name"])
        record = struct.pack("<I", len(group["items"]))
        for item in group["items"]:
            prefix = next((p for p in GROUP_KINDS if item["name"].startswith(p)), None)
            if prefix is None:
                raise DataError(f"data/sound/archive.json: group item {item['name']!r} is not a sequence, bank or "
                                "wave archive")
            kind = ("SEQ", "BANK", "WAVEARC")[GROUP_KINDS[prefix]]
            flags = sum(1 << LOAD_FLAGS.index(f) for f in item["load"])
            number = find(kind, item["name"], "data/sound/archive.json")
            record += GROUP_ITEM.pack(GROUP_KINDS[prefix], flags, 0, number)
        records["GROUP"].append(record)
    for player in archive["stream_players"]:
        names["STRM_PLAYER"].append(player["name"])
        channels = player["channels"] + [0xFF] * (16 - len(player["channels"]))
        records["STRM_PLAYER"].append(STRM_PLAYER_INFO.pack(len(player["channels"]), *channels))
    for stream in archive["streams"]:
        if stream["player"] not in strm_players:
            raise DataError(f"data/sound/archive.json: stream player {stream['player']!r} is not in stream_players")
        names["STRM"].append(stream["name"])
        records["STRM"].append(STRM_INFO.pack(file_of("STRM", stream["name"]), stream["volume"],
                                              stream["player_priority"], strm_players[stream["player"]]))
    for path, data in ((output, write_sdat(names, records, files)), (bgm_info_output, pack_bgm_info(root))):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.write_bytes(data)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "extract":
            extract(Path(sys.argv[2]))
        elif len(sys.argv) == 5 and sys.argv[1] == "pack":
            pack(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        else:
            raise SystemExit(__doc__)
    except DataError as error:
        sys.exit(f"error: {error}")
