// NDS Synth: an instrument bank, read from a DLS file that tools/data/sound_dls.py exported.
//
// Each region of those files has a "dsrg" chunk with what the DS sound driver plays: the wave's type, the PSG duty,
// the base key, the envelope and pan, and the wave's timer. A DLS file without it plays too, with its regions'
// pitch, loop and pan and the envelope nearest to its articulation.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace nds {

enum class WaveType : uint8_t { Pcm = 1, Psg = 2, Noise = 3 };

struct Sample {
    std::vector<int16_t> data;
    uint32_t loopStart = 0;
    bool looped = false;
};

struct Region {
    uint8_t low = 0, high = 127;
    WaveType type = WaveType::Pcm;
    uint8_t duty = 0;             // PSG duty: high for (duty + 1) / 8 of a cycle
    uint8_t baseKey = 60;         // The key that plays the wave at its timer
    uint8_t attack = 127, decay = 127, sustain = 127, release = 127, pan = 64;
    uint16_t timer = 0x1F46;      // The sound timer of the wave at its base key
    int sample = -1;
};

struct Instrument {
    std::string name;
    std::vector<Region> regions;
    const Region* find(int key) const;
};

struct Bank {
    std::string name;
    std::vector<Sample> samples;
    std::map<uint32_t, Instrument> instruments;  // By bank select * 128 + program
    std::map<uint32_t, Instrument> drums;        // The drum kits of a DLS file, which a channel takes when it has
                                                 // no instrument of that number
    const Instrument* find(int program) const;
};

std::unique_ptr<Bank> loadDls(const std::string& path, std::string& error);

}  // namespace nds
