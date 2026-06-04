# Builds the standalone C++ string-codec binaries for likebench.
#
#   cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
#   cmake --build cpp/build -j
#
# Produces: cpp/build/bench-compress-cpp  (referenced from benchmarks.toml).
cmake_minimum_required(VERSION 3.16)
project(likebench_cpp CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG")
set(CMAKE_C_FLAGS_RELEASE "-O3 -DNDEBUG")

# LZ4 (vendored single translation unit).
add_library(lz4 STATIC external/lz4/lz4.c)
target_include_directories(lz4 PUBLIC external/lz4)

add_executable(bench-compress-cpp
    bench-compress-cpp/main.cpp
    external/fsst/libfsst.cpp
    external/fsst/fsst_avx512.cpp
    external/fsst12/libfsst12.cpp
    external/onpair/src/onpair.cpp
    external/onpair/src/onpair16.cpp
)
# onpair_cpp headers use #include <robin_hood.h> and sibling headers (lpm.h, …).
target_include_directories(bench-compress-cpp PRIVATE
    external/onpair/include
    external/robin_hood
)
target_link_libraries(bench-compress-cpp PRIVATE lz4)
