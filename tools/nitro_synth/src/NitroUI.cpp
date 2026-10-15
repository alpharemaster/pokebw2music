// Nitro Synth's UI: the DLS file, the output settings, and what each of the 16 channels plays, as Fruity LSD lays
// out a DLS player's channels
#include "DistrhoUI.hpp"
#include "NitroPlugin.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

START_NAMESPACE_DISTRHO

namespace {

constexpr float W = DISTRHO_UI_DEFAULT_WIDTH, H = DISTRHO_UI_DEFAULT_HEIGHT;
constexpr float ROW = 21, TABLE_Y = 150;

struct Box {
    float x, y, w, h;
    bool contains(double px, double py) const { return px >= x && px < x + w && py >= y && py < y + h; }
};

const Box LOAD_BUTTON{600, 14, 144, 30};
const Box TOGGLES[3] = {{16, 66, 150, 26}, {176, 66, 150, 26}, {336, 66, 150, 26}};
const uint32_t TOGGLE_PARAMETERS[3] = {kParameterHardware, kParameterVoiceLimit, kParameterAllChannels};
const char* const TOGGLE_LABELS[3] = {"Hardware output", "16 channel limit", "All 16 channels"};
const Box SLIDERS[3] = {{16, 104, 230, 26}, {262, 104, 230, 26}, {508, 104, 236, 26}};
const uint32_t SLIDER_PARAMETERS[3] = {kParameterMasterVolume, kParameterSongVolume, kParameterChannelPriority};
const char* const SLIDER_LABELS[3] = {"Master", "Song volume", "Channel priority"};
const float COLUMNS[] = {16, 48, 104, 352, 398, 444, 498, 560, 640, 700};
const char* const HEADERS[] = {"Ch", "Prog", "Instrument", "Vol", "Exp", "Pan", "Bend", "Porta", "Mod", "Voices"};

}  // namespace

class NitroUI : public UI {
public:
    NitroUI() : UI(DISTRHO_UI_DEFAULT_WIDTH, DISTRHO_UI_DEFAULT_HEIGHT)
    {
        loadSharedResources();
        const double scale = getScaleFactor();
        if (scale != 1.0)
            setSize(uint(W * scale), uint(H * scale));
        for (uint32_t i = 0; i < kParameterCount; i++)
            fValues[i] = 0;
        fValues[kParameterMasterVolume] = 127;
        fValues[kParameterSongVolume] = 90;
        fValues[kParameterChannelPriority] = 64;
        fValues[kParameterHardware] = 1;
        fValues[kParameterVoiceLimit] = 1;
    }

protected:
    void parameterChanged(uint32_t index, float value) override
    {
        if (index < kParameterCount) {
            fValues[index] = value;
            repaint();
        }
    }

    void stateChanged(const char*, const char*) override { repaint(); }

    void uiIdle() override
    {
        // The channels change with the music
        if (NitroPlugin* plugin = this->plugin()) {
            auto view = plugin->channelView();
            if (std::memcmp(&view, &fView, sizeof view) != 0) {
                fView = view;
                repaint();
            }
        }
    }

    void uiFileBrowserSelected(const char* filename) override
    {
        if (filename && *filename) {
            setState("dls", filename);
            repaint();
        }
    }

    bool onMouse(const MouseEvent& ev) override
    {
        if (ev.button != 1)
            return false;
        const double scale = getScaleFactor();
        const double x = ev.pos.getX() / scale, y = ev.pos.getY() / scale;
        if (!ev.press) {
            if (fDragging >= 0)
                editParameter(SLIDER_PARAMETERS[fDragging], false);
            fDragging = -1;
            return false;
        }
        if (LOAD_BUTTON.contains(x, y)) {
            FileBrowserOptions options;
            options.title = "Load a DLS file";
            openFileBrowser(options);
            return true;
        }
        for (int i = 0; i < 3; i++)
            if (TOGGLES[i].contains(x, y)) {
                const uint32_t p = TOGGLE_PARAMETERS[i];
                fValues[p] = fValues[p] > 0.5f ? 0 : 1;
                editParameter(p, true);
                setParameterValue(p, fValues[p]);
                editParameter(p, false);
                repaint();
                return true;
            }
        for (int i = 0; i < 3; i++)
            if (SLIDERS[i].contains(x, y)) {
                fDragging = i;
                editParameter(SLIDER_PARAMETERS[i], true);
                drag(x);
                return true;
            }
        return false;
    }

    // The mouse wheel over a channel's program picks the bank's next or previous one, for hosts that don't pass the
    // MIDI file's program changes
    bool onScroll(const ScrollEvent& ev) override
    {
        const double scale = getScaleFactor();
        const double x = ev.pos.getX() / scale, y = ev.pos.getY() / scale;
        const int ch = int((y - TABLE_Y) / ROW) - 1;
        if (ch < 0 || ch >= 16 || x < COLUMNS[1] || x >= COLUMNS[3] || ev.delta.getY() == 0)
            return false;
        NitroPlugin* plugin = this->plugin();
        if (!plugin)
            return false;
        std::vector<int> programs = plugin->programs();
        if (programs.empty())
            return false;
        const int current = fView[ch].program;
        int next = current;
        if (ev.delta.getY() > 0) {  // Up: the next program
            auto it = std::upper_bound(programs.begin(), programs.end(), current);
            next = it != programs.end() ? *it : programs.front();
        } else {
            auto it = std::lower_bound(programs.begin(), programs.end(), current);
            next = it != programs.begin() ? *(it - 1) : programs.back();
        }
        const uint32_t p = kParameterProgram1 + uint32_t(ch);
        editParameter(p, true);
        setParameterValue(p, float(next));
        editParameter(p, false);
        fView[ch].program = next;
        repaint();
        return true;
    }

    bool onMotion(const MotionEvent& ev) override
    {
        if (fDragging < 0)
            return false;
        drag(ev.pos.getX() / getScaleFactor());
        return true;
    }

    void onNanoDisplay() override
    {
        const float s = float(getScaleFactor());
        scale(s, s);
        fontFace(NANOVG_DEJAVU_SANS_TTF);
        NitroPlugin* plugin = this->plugin();

        // The panel and header
        background(0, 0, W, H, Color(30, 33, 41));
        beginPath();
        rect(0, 0, W, 56);
        fillPaint(linearGradient(0, 0, 0, 56, Color(54, 62, 88), Color(36, 40, 56)));
        fill();
        label(16, 22, 20, "NITRO SYNTH", Color(255, 214, 102));
        label(16, 42, 11, "DS sound driver  -  Pokemon Black 2 / White 2  -  scroll a program to change it",
              Color(170, 178, 200));
        std::string bank = plugin ? plugin->bankName() : std::string();
        std::string path = plugin ? plugin->dlsPath() : std::string();
        std::string error = plugin ? plugin->loadError() : std::string();
        std::string shown = !error.empty() ? "Error: " + error
                          : !bank.empty()  ? bank
                          : !path.empty()  ? path.substr(path.find_last_of("/\\") + 1)
                                           : "No DLS file loaded";
        textAlign(ALIGN_RIGHT | ALIGN_MIDDLE);
        fontSize(12);
        fillColor(error.empty() ? Color(220, 224, 236) : Color(255, 120, 110));
        text(LOAD_BUTTON.x - 12, 29, shown.c_str(), nullptr);
        button(LOAD_BUTTON, "Load DLS...", true);

        // The settings
        for (int i = 0; i < 3; i++)
            button(TOGGLES[i], TOGGLE_LABELS[i], fValues[TOGGLE_PARAMETERS[i]] > 0.5f);
        for (int i = 0; i < 3; i++)
            slider(SLIDERS[i], SLIDER_LABELS[i], fValues[SLIDER_PARAMETERS[i]]);

        // The channels
        background(10, TABLE_Y - 8, W - 20, ROW * 17 + 12, Color(22, 24, 30));
        textAlign(ALIGN_LEFT | ALIGN_MIDDLE);
        fontSize(11);
        fillColor(Color(140, 148, 170));
        for (size_t c = 0; c < sizeof HEADERS / sizeof *HEADERS; c++)
            text(COLUMNS[c], TABLE_Y + ROW / 2, HEADERS[c], nullptr);
        char buffer[64];
        for (int ch = 0; ch < 16; ch++) {
            const nitro::ChannelView& v = fView[ch];
            const float y = TABLE_Y + ROW * (ch + 1);
            if (ch & 1)
                background(12, y, W - 24, ROW, Color(28, 31, 39));
            const Color text_ = v.voices ? Color(236, 238, 245) : Color(150, 156, 172);
            fontSize(12);
            fillColor(text_);
            std::snprintf(buffer, sizeof buffer, "%d", ch + 1);
            text(COLUMNS[0], y + ROW / 2, buffer, nullptr);
            std::snprintf(buffer, sizeof buffer, "%d", v.program);
            text(COLUMNS[1], y + ROW / 2, buffer, nullptr);
            std::string name = plugin ? plugin->instrumentName(v.program) : std::string();
            if (!bank.empty() && name.compare(0, bank.size(), bank) == 0)
                name = name.substr(std::min(name.size(), bank.size() + 1));
            if (name.size() > 34)
                name = name.substr(0, 33) + "...";
            text(COLUMNS[2], y + ROW / 2, name.c_str(), nullptr);
            std::snprintf(buffer, sizeof buffer, "%d", v.volume);
            text(COLUMNS[3], y + ROW / 2, buffer, nullptr);
            std::snprintf(buffer, sizeof buffer, "%d", v.expression);
            text(COLUMNS[4], y + ROW / 2, buffer, nullptr);
            std::snprintf(buffer, sizeof buffer, v.pan == 64 ? "C" : v.pan < 64 ? "L%d" : "R%d", std::abs(v.pan - 64));
            text(COLUMNS[5], y + ROW / 2, buffer, nullptr);
            std::snprintf(buffer, sizeof buffer, "%+d/%d", v.bend, v.bendRange);
            text(COLUMNS[6], y + ROW / 2, buffer, nullptr);
            std::snprintf(buffer, sizeof buffer, v.portamento ? "on %d" : "off", v.portamentoTime);
            text(COLUMNS[7], y + ROW / 2, buffer, nullptr);
            static const char* const types[] = {"pit", "vol", "pan"};
            std::snprintf(buffer, sizeof buffer, "%d %s", v.lfoDepth, types[v.lfoType < 3 ? v.lfoType : 0]);
            text(COLUMNS[8], y + ROW / 2, buffer, nullptr);
            // The voices as a meter of the 16 channels
            for (int k = 0; k < 8; k++) {
                beginPath();
                rect(COLUMNS[9] + k * 5, y + 6, 4, ROW - 12);
                fillColor(k < v.voices ? Color(120, 220, 140) : Color(50, 54, 64));
                fill();
            }
        }
    }

private:
    NitroPlugin* plugin() const { return static_cast<NitroPlugin*>(getPluginInstancePointer()); }

    void drag(double x)
    {
        const Box& box = SLIDERS[fDragging];
        const uint32_t p = SLIDER_PARAMETERS[fDragging];
        const double t = (x - (box.x + 110)) / (box.w - 120);
        const float value = float(int(std::max(0.0, std::min(1.0, t)) * 127 + 0.5));
        if (value != fValues[p]) {
            fValues[p] = value;
            setParameterValue(p, value);
            repaint();
        }
    }

    void background(float x, float y, float w, float h, const Color& color)
    {
        beginPath();
        rect(x, y, w, h);
        fillColor(color);
        fill();
    }

    void label(float x, float y, float size, const char* string, const Color& color)
    {
        textAlign(ALIGN_LEFT | ALIGN_MIDDLE);
        fontSize(size);
        fillColor(color);
        text(x, y, string, nullptr);
    }

    void button(const Box& b, const char* string, bool on)
    {
        beginPath();
        roundedRect(b.x, b.y, b.w, b.h, 5);
        fillColor(on ? Color(74, 110, 196) : Color(52, 56, 68));
        fill();
        strokeColor(on ? Color(130, 165, 240) : Color(80, 86, 100));
        stroke();
        textAlign(ALIGN_CENTER | ALIGN_MIDDLE);
        fontSize(12);
        fillColor(Color(240, 242, 248));
        text(b.x + b.w / 2, b.y + b.h / 2, string, nullptr);
    }

    void slider(const Box& b, const char* string, float value)
    {
        label(b.x, b.y + b.h / 2, 12, string, Color(200, 206, 222));
        const float x = b.x + 110, w = b.w - 120;
        beginPath();
        roundedRect(x, b.y + 9, w, 8, 4);
        fillColor(Color(52, 56, 68));
        fill();
        beginPath();
        roundedRect(x, b.y + 9, w * value / 127, 8, 4);
        fillColor(Color(255, 196, 80));
        fill();
        char buffer[16];
        std::snprintf(buffer, sizeof buffer, "%d", int(value));
        textAlign(ALIGN_RIGHT | ALIGN_MIDDLE);
        fontSize(11);
        fillColor(Color(230, 232, 240));
        text(b.x + b.w, b.y + 3, buffer, nullptr);
    }

    float fValues[kParameterCount];
    std::array<nitro::ChannelView, 16> fView{};
    int fDragging = -1;

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(NitroUI)
};

UI* createUI() { return new NitroUI(); }

END_NAMESPACE_DISTRHO
