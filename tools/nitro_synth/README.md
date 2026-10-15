# Nitro Synth

A VST3 instrument that plays the game's sound banks the way Pokémon Black 2 and White 2's sound driver does. Its
layout follows Fruity LSD's: load a DLS file, then each of the 16 MIDI channels plays a track with that bank. It
plays the DLS files that `tools/data/sound_dls.py` exports to `build/dls/`, and the MIDI files of
`data/sound/sequences/` play in it as they do in the game, controllers included.

## Building

It is built with Visual Studio's compiler (2022 or later; 2026 builds it). Add the "Desktop development with C++"
workload in the Visual Studio Installer, then run this from WSL:

```sh
tools/nitro_synth/build_windows.sh            # build/nitro_synth/NitroSynth.vst3
tools/nitro_synth/build_windows.sh --install  # and copy it to C:\Program Files\Common Files\VST3
```

The script fetches [DPF](https://github.com/DISTRHO/DPF) (the DISTRHO Plugin Framework, ISC) at a pinned commit.
Installing to `Program Files` needs administrator rights; without them, copy the bundle there by hand or add
`build\nitro_synth` to FL Studio's plugin search paths.

## Playing a song

1. Export the banks: `python3 tools/data/sound_dls.py data/sound build/dls`. `build/dls/songs.txt` says which bank
   each song plays.
2. In FL Studio, add Nitro Synth, click **Load DLS...** and pick the song's bank, such as `mus_wb_pokecen.dls`.
3. Import the song's MIDI, such as `data/sound/sequences/bgm_pokecen.mid`, and route its channels to the plugin
   (MIDI channel N is the game's track N - 1).
4. Set **Song volume** to the song's `"volume"` in its JSON (66 for the Pokémon Center; 90, the BGMs' median, by
   default) and **Channel priority** to its `"channel_priority"`, for the loudness and voice stealing that the game
   gives it.

## Controllers

The MIDI exports write the driver's commands as these controllers, and the plugin plays them as the driver does:

| Controller | Command | |
| --- | --- | --- |
| 0 | Bank select | Program + 128 per step |
| 85 | Program | With the bank select, the same as a program change, which the exports write beside each one |
| 1 | Modulation depth | |
| 3 | Modulation type | 0 pitch, 1 volume, 2 pan |
| 5 | Portamento time | 0 slides over the note's length (see below), more over `(time² × semitones × 64) >> 11` frames |
| 7, 11 | Volume, expression | On the driver's curve, 40 × log10(value / 127) dB |
| 10 | Pan | |
| 14 | Track priority | Adds to the channel priority, for the 16 channels |
| 65 | Portamento on | At 64 and up |
| 72, 73, 75, 79 | Release, attack, decay, sustain | The track's envelope, over its instruments' |
| 76, 77, 78 | Modulation speed, range, delay | Delay in frames |
| 84 | Portamento key | Slides the next note from this key, and turns portamento on |
| 102, 103 | Pitch sweep | High and low 7 bits, plus 8192, in 1/64 semitones |
| RPN 0 | Bend range | |
| 121 | Reset | The track's settings, as a new track has them |

Pitch bend is the driver's: 14-bit bends are cut to its 256 steps.

VST3 has no MIDI program change event: a host passes program changes as a parameter that the plugin maps to
controller 130. DPF leaves that out, so `patch_dpf.py` adds it to the build's copy of DPF. Since a host may still not
pass them, the exports also write each program as controller 85, and each channel has a **Program** parameter, which
host automation can set and which the mouse wheel over a channel's program in the UI steps through the bank's.

## What it emulates

Everything below is taken from the game's ARM7 sound driver (the addresses are in `src/NitroEngine.hpp`):

- Every frame (2728 × 64 cycles, about 5.2 ms) each note's envelope, modulation, sweep and portamento move as the
  driver moves them, and its timer, volume and pan are set again.
- A note takes one of the 16 hardware channels as the driver allocates them: PCM any of `PLAYER_BGM`'s (1 to 13
  and 15), PSG 8 to 13, noise 14 and 15, in the driver's order, else the one with the lowest priority, and is
  dropped if that one's is higher. A released note's priority drops to 1.
- **Hardware output** mixes the channels at the DS's 32728.5 Hz without interpolation, with each channel's 7-bit
  volume and divider and the 10-bit output, as the console sounds. Turned off, the same channels play at the host's
  rate, interpolated.
- **16 channel limit** off gives 64 voices without the hardware's channel rules; **All 16 channels** lets the
  music use the channels that `PLAYER_BGM` leaves to the sound effects.

Two things differ from the game:

- A portamento time of 0 slides over the note's length, which a plugin can't know when the note starts, so it
  slides over the length of the channel's previous note (a quarter note before the first).
- The timer and volume tables are in the DS BIOS, not the ROM, so they are computed from their formulas.

## Sources

- `src/NitroEngine.*`: the sound driver.
- `src/NitroBank.*`: reading the DLS files, with each region's `dsrg` chunk of what the driver plays (the wave
  type, PSG duty, base key, envelope, pan and timer). Other DLS files play too, with the envelope nearest to their
  articulation.
- `src/NitroPlugin.*`, `src/NitroUI.cpp`: the DPF plugin and its UI.
- `test/render.cpp`: plays a MIDI file with a DLS file into a WAV file without a host, to test the engine on Linux:
  `g++ -std=c++17 -O2 test/render.cpp src/NitroEngine.cpp src/NitroBank.cpp -o render`.
