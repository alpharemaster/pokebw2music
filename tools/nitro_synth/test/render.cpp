// Nitro Synth's engine outside a host: plays a MIDI file with a DLS file into a WAV file, to test the engine.
//
//     render BANK.dls SONG.mid OUT.wav [--clean] [--seconds N] [--rate HZ]
#include "../src/NitroEngine.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {

struct Event {
    double seconds;
    uint64_t order;
    std::vector<uint8_t> data;
};

uint32_t varlen(const std::vector<uint8_t>& d, size_t& pos)
{
    uint32_t value = 0;
    while (pos < d.size()) {
        uint8_t b = d[pos++];
        value = value << 7 | (b & 0x7F);
        if (!(b & 0x80))
            break;
    }
    return value;
}

// A MIDI file's channel events in time order, with its tempo map applied, and the last tempo
std::vector<Event> readMidi(const std::string& path, double& lastBpm)
{
    std::ifstream file(path, std::ios::binary);
    std::vector<uint8_t> d((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    if (d.size() < 14 || std::memcmp(d.data(), "MThd", 4))
        return {};
    int tracks = d[10] << 8 | d[11], division = d[12] << 8 | d[13];
    struct Raw { uint64_t tick, order; std::vector<uint8_t> data; bool tempo; uint32_t us; };
    std::vector<Raw> raw;
    size_t pos = 14;
    uint64_t order = 0;
    for (int t = 0; t < tracks && pos + 8 <= d.size(); t++) {
        uint32_t size = uint32_t(d[pos + 4] << 24 | d[pos + 5] << 16 | d[pos + 6] << 8 | d[pos + 7]);
        size_t end = pos + 8 + size;
        pos += 8;
        uint64_t tick = 0;
        uint8_t status = 0;
        while (pos < end) {
            tick += varlen(d, pos);
            if (d[pos] & 0x80)
                status = d[pos++];
            if (status == 0xFF) {
                uint8_t type = d[pos++];
                uint32_t length = varlen(d, pos);
                if (type == 0x51 && length == 3)
                    raw.push_back({tick, order++, {}, true, uint32_t(d[pos] << 16 | d[pos + 1] << 8 | d[pos + 2])});
                pos += length;
            } else if (status == 0xF0 || status == 0xF7) {
                pos += varlen(d, pos);
            } else {
                int length = (status & 0xF0) == 0xC0 || (status & 0xF0) == 0xD0 ? 1 : 2;
                std::vector<uint8_t> msg{status};
                for (int i = 0; i < length; i++)
                    msg.push_back(d[pos++]);
                raw.push_back({tick, order++, msg, false, 0});
            }
        }
        pos = end;
    }
    std::stable_sort(raw.begin(), raw.end(), [](const Raw& a, const Raw& b) { return a.tick < b.tick; });
    std::vector<Event> out;
    double seconds = 0, usPerTick = 500000.0 / division;
    uint64_t last = 0;
    lastBpm = 120;
    for (const Raw& r : raw) {
        seconds += (r.tick - last) * usPerTick / 1e6;
        last = r.tick;
        if (r.tempo) {
            usPerTick = double(r.us) / division;
            lastBpm = 60e6 / r.us;
            out.push_back({seconds, r.order, {0xFF, uint8_t(std::min(255.0, lastBpm))}});
        } else {
            out.push_back({seconds, r.order, r.data});
        }
    }
    return out;
}

void writeWav(const std::string& path, const std::vector<float>& left, const std::vector<float>& right, int rate)
{
    std::ofstream out(path, std::ios::binary);
    uint32_t frames = uint32_t(left.size()), bytes = frames * 4;
    auto u32 = [&](uint32_t v) { out.write(reinterpret_cast<const char*>(&v), 4); };
    auto u16 = [&](uint16_t v) { out.write(reinterpret_cast<const char*>(&v), 2); };
    out.write("RIFF", 4); u32(36 + bytes); out.write("WAVEfmt ", 8); u32(16); u16(1); u16(2); u32(rate);
    u32(rate * 4); u16(4); u16(16); out.write("data", 4); u32(bytes);
    for (uint32_t i = 0; i < frames; i++) {
        int16_t l = int16_t(std::clamp(left[i], -1.0f, 1.0f) * 32767), r = int16_t(std::clamp(right[i], -1.0f, 1.0f) * 32767);
        u16(uint16_t(l)); u16(uint16_t(r));
    }
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc < 4) {
        std::fprintf(stderr, "usage: render BANK.dls SONG.mid OUT.wav [--clean] [--seconds N] [--rate HZ]\n");
        return 2;
    }
    bool clean = false;
    double limit = 1e9;
    int rate = 48000;
    for (int i = 4; i < argc; i++) {
        if (!std::strcmp(argv[i], "--clean"))
            clean = true;
        else if (!std::strcmp(argv[i], "--seconds") && i + 1 < argc)
            limit = std::atof(argv[++i]);
        else if (!std::strcmp(argv[i], "--rate") && i + 1 < argc)
            rate = std::atoi(argv[++i]);
    }
    std::string error;
    auto bank = nitro::loadDls(argv[1], error);
    if (!bank) {
        std::fprintf(stderr, "error: %s\n", error.c_str());
        return 1;
    }
    std::printf("%s: %zu instruments, %zu samples\n", argv[1], bank->instruments.size(), bank->samples.size());
    double bpm;
    std::vector<Event> events = readMidi(argv[2], bpm);
    nitro::Engine engine;
    engine.setSampleRate(rate);
    engine.settings.hardware = !clean;
    engine.setBank(std::move(bank));
    double end = std::min(limit, events.empty() ? 0.0 : events.back().seconds + 2.0);
    std::vector<float> left(size_t(end * rate)), right(left.size());
    size_t next = 0, done = 0;
    int peakVoices = 0;
    while (done < left.size()) {
        size_t until = left.size();
        if (next < events.size())
            until = std::min(until, size_t(events[next].seconds * rate));
        if (until > done) {
            engine.render(left.data() + done, right.data() + done, uint32_t(until - done));
            done = until;
        }
        while (next < events.size() && size_t(events[next].seconds * rate) <= done) {
            const Event& e = events[next++];
            if (e.data[0] == 0xFF)
                engine.setTempo(e.data[1]);
            else
                engine.midi(e.data.data(), uint32_t(e.data.size()));
            peakVoices = std::max(peakVoices, engine.activeVoices());
        }
    }
    float peak = 0;
    double energy = 0;
    for (size_t i = 0; i < left.size(); i++) {
        peak = std::max({peak, std::fabs(left[i]), std::fabs(right[i])});
        energy += left[i] * left[i] + right[i] * right[i];
    }
    std::printf("%.1f s, peak %.3f, rms %.4f, most voices %d\n", end, peak,
                std::sqrt(energy / std::max<size_t>(1, 2 * left.size())), peakVoices);
    writeWav(argv[3], left, right, rate);
    return 0;
}
