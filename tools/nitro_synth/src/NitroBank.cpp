// Nitro Synth: reading DLS files (see NitroBank.hpp)
#include "NitroBank.hpp"
#include "NitroEngine.hpp"

#include <algorithm>
#include <climits>
#include <cmath>
#include <cstring>
#include <fstream>

namespace nitro {

const Region* Instrument::find(int key) const
{
    for (const Region& region : regions)
        if (key >= region.low && key <= region.high)
            return &region;
    return nullptr;
}

const Instrument* Bank::find(int program) const
{
    auto it = instruments.find(program);
    if (it != instruments.end())
        return &it->second;
    it = drums.find(program);
    return it != drums.end() ? &it->second : nullptr;
}

namespace {

struct Chunk {
    char tag[5] = {};
    char kind[5] = {};      // A LIST's type
    const uint8_t* data = nullptr;
    uint32_t size = 0;
};

uint16_t u16(const uint8_t* p) { return uint16_t(p[0] | p[1] << 8); }
uint32_t u32(const uint8_t* p) { return uint32_t(p[0] | p[1] << 8 | p[2] << 16 | uint32_t(p[3]) << 24); }

// The chunks inside a RIFF or LIST's data
std::vector<Chunk> children(const uint8_t* data, uint32_t size)
{
    std::vector<Chunk> out;
    uint32_t pos = 0;
    while (pos + 8 <= size) {
        Chunk chunk;
        std::memcpy(chunk.tag, data + pos, 4);
        uint32_t length = u32(data + pos + 4);
        if (length > size - pos - 8)
            break;
        chunk.data = data + pos + 8;
        chunk.size = length;
        if ((!std::strcmp(chunk.tag, "LIST") || !std::strcmp(chunk.tag, "RIFF")) && length >= 4) {
            std::memcpy(chunk.kind, chunk.data, 4);
            chunk.data += 4;
            chunk.size -= 4;
        }
        out.push_back(chunk);
        pos += 8 + length + (length & 1);
    }
    return out;
}

const Chunk* find(const std::vector<Chunk>& chunks, const char* tag, const char* kind = nullptr)
{
    for (const Chunk& chunk : chunks)
        if (!std::strcmp(chunk.tag, tag) && (!kind || !std::strcmp(chunk.kind, kind)))
            return &chunk;
    return nullptr;
}

std::string infoName(const std::vector<Chunk>& chunks)
{
    if (const Chunk* info = find(chunks, "LIST", "INFO"))
        if (const Chunk* name = find(children(info->data, info->size), "INAM"))
            return std::string(reinterpret_cast<const char*>(name->data), strnlen(reinterpret_cast<const char*>(name->data), name->size));
    return {};
}

// The nearest driver value to a DLS time, for the files without "dsrg" chunks
int nearest(double seconds, double (*convert)(int))
{
    int best = 127;
    double error = 1e30;
    for (int value = 0; value < 128; value++) {
        double e = std::fabs(std::log((convert(value) + 1e-4) / (seconds + 1e-4)));
        if (e < error) {
            error = e;
            best = value;
        }
    }
    return best;
}

double timeOf(int32_t timeCents) { return timeCents == INT32_MIN ? 0.0 : std::pow(2.0, timeCents / 65536.0 / 1200.0); }

struct Wave {
    int sample = -1;
    double rate = 32768;
    int unity = 60, fine = 0;
};

}  // namespace

std::unique_ptr<Bank> loadDls(const std::string& path, std::string& error)
{
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        error = "can't open " + path;
        return nullptr;
    }
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    std::vector<Chunk> top = children(bytes.data(), uint32_t(bytes.size()));
    if (top.empty() || std::strcmp(top[0].tag, "RIFF") || std::strcmp(top[0].kind, "DLS ")) {
        error = "not a DLS file";
        return nullptr;
    }
    std::vector<Chunk> body = children(top[0].data, top[0].size);
    auto bank = std::make_unique<Bank>();
    bank->name = infoName(body);

    // The wave pool, in the pool table's order
    std::vector<Wave> waves;
    const Chunk* pool = find(body, "LIST", "wvpl");
    const Chunk* table = find(body, "ptbl");
    if (!pool || !table || table->size < 8) {
        error = "DLS file without a wave pool";
        return nullptr;
    }
    uint32_t count = u32(table->data + 4);
    for (uint32_t i = 0; i < count && 8 + 4 * i + 4 <= table->size; i++) {
        uint32_t offset = u32(table->data + 8 + 4 * i);
        Wave wave;
        if (offset + 12 <= pool->size && !std::memcmp(pool->data + offset, "LIST", 4)) {
            uint32_t length = u32(pool->data + offset + 4);
            std::vector<Chunk> parts = children(pool->data + offset + 12, std::min(length - 4, pool->size - offset - 12));
            const Chunk* fmt = find(parts, "fmt ");
            const Chunk* data = find(parts, "data");
            if (fmt && data && fmt->size >= 16 && u16(fmt->data) == 1) {
                int channels = u16(fmt->data + 2), bits = u16(fmt->data + 14);
                Sample sample;
                uint32_t frames = data->size / (channels * bits / 8);
                sample.data.resize(frames);
                for (uint32_t f = 0; f < frames; f++) {
                    const uint8_t* at = data->data + f * channels * (bits / 8);
                    sample.data[f] = bits == 16 ? int16_t(u16(at)) : int16_t((at[0] - 128) << 8);
                }
                wave.rate = u32(fmt->data + 4);
                if (const Chunk* wsmp = find(parts, "wsmp"); wsmp && wsmp->size >= 20) {
                    wave.unity = u16(wsmp->data + 4);
                    wave.fine = int16_t(u16(wsmp->data + 6));
                    if (u32(wsmp->data + 16) && wsmp->size >= 36) {
                        sample.looped = true;
                        sample.loopStart = u32(wsmp->data + 28);
                    }
                }
                wave.sample = int(bank->samples.size());
                bank->samples.push_back(std::move(sample));
            }
        }
        waves.push_back(wave);
    }

    const Chunk* list = find(body, "LIST", "lins");
    if (!list) {
        error = "DLS file without instruments";
        return nullptr;
    }
    for (const Chunk& entry : children(list->data, list->size)) {
        if (std::strcmp(entry.kind, "ins "))
            continue;
        std::vector<Chunk> parts = children(entry.data, entry.size);
        const Chunk* header = find(parts, "insh");
        const Chunk* regions = find(parts, "LIST", "lrgn");
        if (!header || header->size < 12 || !regions)
            continue;
        uint32_t locale = u32(header->data + 4);
        uint32_t number = ((locale >> 8) & 0x7F) * 128 + (u32(header->data + 8) & 0x7F);
        Instrument instrument;
        instrument.name = infoName(parts);
        for (const Chunk& item : children(regions->data, regions->size)) {
            if (std::strcmp(item.kind, "rgn ") && std::strcmp(item.kind, "rgn2"))
                continue;
            std::vector<Chunk> fields = children(item.data, item.size);
            const Chunk* rgnh = find(fields, "rgnh");
            const Chunk* wlnk = find(fields, "wlnk");
            if (!rgnh || rgnh->size < 4 || !wlnk || wlnk->size < 12)
                continue;
            uint32_t index = u32(wlnk->data + 8);
            if (index >= waves.size() || waves[index].sample < 0)
                continue;
            const Wave& wave = waves[index];
            Region region;
            region.low = uint8_t(std::min<uint16_t>(127, u16(rgnh->data)));
            region.high = uint8_t(std::min<uint16_t>(127, u16(rgnh->data + 2)));
            region.sample = wave.sample;
            int unity = wave.unity, fine = wave.fine;
            Sample& sample = bank->samples[wave.sample];
            if (const Chunk* wsmp = find(fields, "wsmp"); wsmp && wsmp->size >= 20) {
                unity = u16(wsmp->data + 4);
                fine = int16_t(u16(wsmp->data + 6));
            }
            if (const Chunk* native = find(fields, "dsrg"); native && native->size >= 12) {
                const uint8_t* d = native->data;
                region.type = WaveType(d[0] >= 1 && d[0] <= 3 ? d[0] : 1);
                region.duty = d[1];
                region.baseKey = d[2];
                region.attack = d[3];
                region.decay = d[4];
                region.sustain = d[5];
                region.release = d[6];
                region.pan = d[7];
                region.timer = u16(d + 8);
            } else {
                // A foreign DLS file: its pitch as a timer, and the envelope nearest to its articulation
                region.baseKey = uint8_t(unity & 0x7F);
                double rate = wave.rate * std::pow(2.0, fine / 1200.0);
                region.timer = uint16_t(std::clamp(ARM7_CLOCK / rate, 16.0, 65535.0));
                const Chunk* lart = find(fields, "LIST", "lart");
                if (!lart)
                    lart = find(parts, "LIST", "lart");
                if (lart)
                    if (const Chunk* art = find(children(lart->data, lart->size), "art1"); art && art->size >= 8) {
                        uint32_t blocks = u32(art->data + 4);
                        for (uint32_t b = 0; b < blocks && 8 + 12 * b + 12 <= art->size; b++) {
                            const uint8_t* c = art->data + 8 + 12 * b;
                            uint16_t source = u16(c), destination = u16(c + 4);
                            int32_t scale = int32_t(u32(c + 8));
                            if (source != 0)
                                continue;
                            if (destination == 0x0206)
                                region.attack = uint8_t(nearest(timeOf(scale), attackSeconds));
                            else if (destination == 0x0207)
                                region.decay = uint8_t(nearest(timeOf(scale), fallSeconds));
                            else if (destination == 0x0209)
                                region.release = uint8_t(nearest(timeOf(scale), fallSeconds));
                            else if (destination == 0x020A) {
                                double level = std::clamp(scale / 65536.0 / 1000.0, 0.0, 1.0);
                                double db = (level - 1) * 96;  // DLS's 96 dB range
                                region.sustain = uint8_t(std::clamp(std::lround(127 * std::pow(10.0, db / 40)), 0L, 127L));
                            } else if (destination == 0x0004)
                                region.pan = uint8_t(std::clamp(64 + std::lround(scale / 65536.0 * 64 / 500), 0L, 127L));
                        }
                    }
            }
            (void)sample;
            instrument.regions.push_back(region);
        }
        if (instrument.regions.empty())
            continue;
        if (locale & 0x80000000u)
            bank->drums.emplace(number, std::move(instrument));
        else
            bank->instruments.emplace(number, std::move(instrument));
    }
    if (bank->instruments.empty() && bank->drums.empty()) {
        error = "DLS file without playable instruments";
        return nullptr;
    }
    return bank;
}

}  // namespace nitro
