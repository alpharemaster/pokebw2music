// Nitro Synth's plugin (see NitroPlugin.hpp)
#include "NitroPlugin.hpp"

#include <algorithm>
#include <cstring>

START_NAMESPACE_DISTRHO

NitroPlugin::NitroPlugin() : Plugin(kParameterCount, 0, 1)
{
    fEngine.setSampleRate(getSampleRate());
}

std::string NitroPlugin::bankName() const
{
    std::lock_guard<std::mutex> lock(fLock);
    auto bank = fEngine.bank();
    return bank ? bank->name : std::string();
}

std::string NitroPlugin::instrumentName(int program) const
{
    std::lock_guard<std::mutex> lock(fLock);
    auto bank = fEngine.bank();
    if (!bank)
        return {};
    const nitro::Instrument* instrument = bank->find(program);
    return instrument ? instrument->name : std::string("(none)");
}

std::vector<int> NitroPlugin::programs() const
{
    std::lock_guard<std::mutex> lock(fLock);
    std::vector<int> out;
    if (auto bank = fEngine.bank()) {
        for (const auto& item : bank->instruments)
            out.push_back(int(item.first));
        for (const auto& item : bank->drums)
            if (!bank->instruments.count(item.first))
                out.push_back(int(item.first));
        std::sort(out.begin(), out.end());
    }
    return out;
}

void NitroPlugin::initParameter(uint32_t index, Parameter& p)
{
    p.hints = kParameterIsAutomatable;
    if (index >= kParameterProgram1) {
        const int channel = int(index - kParameterProgram1) + 1;
        p.name = String("Program ") + String(channel);
        p.symbol = String("program_") + String(channel);
        p.hints |= kParameterIsInteger;
        p.ranges = ParameterRanges(0, 0, 16383);
        return;
    }
    switch (index) {
    case kParameterMasterVolume:
        p.name = "Master Volume";
        p.symbol = "master";
        p.hints |= kParameterIsInteger;
        p.ranges = ParameterRanges(127, 0, 127);
        break;
    case kParameterSongVolume:
        p.name = "Song Volume";
        p.symbol = "song_volume";
        p.hints |= kParameterIsInteger;
        p.ranges = ParameterRanges(90, 0, 127);
        break;
    case kParameterChannelPriority:
        p.name = "Channel Priority";
        p.symbol = "channel_priority";
        p.hints |= kParameterIsInteger;
        p.ranges = ParameterRanges(64, 0, 127);
        break;
    case kParameterHardware:
        p.name = "Hardware Output";
        p.symbol = "hardware";
        p.hints |= kParameterIsBoolean | kParameterIsInteger;
        p.ranges = ParameterRanges(1, 0, 1);
        break;
    case kParameterVoiceLimit:
        p.name = "16 Channel Limit";
        p.symbol = "voice_limit";
        p.hints |= kParameterIsBoolean | kParameterIsInteger;
        p.ranges = ParameterRanges(1, 0, 1);
        break;
    case kParameterAllChannels:
        p.name = "All 16 Channels";
        p.symbol = "all_channels";
        p.hints |= kParameterIsBoolean | kParameterIsInteger;
        p.ranges = ParameterRanges(0, 0, 1);
        break;
    }
}

float NitroPlugin::getParameterValue(uint32_t index) const
{
    if (index >= kParameterProgram1)
        return float(fEngine.program(int(index - kParameterProgram1)));
    const nitro::Settings& s = fEngine.settings;
    switch (index) {
    case kParameterMasterVolume: return s.masterVolume;
    case kParameterSongVolume: return s.songVolume;
    case kParameterChannelPriority: return s.channelPriority;
    case kParameterHardware: return s.hardware ? 1 : 0;
    case kParameterVoiceLimit: return s.voiceLimit ? 1 : 0;
    case kParameterAllChannels: return s.allChannels ? 1 : 0;
    }
    return 0;
}

void NitroPlugin::setParameterValue(uint32_t index, float value)
{
    if (index >= kParameterProgram1) {
        fEngine.setProgram(int(index - kParameterProgram1), int(value + 0.5f));
        return;
    }
    nitro::Settings& s = fEngine.settings;
    switch (index) {
    case kParameterMasterVolume: s.masterVolume = value; break;
    case kParameterSongVolume: s.songVolume = value; break;
    case kParameterChannelPriority: s.channelPriority = value; break;
    case kParameterHardware: s.hardware = value > 0.5f; break;
    case kParameterVoiceLimit: s.voiceLimit = value > 0.5f; break;
    case kParameterAllChannels: s.allChannels = value > 0.5f; break;
    }
}

void NitroPlugin::initState(uint32_t, State& state)
{
    state.key = "dls";
    state.label = "DLS File";
    state.hints = kStateIsFilenamePath;
}

String NitroPlugin::getState(const char* key) const
{
    return std::strcmp(key, "dls") == 0 ? String(fPath.c_str()) : String();
}

void NitroPlugin::setState(const char* key, const char* value)
{
    if (std::strcmp(key, "dls") != 0)
        return;
    std::string error;
    std::shared_ptr<const nitro::Bank> bank;
    if (value && *value)
        bank = nitro::loadDls(value, error);
    std::lock_guard<std::mutex> lock(fLock);
    fPath = value ? value : "";
    fError = error;
    fEngine.setBank(std::move(bank));
}

void NitroPlugin::activate()
{
    std::lock_guard<std::mutex> lock(fLock);
    fEngine.setSampleRate(getSampleRate());
    fEngine.reset();
}

void NitroPlugin::sampleRateChanged(double rate)
{
    std::lock_guard<std::mutex> lock(fLock);
    fEngine.setSampleRate(rate);
}

void NitroPlugin::run(const float**, float** outputs, uint32_t frames, const MidiEvent* events, uint32_t count)
{
    std::unique_lock<std::mutex> lock(fLock, std::try_to_lock);
    if (!lock.owns_lock()) {
        // A DLS file is loading
        std::memset(outputs[0], 0, sizeof(float) * frames);
        std::memset(outputs[1], 0, sizeof(float) * frames);
        return;
    }
    const TimePosition& time = getTimePosition();
    if (time.bbt.valid)
        fEngine.setTempo(time.bbt.beatsPerMinute);
    uint32_t done = 0;
    for (uint32_t i = 0; i < count; i++) {
        const MidiEvent& event = events[i];
        uint32_t at = std::min(event.frame, frames);
        if (at > done) {
            fEngine.render(outputs[0] + done, outputs[1] + done, at - done);
            done = at;
        }
        fEngine.midi(event.size > MidiEvent::kDataSize ? event.dataExt : event.data, event.size);
    }
    if (frames > done)
        fEngine.render(outputs[0] + done, outputs[1] + done, frames - done);
}

Plugin* createPlugin() { return new NitroPlugin(); }

END_NAMESPACE_DISTRHO
