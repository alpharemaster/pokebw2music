#!/usr/bin/env python3
"""Export the sound archive's instrument banks as DLS files, to play the music's MIDI files with the game's
instruments.

    sound_dls.py data/sound OUT_DIR [--all | BANK_OR_SEQUENCE ...]

Without names it exports the banks of the music and jingles (SEQ_BGM_*, SEQ_ME_*); with names, those banks or the
banks of those sequences; with --all, every bank. Each bank becomes OUT_DIR/<bank>.dls, and OUT_DIR/songs.txt says
which file each music sequence's .mid plays with.

A bank's program N is the DLS instrument N of bank N // 128 (the MIDI files' bank select), once as a melodic
instrument and once more as a drum kit, so that a General MIDI synthesizer finds it on channel 10 too; a drum set
and a key split are one instrument with a region for each key or range. The samples are decoded to PCM16 at their
true rate (the hardware's 16756991 Hz over the wave's timer), the PSG instruments get square waves of their duty
and the noise instrument the noise generator's sequence, at the pitch the hardware gives them at their base key
(timer 0x1F46, middle C for the square waves).

The envelopes follow the sound driver of the ARM7 (the envelope update at 0x03800ea0, the attack table at
0x03808f5c, the decay and release rates at 0x038014d4 and the decibel table at 0x03808e48), which updates each
channel every 2728 * 64 cycles, about 5.2 ms:
- the attenuation starts at -72.3 dB and the attack multiplies it by a / 256 each update, a being 255 - attack, or
  ATTACK_TABLE's for an attack of 109 and up. DLS's attack rises linearly in amplitude, so its time is twice the time
  the driver takes to reach -6 dB, which puts the half-way point, and nearly the 90% point, where the driver has
  them;
- the decay and the release lower it by a fixed rate in 1/1280 dB each update, linear in dB as DLS's are, so their
  DLS times, the times to fall 96 dB, are exact;
- the sustain level is 400 * log10(sustain / 127) tenths of a dB, which DLS takes in 0.1% of its 96 dB range.
The velocity, volume and expression need nothing: the driver's curve, 40 * log10(value / 127) dB, is DLS's default
concave curve.
"""
import argparse
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data import sseq  # noqa: E402
from tools.data.datajson import DataError  # noqa: E402
from tools.data.sound_data import CRY_WAVE_ARCHIVES, cry_swar, file_name, load_list, swar_waves, swav_wave  # noqa: E402
from tools.data.pokemon_cries import cry_folder  # noqa: E402
from tools.data.sound_waves import WAVE, decode, wav_wave  # noqa: E402

ARM7_CLOCK = 16756991
FRAME = 2728 * 64 / 33513982                   # Seconds between the driver's envelope updates
ATTACK_TABLE = [0x00, 0x01, 0x05, 0x0E, 0x1A, 0x26, 0x33, 0x3F, 0x49, 0x54, 0x5C, 0x64, 0x6D, 0x74, 0x7B, 0x7F,
                0x84, 0x89, 0x8F]
SILENT = -723 << 7                             # The attenuation a note starts at, in 1/1280 dB
PER_DB = 1280                                  # The attenuation's units per dB
DLS_RANGE_DB = 96                              # The range of DLS's volume envelope
PSG_TIMER = 0x1F46                             # The timer of the PSG and noise channels at their base key
PSG_STEPS = 32                                 # Samples per cycle of the exported square waves

# DLS connections: sources, destinations and transforms
SRC_NONE, DST_PAN = 0x0000, 0x0004
DST_ATTACK, DST_DECAY, DST_RELEASE, DST_SUSTAIN = 0x0206, 0x0207, 0x0209, 0x020A
DRUMS = 0x80000000


# The sound driver's envelope


def attack_rate(attack: int) -> int:
    return ATTACK_TABLE[127 - attack] if attack >= 109 else 255 - attack


def fall_rate(value: int) -> int:
    """The decay or release rate of a value: its attenuation's fall per update."""
    if value == 127:
        return 0xFFFF
    if value == 126:
        return 0x3C00
    if value < 50:
        return value * 2 + 1
    return 0x1E00 // (126 - value)


def sustain_db(sustain: int) -> float:
    return -3276.8 if sustain == 0 else max(-72.2, round(400 * math.log10(sustain / 127)) / 10)


def attack_seconds(attack: int) -> float:
    """DLS's attack time for the driver's attack: twice its time to reach -6 dB, or 0 for an instant one."""
    rate = attack_rate(attack)
    if rate == 0:
        return 0
    level, frames = SILENT, 0
    while level < -6.02 * PER_DB:
        level = -((-level * rate) >> 8)
        frames += 1
    return 2 * frames * FRAME


def fall_seconds(value: int) -> float:
    """DLS's decay or release time, which DLS measures over its whole range."""
    return DLS_RANGE_DB * PER_DB / fall_rate(value) * FRAME


def time_cents(seconds: float) -> int:
    if seconds <= 0:
        return -0x80000000
    return round(1200 * math.log2(seconds) * 65536)


# DLS


def chunk(tag: bytes, data: bytes) -> bytes:
    return tag + struct.pack("<I", len(data)) + data + bytes(len(data) & 1)


def riff_list(kind: bytes, *chunks: bytes) -> bytes:
    return chunk(b"LIST", kind + b"".join(chunks))


def info(name: str) -> bytes:
    return riff_list(b"INFO", chunk(b"INAM", name.encode("ascii", "replace") + b"\0"))


def articulation(note: dict) -> bytes:
    """The region's envelope and pan, as DLS level 1 connections."""
    sustain = max(0.0, 1 + sustain_db(note["sustain"]) / DLS_RANGE_DB)
    blocks = [
        (SRC_NONE, SRC_NONE, DST_ATTACK, 0, time_cents(attack_seconds(note["attack"]))),
        (SRC_NONE, SRC_NONE, DST_DECAY, 0, time_cents(fall_seconds(note["decay"]))),
        (SRC_NONE, SRC_NONE, DST_SUSTAIN, 0, round(sustain * 1000 * 65536)),
        (SRC_NONE, SRC_NONE, DST_RELEASE, 0, time_cents(fall_seconds(note["release"]))),
        (SRC_NONE, SRC_NONE, DST_PAN, 0, round((note["pan"] - 64) * 500 / 64 * 65536)),
    ]
    art = struct.pack("<II", 8, len(blocks)) + b"".join(struct.pack("<HHHHi", *block) for block in blocks)
    return riff_list(b"lart", chunk(b"art1", art))


def wave_sample(unity: int, fine: int, loop: tuple[int, int] | None) -> bytes:
    data = struct.pack("<IHhiII", 20, unity, fine, 0, 0, 1 if loop else 0)
    if loop:
        data += struct.pack("<IIII", 16, 0, *loop)
    return chunk(b"wsmp", data)


class Pool:
    """The waves of a DLS file, each PCM16 with its rate, fine tune and loop, by what they come from."""
    def __init__(self):
        self.waves = []
        self.index = {}

    def add(self, key, rate: float, samples: list[int], loop_start: int | None) -> int:
        if key not in self.index:
            whole = max(1, round(rate))
            fine = round(1200 * math.log2(rate / whole))
            loop = (loop_start, len(samples) - loop_start) if loop_start is not None else None
            self.index[key] = len(self.waves)
            self.waves.append((whole, fine, samples, loop, key))
        return self.index[key]

    def fine(self, index: int) -> int:
        return self.waves[index][1]

    def loop(self, index: int):
        return self.waves[index][3]

    def chunks(self) -> tuple[bytes, bytes]:
        lists = []
        for rate, fine, samples, loop, key in self.waves:
            fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
            data = struct.pack(f"<{len(samples)}h", *samples)
            lists.append(riff_list(b"wave", chunk(b"fmt ", fmt), wave_sample(60, fine, loop), chunk(b"data", data),
                                   info(str(key))))
        offsets, cursor = [], 0
        for item in lists:
            offsets.append(cursor)
            cursor += len(item)
        table = chunk(b"ptbl", struct.pack(f"<II{len(offsets)}I", 8, len(offsets), *offsets))
        return table, riff_list(b"wvpl", *lists)


def psg_wave(duty: int) -> tuple[float, list[int]]:
    """A cycle of the square wave of a PSG duty, high for (duty + 1) / 8 of it, at middle C's rate."""
    high = (duty + 1) * PSG_STEPS // 8
    return ARM7_CLOCK / PSG_TIMER / 8 * PSG_STEPS, [0x7FFF if i < high else -0x7FFF for i in range(PSG_STEPS)]


def noise_wave() -> tuple[float, list[int]]:
    """The noise generator's 15-bit sequence, a sample per step of its timer."""
    state, samples = 0x7FFF, []
    for _ in range(0x7FFF):
        if state & 1:
            state = (state >> 1) ^ 0x6000
            samples.append(-0x7FFF)
        else:
            state >>= 1
            samples.append(0x7FFF)
    return ARM7_CLOCK / PSG_TIMER, samples


class Waves:
    """The sound archive's waves, decoded, by wave archive and number."""
    def __init__(self, root: Path):
        self.root = root
        self.archives = {name: number for number, name in load_list("WAVEARC").items()}
        self.cache = {}

    def get(self, archive: str, number: int):
        if archive not in self.cache:
            self.cache[archive] = self.load(archive, set())
        waves = self.cache[archive]
        if not 0 <= number < len(waves):
            return None
        return waves[number]

    def load(self, archive: str, seen: set) -> list:
        if archive in seen or archive not in self.archives:
            return []
        seen.add(archive)
        number = self.archives[archive]
        if number in CRY_WAVE_ARCHIVES:
            raw = swar_waves(cry_swar(cry_folder(self.root.parent / "pokemon", number) / "cry.wav"))
        else:
            folder = self.root / "wave_archives" / file_name(archive, "WAVE_")
            options = json.loads((folder / "data.json").read_text()) if (folder / "data.json").exists() else {}
            if "shares_data_with" in options:
                return self.load(options["shares_data_with"], seen)
            raw = []
            for i in range(10000):
                swav, wav = folder / f"{i:03}.swav", folder / f"{i:03}.wav"
                if swav.exists():
                    raw.append(swav_wave(swav.read_bytes(), str(swav)))
                elif wav.exists():
                    raw.append(wav_wave(wav, options.get("waves", {}).get(f"{i:03}", {}), options, str(wav)))
                else:
                    break
        waves = []
        for wave in raw:
            timer = WAVE.unpack_from(wave)[3]
            _, samples, loop_start = decode(wave)
            waves.append((ARM7_CLOCK / timer, samples, loop_start))
        return waves


def bank_dls(name: str, bank: dict, waves: Waves) -> tuple[bytes, list[str]]:
    """Returns a bank's DLS file, and what it couldn't export."""
    pool = Pool()
    problems = []
    slots = bank.get("wave_archives", [])

    def region(low: int, high: int, note: dict, where: str) -> bytes | None:
        kind = note["type"]
        key = note["key"]
        key = sseq.parse_key(key) if isinstance(key, str) else key
        if kind in ("pcm", "direct_pcm"):
            slot = note["wave_archive"]
            archive = slots[slot] if slot < len(slots) else None
            wave = waves.get(archive, note["wave"]) if archive else None
            if wave is None:
                problems.append(f"{where}: wave {note['wave']} of wave archive slot {slot} is missing")
                return None
            index = pool.add((archive, note["wave"]), *wave)
        elif kind == "psg":
            index = pool.add(("psg", note["wave"]), *psg_wave(min(6, note["wave"])), 0)
        else:
            index = pool.add(("noise",), *noise_wave(), 0)
        header = struct.pack("<HHHHHH", low, high, 0, 127, 0, 0)
        return riff_list(b"rgn ", chunk(b"rgnh", header), wave_sample(key, pool.fine(index), pool.loop(index)),
                         chunk(b"wlnk", struct.pack("<HHII", 0, 0, 1, index)), articulation(note))

    instruments = []
    for i, instrument in enumerate(bank.get("instruments", [])):
        programs = instrument["program"]
        programs = [] if programs is None else programs if isinstance(programs, list) else [programs]
        if not programs:
            continue
        where = f"{name} instruments[{i}]"
        if instrument["type"] == "drum_set":
            first = instrument["first_key"]
            first = sseq.parse_key(first) if isinstance(first, str) else first
            regions = [region(first + k, first + k, drum, f"{where}.drums[{k}]")
                       for k, drum in enumerate(instrument["drums"])]
        elif instrument["type"] == "key_split":
            regions, low = [], 0
            for k, part in enumerate(instrument["regions"]):
                last = part["last_key"]
                last = sseq.parse_key(last) if isinstance(last, str) else last
                if last >= low:  # A region after a higher one never plays
                    regions.append(region(low, last, part, f"{where}.regions[{k}]"))
                    low = last + 1
        else:
            regions = [region(0, 127, instrument, where)]
        regions = [r for r in regions if r]
        if not regions:
            continue
        for program in programs:
            for drums in (0, DRUMS):
                header = struct.pack("<III", len(regions), drums | (program // 128) << 8, program % 128)
                label = f"{name} program {program}" + (" (drums)" if drums else "")
                instruments.append(riff_list(b"ins ", chunk(b"insh", header), riff_list(b"lrgn", *regions),
                                             info(label)))
    table, pool_list = pool.chunks()
    body = (b"DLS " + chunk(b"vers", struct.pack("<II", 1 << 16, 0))
            + chunk(b"colh", struct.pack("<I", len(instruments))) + riff_list(b"lins", *instruments) + table
            + pool_list + info(name))
    return chunk(b"RIFF", body), problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="data/sound")
    parser.add_argument("output", type=Path, help="the directory to write the DLS files to")
    parser.add_argument("names", nargs="*", help="banks (BANK_*) or sequences (SEQ_*) to export")
    parser.add_argument("--all", action="store_true", help="export every bank")
    args = parser.parse_args()
    banks = {name: name for name in load_list("BANK").values()}
    songs = {}
    for number, name in load_list("SEQ").items():
        path = args.root / "sequences" / f"{file_name(name, 'SEQ_')}.json"
        if path.exists():
            songs[name] = json.loads(path.read_text())["bank"]
    if args.all:
        wanted = sorted(banks)
    elif args.names:
        wanted = []
        for name in args.names:
            if name in banks:
                wanted.append(name)
            elif name in songs:
                wanted.append(songs[name])
            else:
                sys.exit(f"error: {name} is neither a bank nor a sequence")
    else:
        wanted = sorted({bank for song, bank in songs.items() if song.startswith(("SEQ_BGM_", "SEQ_ME_"))})
    args.output.mkdir(parents=True, exist_ok=True)
    waves = Waves(args.root)
    for name in dict.fromkeys(wanted):
        path = args.root / "banks" / f"{file_name(name, 'BANK_')}.json"
        bank = json.loads(path.read_text())
        seen = set()
        while "shares_data_with" in bank and bank["shares_data_with"] not in seen:
            seen.add(bank["shares_data_with"])
            shared = json.loads((args.root / "banks" / f"{file_name(bank['shares_data_with'], 'BANK_')}.json")
                                .read_text())
            bank = {**shared, "wave_archives": bank["wave_archives"]}
        try:
            data, problems = bank_dls(name, bank, waves)
        except DataError as error:
            sys.exit(f"error: {error}")
        (args.output / f"{file_name(name, 'BANK_')}.dls").write_bytes(data)
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
    lines = [f"{file_name(song, 'SEQ_')}.mid {file_name(bank, 'BANK_')}.dls" for song, bank in sorted(songs.items())
             if song.startswith(("SEQ_BGM_", "SEQ_ME_")) and bank in wanted]
    (args.output / "songs.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(dict.fromkeys(wanted))} banks in {args.output}")


if __name__ == "__main__":
    main()
