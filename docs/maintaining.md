# Maintaining the image

How the image is built, how to change a patch, and what CI does. For running the room, see the [README](../README.md).

## The patch series

The image is Eden's standalone room (`eden-room`) built from the tip of Eden's `master` ([`upstream.json`](../upstream.json)) with the `git format-patch` files in [`patches/`](../patches), applied in order. Each patch is one theme, and its commit message is the full explanation:

| Patch | Upstream files | What it does |
|---|---|---|
| 001 Build the static room with the lobby web service | `CMakeLists.txt`, `externals/CMakeLists.txt`, `src/CMakeLists.txt` | Keeps `ENABLE_WEB_SERVICE` in `YUZU_STATIC_ROOM` builds |
| 002 Run the room as a container service | `src/dedicated_room/yuzu_room.cpp`, `src/common/logging.cpp` | Signal-driven shutdown, `--username` fix, wall-clock log lines, thread-safe log backends |
| 003 Fix and harden the public lobby announce | `src/web_service/*`, `src/network/announce_multiplayer_session.cpp` | Lobby player format, bare status codes (404 re-register, 401 JWT refresh), HTTP methods, reply validation, safe unregister |
| 004 Validate packets and enforce room admission | `src/network/room.cpp` | Packet bounds, relay cap and budget, admission before relay, join rate limit, local-only moderators |
| 005 Relay game traffic for internet play | `src/network/room.cpp` | Prompt flush and shared locks, relay modes, ENet throttle and timeouts, unknown-IP fallback |
| 006 Log room activity and transport diagnostics | `src/network/room.cpp`, `src/common/logging.cpp` | Activity tags and labels, player counts, `STAT` and `DIAG` |

Conventions in the patches:

- Every `EDEN_ROOM_*` setting is a `const` member of `RoomImpl`, in the settings block, read once when the room is created (`EnvNumber`, `EnvString`).
- Room activity is logged as a Network Info message whose first word is a tag (`JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, `STAT`, `DIAG`); `FormatLogMessage` turns the tag into the label. A new kind of activity line needs its tag added to `ActivityTags` in `logging.cpp`.
- The image's defaults live in the [`Dockerfile`](../Dockerfile) and [`docker-entrypoint.sh`](../docker-entrypoint.sh), which validates every setting before starting the room.

## Changing a patch

You need Git, Bash and Python 3. Building needs Linux with CMake, Ninja, `patch`, Clang 19+ (with `clang-scan-deps`) or GCC 16+ (upstream uses C++20 modules) and the OpenSSL development files; the tests need `libenet7`. On Ubuntu 24.04: `clang-20 clang-tools-20 ninja-build patch libssl-dev libenet7`, with `CC=/usr/lib/llvm-20/bin/clang CXX=/usr/lib/llvm-20/bin/clang++`.

```sh
scripts/patches.sh prepare      # clone Eden into work/upstream and apply the patches as commits
# edit work/upstream with ordinary git: amend the patch's commit, or add a commit for a new patch
scripts/build-room.sh work/upstream build                       # build build/eden-room
python3 tests/test_room_protocol.py --binary build/eden-room    # the protocol tests
python3 tests/test_room_protocol.py --binary build/eden-room --stress 120   # optional: relay under churn
scripts/patches.sh export       # write the commits back to patches/
git diff patches/               # review, then commit and push
```

Each commit becomes one patch file; its subject becomes the file name. A new or renamed patch needs a matching entry in [`patch-watch.json`](../patch-watch.json) (CI checks that every patch has one).

If a patch stops applying, `prepare` stops at it with the conflicts marked: fix them, run `git -C work/upstream am --continue`, then `export`.

## What CI does

[`build.yml`](../.github/workflows/build.yml) runs every Monday at 11 am Sydney time, on every push to `main` that changes the build (Markdown and `docs/` don't count), and on demand (**Actions → Build Eden Room → Run workflow**, optionally with an upstream branch, tag or commit).

1. **Decide.** [`scripts/upstream.py`](../scripts/upstream.py) finds the tip of Eden's `master` and compares it, the base image and this repository with the labels on the published `latest` image. If nothing changed, the run stops, unless the image is a week old: it installs Ubuntu packages, so it is rebuilt weekly for their updates.
2. **Patch.** The patches are applied with a 3-way merge, so small upstream changes around them are absorbed. [`scripts/patch-watch.py`](../scripts/patch-watch.py) lists upstream commits that touched files a patch depends on.
3. **Build and test the room.** [`scripts/build-room.sh`](../scripts/build-room.sh) builds the static room (ccache and downloaded dependencies are cached between runs), and [`tests/test_room_protocol.py`](../tests/test_room_protocol.py) runs it against generated ENet clients and a stand-in lobby.
4. **Build and test the image.** The image must start as the `eden` user, stop with exit code 0, and pass the protocol tests again (the image reaches the stand-in lobby through `host.docker.internal`).
5. **Publish.** Only from `main`: the image goes to Docker Hub with its tags, and this README becomes the Docker Hub description. The image's labels record the exact upstream commit, which is how the next run knows what was built. Nothing is committed back to this repository.

A manual run with an upstream ref publishes its pinning tag but never moves `latest`. A manual run on another branch builds and tests but publishes nothing.

### When something goes wrong

- **A patch no longer applies, or the build or tests fail:** an issue titled **Build failed** opens with the conflict or the failing step, and the last good image stays published. The next successful build closes it.
- **Upstream touched code a patch depends on** (even if the patch still applies): an issue titled **Patch review needed** lists the upstream commits. Read the patch against them, and when it is still right, bump its `reviewed_against` in `patch-watch.json`. A clean apply and green tests don't prove a patch still works: in November 2026 upstream's move to Jacinth and Hydrocarbon broke the lobby with every patch applying and every test passing, which is why the stand-in lobby now checks the update format and the re-registration after a 404.

### What the tests cover

`tests/test_room_protocol.py` runs the room over the real ENet protocol:

- admission, rejection, the join rate limit and moderator rights;
- packet validation and byte-exact relay, including to unknown addresses, and the delivery flags each kind of packet gets;
- the log labels, `STAT` and `DIAG`, including for a player behind a link that delays, loses and pauses their packets;
- a clean `SIGTERM` stop, also of a public room while it unregisters from a stand-in lobby that takes 25 seconds to answer, with its ban list saved;
- lobby updates in the lobby's player format, re-registration after a 404, and requests without `Expect: 100-continue`;
- with `--binary`, a room whose log file can't be written.

The other tests cover the scripts: [`tests/test_entrypoint.py`](../tests/test_entrypoint.py) (exit status and signal handling), [`tests/test_upstream.py`](../tests/test_upstream.py) and [`tests/test_patch_watch.py`](../tests/test_patch_watch.py).

## Repository layout

| Path | Purpose |
|---|---|
| `patches/` | The patch series |
| `upstream.json` | Which upstream to build |
| `Dockerfile` | The runtime image around the prebuilt static `eden-room` |
| `docker-entrypoint.sh` | Validates the settings, drops to `PUID`/`PGID`, writes session logs, starts the room |
| `patch-watch.json` | Upstream files and keywords each patch depends on |
| `scripts/build-room.sh` | Build the static room from a patched checkout |
| `scripts/patches.sh` | Fetch upstream, apply, prepare for editing, export |
| `scripts/upstream.py` | Find the commit to build and decide whether to build |
| `scripts/patch-watch.py` | Flag upstream changes that may affect a patch |
| `scripts/issue.sh` | Open and close the CI issues |
| `tests/` | The tests above |

## Why `master` and a static build

Eden's last release is months older than `master`, and both speak the same room protocol (`network_version` 1), so the image follows `master` and any Eden client with that network version can join. The room is built with upstream's `YUZU_STATIC_ROOM` option: it configures nothing of the emulator, so the build is fast and immune to the emulator's own dependencies (Qt, SDL, FFmpeg, Vulkan), and the result needs no shared libraries. The image is plain Ubuntu with `gosu`, `tini`, CA certificates and time zones.
