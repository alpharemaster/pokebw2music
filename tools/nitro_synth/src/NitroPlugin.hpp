// Nitro Synth's plugin: the engine (NitroEngine.hpp) as a DPF instrument, with the DLS file it plays as its state
#pragma once

#include "DistrhoPlugin.hpp"
#include "NitroEngine.hpp"

#include <mutex>
#include <string>

START_NAMESPACE_DISTRHO

class NitroPlugin : public Plugin {
public:
    NitroPlugin();

    // What the UI reads directly
    std::array<nitro::ChannelView, 16> channelView() const { return fEngine.view(); }
    std::string bankName() const;
    std::string instrumentName(int program) const;
    std::string dlsPath() const { return fPath; }
    std::string loadError() const { return fError; }

protected:
    const char* getLabel() const override { return "NitroSynth"; }
    const char* getDescription() const override
    {
        return "Plays the DS sound banks of Pokemon Black 2 and White 2, exported as DLS files, as the game's sound "
               "driver does: its envelopes, portamento, modulation, pitch sweep and 16 hardware channels.";
    }
    const char* getMaker() const override { return "pokebw2"; }
    const char* getHomePage() const override { return DISTRHO_PLUGIN_URI; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }

    void initParameter(uint32_t index, Parameter& parameter) override;
    float getParameterValue(uint32_t index) const override;
    void setParameterValue(uint32_t index, float value) override;
    void initState(uint32_t index, State& state) override;
    String getState(const char* key) const override;
    void setState(const char* key, const char* value) override;

    void activate() override;
    void sampleRateChanged(double rate) override;
    void run(const float**, float** outputs, uint32_t frames, const MidiEvent* events, uint32_t count) override;

private:
    nitro::Engine fEngine;
    mutable std::mutex fLock;  // Held while a DLS file replaces the bank
    std::string fPath, fError;

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(NitroPlugin)
};

END_NAMESPACE_DISTRHO
