#!/usr/bin/env python3
"""Lets DPF's VST3 plugins take MIDI program changes.

    patch_dpf.py DPF_DIR

VST3 has no MIDI program change event: a host sends program changes as the parameter that the plugin's MIDI
mapping gives for controller 130 (kCtrlProgramChange), as it sends controllers. DPF maps controllers 0 to 129
(129 the pitch bend) on each channel to MIDI events, and leaves 130 out (its "TODO how do we deal with program
changes??"), so Nitro Synth never hears a program change. This maps 130 too, and turns it into a program change.
build_windows.sh runs it on its copy of DPF; it stops if DPF's code isn't the one it was written for.
"""
import sys
from pathlib import Path

root = Path(sys.argv[1]) / "distrho" / "src"
EDITS = {
    "DistrhoPluginVST.hpp": [
        ("kVst3InternalParameterCount = kVst3InternalParameterMidiCC_start + 130 * 16",
         "kVst3InternalParameterCount = kVst3InternalParameterMidiCC_start + 131 * 16"),
    ],
    "DistrhoPluginVST3.cpp": [
        ("            CC_Pitchbend,\n", "            CC_Pitchbend,\n            CC_ProgramChange,\n"),
        ("""                case CC_Pitchbend:
                    midiEvent.size = 3;
                    midiEvent.data[0] = 0xE0 | (eventStorage.midi[0] & 0xf);""",
         """                case CC_ProgramChange:
                    midiEvent.size = 2;
                    midiEvent.data[0] = 0xC0 | (eventStorage.midi[0] & 0xf);
                    midiEvent.data[1] = eventStorage.midi[1];
                    midiEvent.data[2] = 0;
                    break;
                case CC_Pitchbend:
                    midiEvent.size = 3;
                    midiEvent.data[0] = 0xE0 | (eventStorage.midi[0] & 0xf);"""),
        ("            const uint8_t cc = paramId % 130;", "            const uint8_t cc = paramId % 131;"),
        ("""            default:
                eventStorage.type = CC_Normal;""", """            case 130:
                eventStorage.type = CC_ProgramChange;
                eventStorage.midi[1] = std::max(0, std::min(127, d_roundToIntPositive(normalized * 127)));
                eventStorage.midi[2] = 0;
                break;
            default:
                eventStorage.type = CC_Normal;"""),
        ("            eventStorage.midi[0] = paramId / 130;", "            eventStorage.midi[0] = paramId / 131;"),
        ('snprintf(ccstr, sizeof(ccstr), "MIDI Ch. %d CC %d", static_cast<uint8_t>(index / 130) + 1, index % 130);',
         'snprintf(ccstr, sizeof(ccstr), "MIDI Ch. %d CC %d", static_cast<uint8_t>(index / 131) + 1, index % 131);'),
        ('snprintf(ccstr, sizeof(ccstr), "Ch.%d CC%d", index / 130 + 1, index % 130);',
         'snprintf(ccstr, sizeof(ccstr), "Ch.%d CC%d", index / 131 + 1, index % 131);'),
        ("DISTRHO_SAFE_ASSERT_INT_RETURN(cc >= 0 && cc < 130, cc, V3_FALSE);",
         "DISTRHO_SAFE_ASSERT_INT_RETURN(cc >= 0 && cc < 131, cc, V3_FALSE);"),
        ("*id = kVst3InternalParameterMidiCC_start + channel * 130 + cc;",
         "*id = kVst3InternalParameterMidiCC_start + channel * 131 + cc;"),
    ],
}

for name, edits in EDITS.items():
    path = root / name
    text = path.read_text()
    if all(new in text for _, new in edits):
        continue  # Already patched
    for old, new in edits:
        if text.count(old) != 1:
            sys.exit(f"error: {path}: expected one {old.strip()[:70]!r}; DPF changed, so update patch_dpf.py")
        text = text.replace(old, new)
    path.write_text(text)
print("DPF: VST3 program changes mapped")
