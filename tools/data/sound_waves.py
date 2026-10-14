#!/usr/bin/env python3
"""Nitro waves from WAV files, for the wave archives of data/sound/wave_archives/, and back.

    sound_waves.py decode WAVE.swav OUT.wav

A wave is PCM8, PCM16 or IMA ADPCM at a sample rate the sound driver plays it at, and it either plays once or loops
from a sample to its end. The packer (sound_data.py) encodes a WAV file of a wave archive with wav_wave(): mono, 8 or
16 bits, its loop from its first smpl chunk loop, which ends the wave. decode writes a wave as a WAV, with its loop,
to listen to or to edit and save over it as a .wav.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data.datajson import DataError  # noqa: E402

WAVE = struct.Struct("<BBHHHI")  # Encoding, loops, rate, timer, loop start and length after it, in words
PCM8, PCM16, ADPCM = 0, 1, 2
ENCODINGS = {"pcm8": PCM8, "pcm16": PCM16, "adpcm": ADPCM}
ARM7_CLOCK = 16756991
# Samples per word of each encoding
PER_WORD = {PCM8: 4, PCM16: 2, ADPCM: 8}
INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8]
STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107,
    118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]


def read_wav(path: Path, where: str) -> tuple[int, list[int], int | None, int | None]:
    """Returns a WAV file's sample rate, its samples as PCM16, and its first loop's start and end samples, if any."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise DataError(f"{where}: not a WAV file")
    chunks = {}
    pos = 12
    while pos + 8 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, pos)
        chunks.setdefault(tag, data[pos + 8:pos + 8 + size])
        pos += 8 + size + (size & 1)
    if b"fmt " not in chunks or b"data" not in chunks:
        raise DataError(f"{where}: a WAV file needs fmt and data chunks")
    fmt, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", chunks[b"fmt "])
    if fmt not in (1, 0xFFFE) or channels != 1 or bits not in (8, 16):
        raise DataError(f"{where}: expected a mono PCM WAV of 8 or 16 bits, not {channels} channels of {bits} bits "
                        f"(format {fmt})")
    raw = chunks[b"data"]
    if bits == 8:
        samples = [(byte - 128) << 8 for byte in raw]
    else:
        samples = [value for (value,) in struct.iter_unpack("<h", raw[:len(raw) & ~1])]
    loop_start = loop_end = None
    smpl = chunks.get(b"smpl")
    if smpl and len(smpl) >= 36 and struct.unpack_from("<I", smpl, 28)[0] and len(smpl) >= 60:
        _, _, loop_start, loop_end = struct.unpack_from("<IIII", smpl, 36)
    return rate, samples, loop_start, loop_end


def adpcm_encode(samples: list[int]) -> bytes:
    """Encodes PCM16 samples as the sound hardware's IMA ADPCM: a header with the first sample and the step index,
    then a nibble per sample, low nibble first. Each nibble is the one whose decoding comes nearest, and the encoder
    follows the hardware's decoding, so that the errors don't add up."""
    predictor = samples[0] if samples else 0
    index = 0
    out = bytearray(struct.pack("<hH", predictor, index))
    nibbles = []
    for sample in samples:
        step = STEP_TABLE[index]
        best = None
        for nibble in range(16):
            diff = step >> 3
            if nibble & 1:
                diff += step >> 2
            if nibble & 2:
                diff += step >> 1
            if nibble & 4:
                diff += step
            value = max(-0x7FFF, predictor - diff) if nibble & 8 else min(0x7FFF, predictor + diff)
            error = abs(sample - value)
            if best is None or error < best[0]:
                best = (error, nibble, value)
        _, nibble, predictor = best
        index = min(88, max(0, index + INDEX_TABLE[nibble & 7]))
        nibbles.append(nibble)
    if len(nibbles) & 1:
        nibbles.append(0)
    out += bytes(nibbles[i] | nibbles[i + 1] << 4 for i in range(0, len(nibbles), 2))
    return bytes(out)


def adpcm_decode(data: bytes) -> list[int]:
    predictor, index = struct.unpack_from("<hH", data)
    index = min(88, index)
    samples = []
    for byte in data[4:]:
        for nibble in (byte & 0xF, byte >> 4):
            step = STEP_TABLE[index]
            diff = step >> 3
            if nibble & 1:
                diff += step >> 2
            if nibble & 2:
                diff += step >> 1
            if nibble & 4:
                diff += step
            predictor = max(-0x7FFF, predictor - diff) if nibble & 8 else min(0x7FFF, predictor + diff)
            index = min(88, max(0, index + INDEX_TABLE[nibble & 7]))
            samples.append(predictor)
    return samples


def wave_bytes(rate: int, samples: list[int], encoding: int, loop_start: int | None, where: str) -> bytes:
    """Returns a wave of PCM16 samples: looping from loop_start to the end, or not looping for None. The loop start
    moves to a whole word of the encoding by silence before the wave, and a looping wave fills its last word from
    its loop."""
    if not 1 <= rate <= 0xFFFF or ARM7_CLOCK // rate > 0xFFFF:
        raise DataError(f"{where}: the sample rate {rate} Hz is out of range; resample it, to 32768 Hz or less")
    if not samples:
        raise DataError(f"{where}: the wave has no samples")
    per_word = PER_WORD[encoding]
    if loop_start is not None:
        if not 0 <= loop_start < len(samples):
            raise DataError(f"{where}: the loop starts at sample {loop_start}, past the wave's end")
        lead = -loop_start % per_word
        samples = [0] * lead + samples
        loop_start += lead
        loop = samples[loop_start:]
        while len(samples) % per_word:
            samples.append(loop[(len(samples) - loop_start) % len(loop)])
    else:
        samples = samples + [0] * (-len(samples) % per_word)
    if encoding == PCM8:
        data = bytes((max(-128, min(127, (value + 128) >> 8))) & 0xFF for value in samples)
    elif encoding == PCM16:
        data = struct.pack(f"<{len(samples)}h", *samples)
    else:
        data = adpcm_encode(samples)
    # The loop start counts in words from the data's start, the ADPCM header's word included
    header_words = 1 if encoding == ADPCM else 0
    start_words = header_words + (loop_start // per_word if loop_start is not None else 0)
    total_words = len(data) // 4
    if total_words > 0x3FFFFF:
        raise DataError(f"{where}: the wave is too long for the sound hardware")
    return WAVE.pack(encoding, loop_start is not None, rate, ARM7_CLOCK // rate, start_words,
                     total_words - start_words) + data


def wav_wave(path: Path, options: dict, archive_options: dict, where: str) -> bytes:
    """Returns the wave of a wave archive's WAV file, with its options and its archive's."""
    rate, samples, loop_start, loop_end = read_wav(path, where)
    encoding = ENCODINGS[options.get("encoding", archive_options.get("encoding", "adpcm"))]
    if "loop" in options:
        loop_start = options.get("loop_start", 0) if options["loop"] else None
        loop_end = None
    elif "loop_start" in options:
        loop_start = options["loop_start"]
    if loop_start is not None and loop_end is not None and loop_start < loop_end < len(samples):
        samples = samples[:loop_end + 1]
    return wave_bytes(rate, samples, encoding, loop_start, where)


def decode(wave: bytes) -> tuple[int, list[int], int | None]:
    """Returns a wave's sample rate, its samples as PCM16 and the sample its loop starts at, or None."""
    encoding, loops, rate, _, start_words, _ = WAVE.unpack_from(wave)
    data = wave[WAVE.size:]
    if encoding == PCM8:
        samples = [(byte - 256 if byte > 127 else byte) << 8 for byte in data]
        loop_start = start_words * 4
    elif encoding == PCM16:
        samples = [value for (value,) in struct.iter_unpack("<h", data)]
        loop_start = start_words * 2
    else:
        samples = adpcm_decode(data)
        loop_start = (start_words - 1) * 8
    return rate, samples, loop_start if loops else None


def wav_bytes(rate: int, samples: list[int], loop_start: int | None) -> bytes:
    """Returns a mono PCM16 WAV file, with a smpl chunk for its loop to its end."""
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunks = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16)
    chunks += struct.pack("<4sI", b"data", len(data)) + data + bytes(len(data) & 1)
    if loop_start is not None:
        smpl = struct.pack("<9I", 0, 0, 1_000_000_000 // rate, 60, 0, 0, 0, 1, 0)
        smpl += struct.pack("<6I", 0, 0, loop_start, len(samples) - 1, 0, 0)
        chunks += struct.pack("<4sI", b"smpl", len(smpl)) + smpl
    return struct.pack("<4sI4s", b"RIFF", 4 + len(chunks), b"WAVE") + chunks


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "decode":
        swav = Path(sys.argv[2]).read_bytes()
        if swav[:4] != b"SWAV":
            raise SystemExit(f"{sys.argv[2]}: not a Nitro wave file")
        Path(sys.argv[3]).write_bytes(wav_bytes(*decode(swav[0x18:])))
    else:
        raise SystemExit(__doc__)
