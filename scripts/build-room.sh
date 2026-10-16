#!/usr/bin/env bash
# Build Eden's standalone room server as a static, stripped executable.
#
#   scripts/build-room.sh <eden-source-dir> <output-dir>
#
# YUZU_STATIC_ROOM configures only the room and the libraries it links (patch 001
# keeps the lobby web service in it). Every dependency is the version upstream
# pins in cpmfile.json, except OpenSSL and Boost, which come from the system
# (Debian/Ubuntu: libssl-dev, libboost-filesystem-dev, libboost-context-dev).
# Needs CMake (upstream's minimum or newer), Ninja and a C++20 compiler; uses
# ccache when it is installed.
set -euo pipefail

src="$(cd "$1" && pwd)"
out="$2"
build="$src/build-room"
launcher=()
if command -v ccache > /dev/null; then
  launcher=(-DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache)
fi

# The room reports its version as <commit>-<refspec>. Without these files Eden reads them
# from git, where HEAD is the last patch commit; name the upstream commit the patches went on.
git -C "$src" rev-parse --verify -q refs/patches/base > "$src/GIT-COMMIT" || git -C "$src" rev-parse HEAD > "$src/GIT-COMMIT"
printf 'patched' > "$src/GIT-REFSPEC"
exclude="$(git -C "$src" rev-parse --git-path info/exclude)"
grep -qx GIT-COMMIT "$exclude" 2>/dev/null || printf 'GIT-COMMIT
GIT-REFSPEC
' >> "$exclude"

cmake -S "$src" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DYUZU_STATIC_ROOM=ON -DENABLE_WEB_SERVICE=ON \
  -DCPMUTIL_FORCE_BUNDLED=ON -DBoost_FORCE_SYSTEM=ON "${launcher[@]}"
cmake --build "$build" --target yuzu_room_standalone

mkdir -p "$out"
strip -o "$out/eden-room" "$build/bin/eden-room"
"$out/eden-room" --version
