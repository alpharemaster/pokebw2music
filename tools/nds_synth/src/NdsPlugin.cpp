// NDS Synth's plugin (see NdsPlugin.hpp)
#include "NdsPlugin.hpp"

#include <algorithm>
#include <cstring>

START_NAMESPACE_DISTRHO

NdsPlugin::NdsPlugin() : Plugin(kParameterCount, 0, 1)
{
    fEngine.setSampleRate(getSampleRate());
}

std::string NdsPlugin::bankName() const
{
    std::lock_guard<std::mutex> lock(fLock);
    auto bank = fEngine.bank();
    return bank ? bank->name : std::string();
}

std::string NdsPlugin::instrumentName(int program) const
{
    std::lock_guard<std::mutex> lock(fLock);
    auto bank = fEngine.bank();
    if (!bank)
        return {};
    const nds::Instrument* instrument = bank->find(program);
    return instrument ? instrument->name : std::string("(none)");
}

std::vector<int> NdsPlugin::programs() const
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

void NdsPlugin::initParameter(uint32_t index, Parameter& p)
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

float NdsPlugin::getParameterValue(uint32_t index) const
{
    if (index >= kParameterProgram1)
        return float(fEngine.program(int(index - kParameterProgram1)));
    const nds::Settings& s = fEngine.settings;
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

void NdsPlugin::setParameterValue(uint32_t index, float value)
{
    if (index >= kParameterProgram1) {
        fEngine.setProgram(int(index - kParameterProgram1), int(value + 0.5f));
        return;
    }
    nds::Settings& s = fEngine.settings;
    switch (index) {
    case kParameterMasterVolume: s.masterVolume = value; break;
    case kParameterSongVolume: s.songVolume = value; break;
    case kParameterChannelPriority: s.channelPriority = value; break;
    case kParameterHardware: s.hardware = value > 0.5f; break;
    case kParameterVoiceLimit: s.voiceLimit = value > 0.5f; break;
    case kParameterAllChannels: s.allChannels = value > 0.5f; break;
    }
}

void NdsPlugin::initState(uint32_t, State& state)
{
    state.key = "dls";
    state.label = "DLS File";
    state.hints = kStateIsFilenamePath;
}

String NdsPlugin::getState(const char* key) const
{
    return std::strcmp(key, "dls") == 0 ? String(fPath.c_str()) : String();
}

void NdsPlugin::setState(const char* key, const char* value)
{
    if (std::strcmp(key, "dls") != 0)
        return;
    std::string error;
    std::shared_ptr<const nds::Bank> bank;
    if (value && *value)
        bank = nds::loadDls(value, error);
    std::lock_guard<std::mutex> lock(fLock);
    fPath = value ? value : "";
    fError = error;
    fEngine.setBank(std::move(bank));
}

void NdsPlugin::activate()
{
    std::lock_guard<std::mutex> lock(fLock);
    fEngine.setSampleRate(getSampleRate());
    fEngine.reset();
}

void NdsPlugin::sampleRateChanged(double rate)
{
    std::lock_guard<std::mutex> lock(fLock);
    fEngine.setSampleRate(rate);
}

void NdsPlugin::run(const float**, float** outputs, uint32_t frames, const MidiEvent* events, uint32_t count)
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
    // The events of a frame with its controllers and program changes first: the driver reads a note's settings when
    // it starts, and the host may give a setting at a note's time after the note
    auto isNote = [](const MidiEvent& e) {
        return e.size >= 1 && ((e.data[0] & 0xF0) == 0x90 || (e.data[0] & 0xF0) == 0x80);
    };
    uint32_t done = 0;
    for (uint32_t i = 0; i < count;) {
        uint32_t at = std::min(events[i].frame, frames);
        if (at > done) {
            fEngine.render(outputs[0] + done, outputs[1] + done, at - done);
            done = at;
        }
        uint32_t end = i;
        while (end < count && std::min(events[end].frame, frames) == at)
            end++;
        for (int pass = 0; pass < 2; pass++)
            for (uint32_t j = i; j < end; j++)
                if (isNote(events[j]) == (pass == 1))
                    fEngine.midi(events[j].size > MidiEvent::kDataSize ? events[j].dataExt : events[j].data,
                                 events[j].size);
        i = end;
    }
    if (frames > done)
        fEngine.render(outputs[0] + done, outputs[1] + done, frames - done);
}

Plugin* createPlugin() { return new NdsPlugin(); }

END_NAMESPACE_DISTRHO
