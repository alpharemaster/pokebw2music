// Nitro Synth: the DS sound driver (see NitroEngine.hpp)
#include "NitroEngine.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>

namespace nitro {

namespace {

// The driver's attack table (0x03808f5c)
const uint8_t ATTACK_TABLE[19] = {0x00, 0x01, 0x05, 0x0E, 0x1A, 0x26, 0x33, 0x3F, 0x49, 0x54,
                                  0x5C, 0x64, 0x6D, 0x74, 0x7B, 0x7F, 0x84, 0x89, 0x8F};
// The modulation's quarter sine (0x03808e24)
const int8_t SINE[33] = {0, 6, 12, 19, 25, 31, 37, 43, 49, 54, 60, 65, 71, 76, 81, 85, 90,
                         94, 98, 102, 106, 109, 112, 115, 117, 120, 122, 123, 125, 126, 126, 127, 127};
// The hardware channels in the order the driver takes them (0x03808f4c), and the ones each wave type may use
const int CHANNEL_ORDER[16] = {4, 5, 6, 7, 2, 0, 3, 1, 8, 9, 10, 11, 14, 12, 15, 13};
constexpr uint16_t PCM_CHANNELS = 0xFFFF, PSG_CHANNELS = 0x3F00, NOISE_CHANNELS = 0xC000;
constexpr uint16_t BGM_CHANNELS = 0xBFFE;  // PLAYER_BGM's in data/sound/archive.json
// The hardware's volume dividers, as shifts
const int DIVIDER_SHIFT[4] = {0, 1, 2, 4};
constexpr int FRAME_CYCLES = 2728 * 64;
constexpr int MAX_VOICES = 64;

int attackRate(int attack) { return attack >= 109 ? ATTACK_TABLE[127 - attack] : 255 - attack; }

int fallRate(int value)
{
    if (value >= 127)
        return 0xFFFF;
    if (value == 126)
        return 0x3C00;
    if (value < 50)
        return value * 2 + 1;
    return 0x1E00 / (126 - value);
}

int sine(int index)
{
    if (index < 32)
        return SINE[index];
    if (index < 64)
        return SINE[64 - index];
    if (index < 96)
        return -SINE[index - 64];
    return -SINE[128 - index];
}

// The BIOS's pitch table (GetPitchTable), which isn't in the ROM: 2 ** (i / 768) in 16-bit fractions
struct PitchTable {
    uint16_t values[768];
    PitchTable()
    {
        for (int i = 0; i < 768; i++)
            values[i] = uint16_t(std::lround(65536.0 * (std::pow(2.0, i / 768.0) - 1)));
    }
};
const PitchTable PITCH;

// The BIOS's volume table (GetVolumeTable): the 7-bit volume of an attenuation from -72.3 dB, for the divider that
// the driver picks for it (0x0380049c)
struct VolumeTable {
    uint8_t values[724];
    VolumeTable()
    {
        for (int i = 0; i < 724; i++) {
            int db = i - 723;
            int shift = db < -240 ? 3 : db < -120 ? 2 : db < -60 ? 1 : 0;
            double volume = 127.0 * std::pow(10.0, db / 200.0) * (1 << DIVIDER_SHIFT[shift]);
            values[i] = uint8_t(std::min(127L, std::lround(volume)));
        }
    }
};
const VolumeTable VOLUME;

}  // namespace

int decibels(int value)
{
    if (value <= 0)
        return -32768;
    return std::max(-722, int(std::lround(400 * std::log10(std::min(127, value) / 127.0))));
}

uint16_t calcTimer(int timer, int pitch)
{
    int octave = 0;
    int p = -pitch;
    while (p < 0) {
        octave--;
        p += 768;
    }
    while (p >= 768) {
        octave++;
        p -= 768;
    }
    uint64_t result = (uint64_t(PITCH.values[p]) + 0x10000) * uint64_t(timer);
    int shift = octave - 16;
    if (shift <= 0) {
        result >>= -shift;
    } else if (shift < 32) {
        if (result & ~(~0ull >> shift))
            return 0xFFFF;
        result <<= shift;
    } else {
        return 0xFFFF;
    }
    return uint16_t(std::clamp<uint64_t>(result, 0x10, 0xFFFF));
}

double attackSeconds(int attack)
{
    int rate = attackRate(attack);
    if (rate == 0)
        return 0;
    int level = SILENT, frames = 0;
    while (level < -6.02 * 1280) {
        level = -((-level * rate) >> 8);
        frames++;
    }
    return 2 * frames * FRAME_SECONDS;
}

double fallSeconds(int value) { return 96.0 * 1280 / fallRate(value) * FRAME_SECONDS; }

Engine::Engine() : fVoices(MAX_VOICES) { fMixerUntilFrame = FRAME_CYCLES; }

void Engine::setSampleRate(double rate)
{
    fRate = rate > 0 ? rate : 48000;
    fFrameClock = 0;
}

void Engine::setBank(std::shared_ptr<const Bank> bank)
{
    for (Voice& voice : fVoices)
        voice.active = false;
    fBank = std::move(bank);
}

void Engine::reset()
{
    for (Voice& voice : fVoices)
        voice.active = false;
    fTracks = {};
}

int Engine::activeVoices() const
{
    return int(std::count_if(fVoices.begin(), fVoices.end(), [](const Voice& v) { return v.active; }));
}

std::array<ChannelView, 16> Engine::view() const
{
    std::array<ChannelView, 16> out{};
    for (int i = 0; i < 16; i++) {
        const Track& t = fTracks[i];
        out[i] = {t.program, t.volume, t.expression, t.pan + 64, t.bend, t.bendRange,
                  t.portamento ? 1 : 0, t.portamentoTime, t.lfoDepth, t.lfoType, 0};
    }
    for (const Voice& voice : fVoices)
        if (voice.active)
            out[voice.channel].voices++;
    return out;
}

// MIDI

void Engine::midi(const uint8_t* data, uint32_t size)
{
    if (size < 1)
        return;
    int status = data[0] & 0xF0, channel = data[0] & 0x0F;
    int a = size > 1 ? data[1] & 0x7F : 0, b = size > 2 ? data[2] & 0x7F : 0;
    switch (status) {
    case 0x80:
        noteOff(channel, a);
        break;
    case 0x90:
        if (b)
            noteOn(channel, a, b);
        else
            noteOff(channel, a);
        break;
    case 0xB0:
        controller(channel, a, b);
        break;
    case 0xC0:
        fTracks[channel].program = fTracks[channel].bankSelect * 128 + a;
        break;
    case 0xE0:
        fTracks[channel].bend = std::clamp(((b << 7 | a) >> 6) - 128, -128, 127);
        break;
    }
}

void Engine::controller(int channel, int number, int value)
{
    Track& t = fTracks[channel];
    switch (number) {
    case 0: t.bankSelect = value; break;
    case 1: t.lfoDepth = value; break;
    case 3: t.lfoType = value; break;
    case 5: t.portamentoTime = value; break;
    case 6:
        if (t.rpnHigh == 0 && t.rpnLow == 0)
            t.bendRange = value;
        break;
    case 7: t.volume = value; break;
    case 10: t.pan = value - 64; break;
    case 11: t.expression = value; break;
    case 14: t.priority = value; break;
    case 65: t.portamento = value >= 64; break;
    case 72: t.envelope[3] = value; break;
    case 73: t.envelope[0] = value; break;
    case 75: t.envelope[1] = value; break;
    case 79: t.envelope[2] = value; break;
    case 76: t.lfoSpeed = value; break;
    case 77: t.lfoRange = value; break;
    case 78: t.lfoDelay = value; break;
    case 84:  // The driver's portamento command sets the key and switches it on
        t.portamentoKey = value;
        t.portamento = true;
        break;
    case 100: t.rpnLow = value; break;
    case 101: t.rpnHigh = value; break;
    case 102: t.sweepHigh = value; break;
    case 103: t.sweep = (t.sweepHigh << 7 | value) - 8192; break;
    case 120:  // All sound off
        for (Voice& voice : fVoices)
            if (voice.active && voice.channel == channel)
                voice.active = false;
        break;
    case 121: {  // Reset the controllers to a new track's
        Track fresh;
        fresh.program = t.program;
        fresh.bankSelect = t.bankSelect;
        fresh.volume = t.volume;
        fresh.pan = t.pan;
        fresh.lastLength = t.lastLength;
        t = fresh;
        break;
    }
    case 123:  // All notes off
        for (Voice& voice : fVoices)
            if (voice.active && voice.channel == channel && voice.state != EnvelopeState::Release) {
                voice.state = EnvelopeState::Release;
                voice.priority = 1;
            }
        break;
    }
}

Voice* Engine::allocate(WaveType type, int priority)
{
    if (!settings.voiceLimit) {
        Voice* best = nullptr;
        for (Voice& voice : fVoices) {
            if (!voice.active)
                return &voice;
            // Full: the oldest released note, else the oldest
            if (!best || (voice.priority < best->priority) ||
                (voice.priority == best->priority && voice.order < best->order))
                best = &voice;
        }
        return best;
    }
    // The driver's allocation (0x03800fc4)
    uint16_t mask = settings.allChannels ? 0xFFFF : BGM_CHANNELS;
    mask &= type == WaveType::Psg ? PSG_CHANNELS : type == WaveType::Noise ? NOISE_CHANNELS : PCM_CHANNELS;
    Voice* onChannel[16] = {};
    for (Voice& voice : fVoices)
        if (voice.active && voice.hardware >= 0)
            onChannel[voice.hardware] = &voice;
    int best = -1, bestPriority = 0, bestLoudness = 0;
    for (int channel : CHANNEL_ORDER) {
        if (!(mask >> channel & 1))
            continue;
        Voice* voice = onChannel[channel];
        int p = voice ? voice->priority : 0;
        int loudness = voice ? (voice->volume << 4) >> DIVIDER_SHIFT[voice->divider] : 0;
        if (best < 0 || p < bestPriority || (p == bestPriority && loudness < bestLoudness)) {
            best = channel;
            bestPriority = p;
            bestLoudness = loudness;
        }
    }
    if (best < 0 || priority < bestPriority)
        return nullptr;
    if (onChannel[best]) {
        onChannel[best]->hardware = best;
        return onChannel[best];
    }
    for (Voice& voice : fVoices)
        if (!voice.active) {
            voice.hardware = best;
            return &voice;
        }
    return nullptr;
}

void Engine::noteOn(int channel, int key, int velocity)
{
    if (!fBank)
        return;
    Track& t = fTracks[channel];
    const Instrument* instrument = fBank->find(t.program);
    const Region* region = instrument ? instrument->find(key) : nullptr;
    if (!region)
        return;
    const Sample* sample = nullptr;
    if (region->type == WaveType::Pcm) {
        if (region->sample < 0 || region->sample >= int(fBank->samples.size()))
            return;
        sample = &fBank->samples[region->sample];
        if (sample->data.empty())
            return;
    }
    int priority = std::clamp(int(settings.channelPriority) + t.priority, 0, 255);
    Voice* voice = allocate(region->type, priority);
    if (!voice)
        return;
    int hardware = voice->hardware;
    *voice = Voice();
    voice->hardware = settings.voiceLimit ? hardware : -1;
    voice->active = true;
    voice->channel = channel;
    voice->key = key;
    voice->velocity = velocity;
    voice->priority = priority;
    voice->order = ++fOrder;
    voice->region = region;
    voice->sample = sample;
    // The instrument's envelope, or the track's where it sets one
    auto pick = [&](int index, int own) { return t.envelope[index] != NO_ENVELOPE ? t.envelope[index] : own; };
    voice->attackRate = attackRate(pick(0, region->attack));
    voice->decayRate = fallRate(pick(1, region->decay));
    voice->sustain = pick(2, region->sustain);
    voice->releaseRate = fallRate(pick(3, region->release));
    // The sweep, with the portamento from the last key (0x03802620)
    voice->sweep = t.sweep;
    if (t.portamento)
        voice->sweep += (t.portamentoKey - key) * 64;
    if (t.portamentoTime == 0) {
        voice->sweepLength = std::max(1, int(std::lround(t.lastLength)));
        voice->sweepByTick = true;
    } else {
        voice->sweepLength = (t.portamentoTime * t.portamentoTime * std::abs(voice->sweep)) >> 11;
    }
    t.portamentoKey = key;
    t.noteStart = fTicks + fTempoCounter / 240;
    // The driver updates a channel in the frame that starts it
    updateVoice(*voice);
}

void Engine::noteOff(int channel, int key)
{
    Voice* oldest = nullptr;
    for (Voice& voice : fVoices)
        if (voice.active && voice.channel == channel && voice.key == key && voice.state != EnvelopeState::Release &&
            (!oldest || voice.order < oldest->order))
            oldest = &voice;
    if (oldest) {
        oldest->state = EnvelopeState::Release;
        oldest->priority = 1;
        Track& t = fTracks[channel];
        t.lastLength = std::max(1.0, fTicks + fTempoCounter / 240 - t.noteStart);
    }
}

// The driver's frames and ticks

void Engine::frame()
{
    while (fTempoCounter >= 240) {
        fTempoCounter -= 240;
        tick();
    }
    fTempoCounter += fTempo;
    for (Voice& voice : fVoices)
        if (voice.active)
            updateVoice(voice);
}

void Engine::tick()
{
    fTicks += 1;
    for (Voice& voice : fVoices)
        if (voice.active && voice.sweepByTick && voice.sweepCount < voice.sweepLength)
            voice.sweepCount++;
}

void Engine::updateVoice(Voice& v)
{
    const Track& t = fTracks[v.channel];
    // The envelope (0x03800ea0)
    switch (v.state) {
    case EnvelopeState::Attack:
        v.level = -((-v.level * v.attackRate) >> 8);
        if (v.level == 0)
            v.state = EnvelopeState::Decay;
        break;
    case EnvelopeState::Decay: {
        int sustain = decibels(v.sustain) * 128;
        v.level -= v.decayRate;
        if (v.level <= sustain) {
            v.level = sustain;
            v.state = EnvelopeState::Sustain;
        }
        break;
    }
    case EnvelopeState::Sustain:
        break;
    case EnvelopeState::Release:
        v.level -= v.releaseRate;
        break;
    }
    int volume = decibels(v.velocity) + decibels(t.volume) + decibels(t.expression) +
                 decibels(int(settings.songVolume)) + (v.level >> 7);
    int pan = (v.region->pan - 64) + t.pan;
    int pitch = (v.key - v.region->baseKey) * 64 + ((t.bend * (t.bendRange << 6)) >> 7);
    // The sweep and portamento (0x03800b38)
    if (v.sweep && v.sweepCount < v.sweepLength) {
        int64_t offset = int64_t(v.sweep) * (v.sweepLength - v.sweepCount) / v.sweepLength;
        pitch += int(offset);
        if (!v.sweepByTick)
            v.sweepCount++;
    }
    // The modulation (0x03801484, then 0x03801418)
    if (t.lfoDepth && v.lfoDelayCount >= t.lfoDelay) {
        int64_t value = int64_t(sine(v.lfoCount >> 8)) * t.lfoDepth * t.lfoRange;
        if (t.lfoType == 0)
            pitch += int((value << 6) >> 14);
        else if (t.lfoType == 1 && volume > -32768)
            volume += int((value * 60) >> 14);
        else if (t.lfoType == 2)
            pan += int((value << 6) >> 14);
    }
    if (v.lfoDelayCount < t.lfoDelay) {
        v.lfoDelayCount++;
    } else {
        int position = v.lfoCount + (t.lfoSpeed << 6);
        int phase = position >> 8;
        while (phase >= 128)
            phase -= 128;
        v.lfoCount = (position & 0xFF) | phase << 8;
    }
    // A released note ends once it is silent
    if (v.state == EnvelopeState::Release && volume <= -723) {
        v.active = false;
        return;
    }
    v.timer = calcTimer(v.region->timer, pitch);
    if (v.region->type == WaveType::Psg)
        v.timer &= 0xFFFC;
    // The volume and its divider (0x0380049c)
    int db = std::clamp(volume, -723, 0);
    v.divider = db < -240 ? 3 : db < -120 ? 2 : db < -60 ? 1 : 0;
    v.volume = volume <= -723 ? 0 : VOLUME.values[db + 723];
    v.gain = volume <= -723 ? 0.0 : std::pow(10.0, db / 200.0);
    v.pan = std::clamp(pan + 64, 0, 127);
}

// Output

namespace {

// The wave's sample at the voice's position, which moves on by a step; false at the end of an unlooped wave
inline bool fetch(Voice& v, double step, bool interpolate, float& out)
{
    switch (v.region->type) {
    case WaveType::Pcm: {
        const std::vector<int16_t>& data = v.sample->data;
        double size = double(data.size());
        if (v.position >= size) {
            if (!v.sample->looped || v.sample->loopStart >= data.size())
                return false;
            double length = size - v.sample->loopStart;
            v.position = v.sample->loopStart + std::fmod(v.position - size, length);
        }
        size_t index = size_t(v.position);
        float value = data[index];
        if (interpolate) {
            size_t next = index + 1;
            if (next >= data.size())
                next = v.sample->looped ? v.sample->loopStart : index;
            value += float(v.position - double(index)) * (data[next] - value);
        }
        out = value;
        v.position += step;
        return true;
    }
    case WaveType::Psg: {
        int stepIndex = int(v.position) & 7;
        out = stepIndex > 6 - std::min<int>(v.region->duty, 6) ? 32767.0f : -32767.0f;
        v.position = std::fmod(v.position + step, 8.0);
        return true;
    }
    case WaveType::Noise: {
        out = v.noiseOut;
        double next = v.position + step;
        for (int n = int(next) - int(v.position); n > 0; n--) {
            if (v.noise & 1) {
                v.noise = uint16_t((v.noise >> 1) ^ 0x6000);
                v.noiseOut = -0x7FFF;
            } else {
                v.noise >>= 1;
                v.noiseOut = 0x7FFF;
            }
        }
        v.position = std::fmod(next, 65536.0);
        return true;
    }
    }
    return false;
}

}  // namespace

void Engine::mixHardware(int32_t& left, int32_t& right)
{
    left = right = 0;
    for (Voice& v : fVoices) {
        if (!v.active)
            continue;
        float sample;
        // A channel steps once each timer period of the 16.76 MHz clock: 512 periods' worth per mixer sample
        if (!fetch(v, 512.0 / v.timer, false, sample)) {
            v.active = false;
            continue;
        }
        int32_t x = ((int32_t(sample) * v.volume) >> 7) >> DIVIDER_SHIFT[v.divider];
        left += (x * (128 - v.pan)) >> 7;
        right += (x * v.pan) >> 7;
    }
}

void Engine::mixClean(float& left, float& right, double rate)
{
    left = right = 0;
    for (Voice& v : fVoices) {
        if (!v.active)
            continue;
        float sample;
        if (!fetch(v, ARM7_CLOCK / v.timer / rate, true, sample)) {
            v.active = false;
            continue;
        }
        float x = sample * float(v.gain) * (127.0f / 128.0f);
        left += x * (128 - v.pan) / 128.0f;
        right += x * v.pan / 128.0f;
    }
}

void Engine::render(float* left, float* right, uint32_t frames)
{
    float master = std::clamp(settings.masterVolume, 0.0f, 127.0f);
    for (uint32_t i = 0; i < frames; i++) {
        float l, r;
        if (settings.hardware) {
            fMixerPhase += MIXER_RATE / fRate;
            while (fMixerPhase >= 1) {
                fMixerPhase -= 1;
                fMixerUntilFrame -= 1024;
                if (fMixerUntilFrame <= 0) {
                    frame();
                    fMixerUntilFrame += FRAME_CYCLES;
                }
                int32_t ml, mr;
                mixHardware(ml, mr);
                // The master volume, the 16-bit clamp and the 10-bit output
                int32_t m = int32_t(master);
                ml = std::clamp((ml * m) >> 7, -32768, 32767) & ~0x3F;
                mr = std::clamp((mr * m) >> 7, -32768, 32767) & ~0x3F;
                fLast[0] = fNext[0];
                fLast[1] = fNext[1];
                fNext[0] = ml / 32768.0f;
                fNext[1] = mr / 32768.0f;
            }
            float phase = float(fMixerPhase);
            l = fLast[0] + (fNext[0] - fLast[0]) * phase;
            r = fLast[1] + (fNext[1] - fLast[1]) * phase;
        } else {
            fFrameClock -= 1;
            if (fFrameClock <= 0) {
                frame();
                fFrameClock += FRAME_SECONDS * fRate;
            }
            mixClean(l, r, fRate);
            l = l * master / 127.0f / 32768.0f;
            r = r * master / 127.0f / 32768.0f;
        }
        left[i] = l;
        right[i] = r;
    }
}

}  // namespace nitro
