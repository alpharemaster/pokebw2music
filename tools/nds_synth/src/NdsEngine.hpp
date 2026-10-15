// NDS Synth: the DS sound driver, as Pokémon Black 2 and White 2's ARM7 runs it, playing MIDI.
//
// Each MIDI channel is a track of the driver, and the controllers are the commands that tools/data/sseq_midi.py
// exports them as (midi_sseq.CONTROLLERS):
//   0 bank select (program + 128 per step)    1 modulation depth    3 modulation type (0 pitch, 1 volume, 2 pan)
//   5 portamento time    7 volume    10 pan    11 expression    14 track priority    65 portamento on (>= 64)
//   72 release   73 attack   75 decay   79 sustain (the track's envelope over its instruments')
//   76 modulation speed    77 modulation range    78 modulation delay    84 portamento key (switches it on)
//   85 program (with the bank select: VST3 hosts may not pass program changes, so the exports write both)
//   102 and 103 pitch sweep (high and low 7 bits, plus 8192)    RPN 0 bend range    121 resets the controllers
// What the driver does with them, and with the notes, follows its code (addresses in the ROM's ARM7 binary):
// - every 2728 * 64 cycles (a frame, about 5.2 ms) each channel's envelope (0x03800ea0), modulation (0x03801418,
//   0x03801484), sweep and portamento (0x03800b38) move, and its timer (0x03800378), volume (0x0380049c) and pan are
//   set again;
// - a note takes a hardware channel (0x03800fc4): PCM any of the player's, PSG 8 to 13 and noise 14 and 15, in the
//   driver's order, else the one with the lowest priority, and is dropped if that one's is higher than its own;
// - a released note's priority drops to 1, and a channel is free once its release falls below -72.3 dB.
// The hardware output mixes the 16 channels at the DS's 32728.5 Hz without interpolation, with each channel's
// 7-bit volume and divider and the 10-bit output; the clean output plays the same channels at the host's rate,
// interpolated.
#pragma once

#include "NdsBank.hpp"

#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <vector>

namespace nds {

constexpr double SYSTEM_CLOCK = 33513982.0;
constexpr double ARM7_CLOCK = SYSTEM_CLOCK / 2;           // The sound timers' clock
constexpr double MIXER_RATE = SYSTEM_CLOCK / 1024;        // The mixer's 32728.5 Hz
constexpr double FRAME_SECONDS = 2728.0 * 64 / SYSTEM_CLOCK;
constexpr int SILENT = -723 * 128;                         // An envelope at -72.3 dB, in 1/1280 dB
constexpr int NO_ENVELOPE = 255;

int decibels(int value);                 // The driver's 0.1 dB of a 0-127 volume (0x03808e48)
uint16_t calcTimer(int timer, int pitch);  // The timer of a pitch in 1/64 semitones over a base timer (0x03800378)
double attackSeconds(int attack);        // The DLS export's times of the driver's envelope values
double fallSeconds(int value);

struct Track {
    int program = 0, bankSelect = 0;
    int volume = 127, expression = 127, pan = 0;  // Pan from -64 to 63
    int bend = 0, bendRange = 2;                  // Bend from -128 to 127
    int priority = 64;
    bool portamento = false;
    int portamentoKey = 60, portamentoTime = 0;
    int sweep = 0, sweepHigh = 64;
    int lfoType = 0, lfoSpeed = 16, lfoDepth = 0, lfoRange = 1, lfoDelay = 0;
    int envelope[4] = {NO_ENVELOPE, NO_ENVELOPE, NO_ENVELOPE, NO_ENVELOPE};
    int rpnHigh = 127, rpnLow = 127;
    // The length of the track's last note in ticks, which a portamento time of 0 slides over, as a note's own
    // length isn't known when it starts
    double lastLength = 48, noteStart = 0;
};

enum class EnvelopeState { Attack, Decay, Sustain, Release };

struct Voice {
    bool active = false;
    int channel = 0;          // The track (MIDI channel)
    int hardware = -1;        // The DS channel, with the voice limit
    int key = 60, velocity = 127;
    int priority = 0;
    uint64_t order = 0;
    const Region* region = nullptr;
    const Sample* sample = nullptr;
    // The envelope (0x03800ea0)
    EnvelopeState state = EnvelopeState::Attack;
    int level = SILENT;
    int attackRate = 0, decayRate = 0, sustain = 0, releaseRate = 0;
    // The sweep and portamento
    int sweep = 0, sweepLength = 0, sweepCount = 0;
    bool sweepByTick = false;
    // The modulation's counters
    int lfoDelayCount = 0, lfoCount = 0;
    // What the last frame set: the timer, the volume (7 bits and divider), the pan
    uint16_t timer = 0x1F46;
    int volume = 0, divider = 0, pan = 64;
    double gain = 0;          // The clean output's volume
    // Playback
    double position = 0;      // Samples, or the PSG step, into the wave
    int psgStep = 0;
    uint16_t noise = 0x7FFF;
    int16_t noiseOut = 0x7FFF;
};

struct Settings {
    float masterVolume = 127;
    float songVolume = 90;       // The sequence's volume (its JSON's "volume"), the BGMs' median by default
    float channelPriority = 64;  // The sequence's channel priority
    bool hardware = true;        // The DS mixer's output
    bool voiceLimit = true;      // The 16 hardware channels
    bool allChannels = false;    // All 16 channels, or PLAYER_BGM's (1 to 13 and 15)
};

// What the UI shows of a channel
struct ChannelView {
    int program = 0, volume = 127, expression = 127, pan = 64, bend = 0, bendRange = 2;
    int portamento = 0, portamentoTime = 0, lfoDepth = 0, lfoType = 0, voices = 0;
};

class Engine {
public:
    Engine();
    void setSampleRate(double rate);
    void setBank(std::shared_ptr<const Bank> bank);
    std::shared_ptr<const Bank> bank() const { return fBank; }
    void setTempo(double bpm) { fTempo = bpm > 1 ? bpm : 120; }
    Settings settings;

    void midi(const uint8_t* data, uint32_t size);
    void render(float* left, float* right, uint32_t frames);
    void reset();

    std::array<ChannelView, 16> view() const;
    int program(int channel) const { return fTracks[channel & 15].program; }
    void setProgram(int channel, int program) { fTracks[channel & 15].program = program; }
    int activeVoices() const;

private:
    void noteOn(int channel, int key, int velocity);
    void noteOff(int channel, int key);
    void controller(int channel, int number, int value);
    Voice* allocate(WaveType type, int priority);
    void frame();
    void tick();
    void updateVoice(Voice& voice);
    void mixHardware(int32_t& left, int32_t& right);
    void mixClean(float& left, float& right, double step);

    std::shared_ptr<const Bank> fBank;
    std::array<Track, 16> fTracks{};
    std::vector<Voice> fVoices;
    double fRate = 48000;
    double fTempo = 120;
    double fTempoCounter = 240;
    double fTicks = 0;
    uint64_t fOrder = 0;
    // Clocks
    double fFrameClock = 0;      // Output samples until the next frame
    double fMixerPhase = 0;      // The hardware output's position between its last two mixer samples
    float fLast[2] = {0, 0}, fNext[2] = {0, 0};
    int fMixerUntilFrame = 0;    // Mixer samples' cycles (1024 each) to the next frame
};

}  // namespace nds
