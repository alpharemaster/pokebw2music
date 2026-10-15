// Nitro Synth's description for DPF (the DISTRHO Plugin Framework)
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND "pokebw2"
#define DISTRHO_PLUGIN_NAME  "Nitro Synth"
#define DISTRHO_PLUGIN_URI   "https://github.com/alpharemaster/pokebw2music#nitro-synth"
#define DISTRHO_PLUGIN_BRAND_ID Pkbw
#define DISTRHO_PLUGIN_UNIQUE_ID NtSy

#define DISTRHO_PLUGIN_HAS_UI              1
#define DISTRHO_PLUGIN_IS_RT_SAFE          1
#define DISTRHO_PLUGIN_IS_SYNTH            1
#define DISTRHO_PLUGIN_NUM_INPUTS          0
#define DISTRHO_PLUGIN_NUM_OUTPUTS         2
#define DISTRHO_PLUGIN_WANT_STATE          1
#define DISTRHO_PLUGIN_WANT_FULL_STATE     1
#define DISTRHO_PLUGIN_WANT_TIMEPOS        1
#define DISTRHO_PLUGIN_WANT_DIRECT_ACCESS  1
#define DISTRHO_PLUGIN_VST3_CATEGORIES     "Instrument|Synth|Sampler"
#define DISTRHO_UI_USE_NANOVG              1
#define DISTRHO_UI_FILE_BROWSER            1
#define DISTRHO_UI_USER_RESIZABLE          0
#define DISTRHO_UI_DEFAULT_WIDTH           760
#define DISTRHO_UI_DEFAULT_HEIGHT          520

enum Parameters {
    kParameterMasterVolume = 0,
    kParameterSongVolume,
    kParameterChannelPriority,
    kParameterHardware,
    kParameterVoiceLimit,
    kParameterAllChannels,
    kParameterProgram1,  // Each channel's program, which program changes set too
    kParameterCount = kParameterProgram1 + 16
};

#endif  // DISTRHO_PLUGIN_INFO_H_INCLUDED
