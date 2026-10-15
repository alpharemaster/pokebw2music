# NDS Synth, a VST3 instrument that plays the DS sound banks as the game's sound driver does. build_windows.sh
# builds it with Visual Studio's compiler, from DPF (the DISTRHO Plugin Framework) at DPF_DIR.
cmake_minimum_required(VERSION 3.15)
project(nds_synth CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")  # No runtime DLLs to install

if(NOT DPF_DIR)
  message(FATAL_ERROR "set DPF_DIR to a checkout of https://github.com/DISTRHO/DPF")
endif()
add_subdirectory(${DPF_DIR} dpf)

dpf_add_plugin(NDSSynth
  TARGETS vst3
  USE_FILE_BROWSER
  FILES_DSP
    src/NdsPlugin.cpp
    src/NdsEngine.cpp
    src/NdsBank.cpp
  FILES_UI
    src/NdsUI.cpp)

target_include_directories(NDSSynth PUBLIC src)
