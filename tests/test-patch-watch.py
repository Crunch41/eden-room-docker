#!/usr/bin/env python3
"""Tests for patch-watch.py.

The headline case is test_real_backblaze_regression: run the gate against the
exact upstream range that silently obsoleted a patch in August 2026 and assert
it now gets flagged. Everything else guards against the gate becoming noisy,
which would be just as useless as it being silent.

Run:  python3 test_patch_watch.py [--offline]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
# The script normally lives in scripts/ with this test in tests/, but both sit
# side by side in the scratch build tree. Accept either.
_CANDIDATES = [
    os.path.join(_HERE, os.pardir, "scripts", "patch-watch.py"),
    os.path.join(_HERE, "patch-watch.py"),
    os.path.join(_HERE, os.pardir, "patch-watch.py"),
]
for _cand in _CANDIDATES:
    if os.path.exists(_cand):
        _SCRIPT = os.path.abspath(_cand)
        break
else:
    raise SystemExit(f"cannot find patch-watch.py; looked in: {_CANDIDATES}")

spec = importlib.util.spec_from_file_location("patch_watch", _SCRIPT)
pw = importlib.util.module_from_spec(spec)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules["patch_watch"] = pw
spec.loader.exec_module(pw)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


# ---------------------------------------------------------------- unit: paths

def test_path_matching():
    print("\n[path matching]")
    check("exact file", pw.path_matches("server/sock.c", "server/sock.c"))
    check("dir prefix", pw.path_matches("dlls/ws2_32/socket.c", "dlls/ws2_32/"))
    check("dir without slash", pw.path_matches("dlls/ws2_32/socket.c", "dlls/ws2_32"))
    check("glob", pw.path_matches("src/Api/Foo.cs", "src/Api/*.cs"))
    check("basename glob", pw.path_matches("a/b/CustomFormat.cs", "CustomFormat.cs"))
    check("no false prefix", not pw.path_matches("server/socket_other.c", "server/sock.c"),
          "server/sock.c must not match server/socket_other.c")
    check("unrelated", not pw.path_matches("docs/README.md", "server/"))


# ------------------------------------------------------------- unit: keywords

def test_keyword_matching():
    print("\n[keyword matching]")
    hits = pw.keyword_hits("Fix upload throughput on slow sockets", ["upload", "throughput", "gui"])
    check("counts distinct hits", sorted(hits) == ["throughput", "upload"], str(hits))
    check("case insensitive", pw.keyword_hits("UPLOAD fixed", ["upload"]) == ["upload"])
    check("word boundary", pw.keyword_hits("preloader changes", ["load"]) == [],
          "'load' should not match inside 'preloader'")
    check("multiword phrase", pw.keyword_hits("the send buffer is pinned", ["send buffer"]) == ["send buffer"])
    check("no match", pw.keyword_hits("update translations", ["socket", "upload"]) == [])


# --------------------------------------------------------- unit: thresholding

def test_threshold_behaviour():
    print("\n[threshold]")
    c = pw.Commit("a" * 40, "improve upload speed", "", [])
    one_kw = [{"id": "p", "watch_keywords": ["upload", "socket"], "min_keyword_hits": 2}]
    check("single keyword below threshold", pw.evaluate(one_kw, [c]) == [])

    c2 = pw.Commit("b" * 40, "improve upload socket speed", "", [])
    check("two keywords trip it", len(pw.evaluate(one_kw, [c2])) == 1)

    lowered = [{"id": "p", "watch_keywords": ["upload"], "min_keyword_hits": 1}]
    check("min_keyword_hits=1 honoured", len(pw.evaluate(lowered, [c])) == 1)

    dropped = [{"id": "p", "disposition": "drop", "watch_keywords": ["upload"], "min_keyword_hits": 1}]
    check("disposition=drop is skipped", pw.evaluate(dropped, [c]) == [])

    pathp = [{"id": "p", "watch_paths": ["server/sock.c"]}]
    c3 = pw.Commit("c" * 40, "unrelated subject", "", ["server/sock.c"])
    check("path hit alone is enough", len(pw.evaluate(pathp, [c3])) == 1)

    c4 = pw.Commit("d" * 40, "unrelated subject", "", ["docs/x.md"])
    check("no path hit, no keywords", pw.evaluate(pathp, [c4]) == [])


def test_ignore_paths():
    print("\n[ignore_paths]")
    patch = [{"id": "p", "watch_paths": ["src/i18n/locale/en.json", "src/real.ts"]}]
    noisy = pw.Commit("e" * 40, "chore: translations", "", ["src/i18n/locale/en.json"])
    check("ignored path does not fire",
          pw.evaluate(patch, [noisy], ["src/i18n/locale/*.json"]) == [])
    check("same commit fires without the ignore list",
          len(pw.evaluate(patch, [noisy])) == 1)

    mixed = pw.Commit("f" * 40, "feat", "", ["src/i18n/locale/en.json", "src/real.ts"])
    got = pw.evaluate(patch, [mixed], ["src/i18n/locale/*.json"])
    check("real path still fires alongside an ignored one", len(got) == 1)
    check("ignored file excluded from the evidence",
          got and "en.json" not in " ".join(got[0].matched))


def test_coverage_check():
    print("\n[coverage check]")
    tmp = tempfile.mkdtemp(prefix="pw-cov-")
    try:
        pdir = os.path.join(tmp, "patches")
        os.makedirs(pdir)
        for n in ("001-alpha.patch", "002-beta.patch"):
            open(os.path.join(pdir, n), "w").close()

        complete = {"patches": [{"id": "001-alpha"}, {"id": "002-beta"}]}
        check("complete manifest passes", pw.check_coverage(complete, pdir) == 0)

        partial = {"patches": [{"id": "001-alpha"}]}
        check("missing entry fails", pw.check_coverage(partial, pdir) == 1)

        orphaned = {"patches": [{"id": "001-alpha"}, {"id": "002-beta"}, {"id": "003-ghost"}]}
        check("orphan entry warns but passes", pw.check_coverage(orphaned, pdir) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ integration: local git

def test_local_repo_end_to_end():
    print("\n[end-to-end against a synthetic repo]")
    tmp = tempfile.mkdtemp(prefix="pw-test-")
    try:
        up = os.path.join(tmp, "upstream")
        os.makedirs(os.path.join(up, "server"))
        git(["init", "-q", "-b", "main"], cwd=up)
        git(["config", "user.email", "t@t"], cwd=up)
        git(["config", "user.name", "t"], cwd=up)

        with open(os.path.join(up, "README.md"), "w") as fh:
            fh.write("base\n")
        git(["add", "-A"], cwd=up)
        git(["commit", "-qm", "initial"], cwd=up)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=up,
                              capture_output=True, text=True).stdout.strip()

        with open(os.path.join(up, "docs.md"), "w") as fh:
            fh.write("docs\n")
        git(["add", "-A"], cwd=up)
        git(["commit", "-qm", "docs: tidy wording"], cwd=up)

        with open(os.path.join(up, "server", "sock.c"), "w") as fh:
            fh.write("int main(void){return 0;}\n")
        git(["add", "-A"], cwd=up)
        git(["commit", "-qm", "fix: rework socket write notification"], cwd=up)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=up,
                              capture_output=True, text=True).stdout.strip()

        manifest = {
            "upstreams": {"main": {"url": up}},
            "patches": [
                {"id": "sock-patch", "disposition": "retain",
                 "watch_paths": ["server/sock.c"], "watch_keywords": []},
                {"id": "unrelated-patch", "disposition": "retain",
                 "watch_paths": ["gui/"], "watch_keywords": ["translation"]},
            ],
        }
        mpath = os.path.join(tmp, "patch-watch.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)

        summary = os.path.join(tmp, "summary.md")
        findings = os.path.join(tmp, "findings.md")
        rc = pw.main([
            "--manifest", mpath, "--old", base, "--new", head,
            "--work-dir", os.path.join(tmp, "mirror"),
            "--summary-out", summary, "--findings-out", findings,
        ])
        text = open(summary, encoding="utf-8").read()

        check("exit 0 in advisory mode", rc == 0, f"rc={rc}")
        check("flags the sock patch", "sock-patch" in text)
        check("does not flag unrelated patch", "unrelated-patch" not in text.split("Patch review ages")[0])
        check("lists both upstream commits",
              "docs: tidy wording" in text and "rework socket write" in text)
        check("writes findings file", os.path.getsize(findings) > 0)

        # strict mode must fail the build
        manifest["strict"] = True
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        rc = pw.main(["--manifest", mpath, "--old", base, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror2"),
                      "--summary-out", os.path.join(tmp, "s2.md")])
        check("strict mode exits 1", rc == 1, f"rc={rc}")

        # clean range -> no findings, no failure
        manifest["strict"] = False
        manifest["patches"] = [{"id": "quiet", "watch_paths": ["nothing/"], "watch_keywords": []}]
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        s3 = os.path.join(tmp, "s3.md")
        rc = pw.main(["--manifest", mpath, "--old", base, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror3"), "--summary-out", s3])
        check("clean range exits 0", rc == 0)
        check("clean range says no overlap", "No patch overlap detected" in open(s3, encoding="utf-8").read())

        # unknown sha must degrade, not fail
        s4 = os.path.join(tmp, "s4.md")
        rc = pw.main(["--manifest", mpath, "--old", "f" * 40, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror4"), "--summary-out", s4])
        check("unknown sha degrades to exit 0", rc == 0, f"rc={rc}")
        check("unknown sha explains itself",
              "Could not review" in open(s4, encoding="utf-8").read())

        # identical shas
        s5 = os.path.join(tmp, "s5.md")
        rc = pw.main(["--manifest", mpath, "--old", head, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror5"), "--summary-out", s5])
        check("no-op range exits 0", rc == 0)
        check("no-op range says unchanged",
              "unchanged" in open(s5, encoding="utf-8").read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------ regression: the real miss

def test_real_backblaze_regression():
    """The August 2026 miss, replayed.

    Upstream 0e0206b fixed the Backblaze upload slowdown a different way than
    our bbtune patch did. It rebased cleanly and nothing complained. The gate
    must flag it now.
    """
    print("\n[regression: real backblaze 2e5508a..0e0206b]")
    tmp = tempfile.mkdtemp(prefix="pw-bb-")
    try:
        manifest = {
            "upstreams": {"backblaze": {
                "url": "https://github.com/JonathanTreffler/backblaze-personal-wine-container.git"}},
            "patches": [
                {"id": "5-bbtune", "disposition": "retain",
                 "watch_paths": ["src/bbtune.c", "patches/", "scripts/build-wineserver.sh"],
                 "watch_keywords": ["upload", "throughput", "socket", "send buffer",
                                    "sndbuf", "wineserver", "mbit"],
                 "min_keyword_hits": 2},
                {"id": "6-logo-only", "disposition": "retain",
                 "watch_paths": ["assets/logo.png"],
                 "watch_keywords": ["logo", "branding"], "min_keyword_hits": 2},
            ],
        }
        mpath = os.path.join(tmp, "patch-watch.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        summary = os.path.join(tmp, "summary.md")
        rc = pw.main([
            "--manifest", mpath,
            "--old", "2e5508abdaebf1ec4d4b2044a9f770f1ef8a1c50",
            "--new", "0e0206b070a2f6242180713865768666a333f372",
            "--work-dir", os.path.join(tmp, "mirror"),
            "--summary-out", summary,
        ])
        text = open(summary, encoding="utf-8").read()
        if "Could not review" in text:
            print("  SKIP  network unavailable")
            return
        check("exit 0 (advisory)", rc == 0, f"rc={rc}")
        check("FLAGS the bbtune patch", "5-bbtune" in text,
              "the whole point of this gate")
        check("does not flag the logo patch",
              "6-logo-only" not in text.split("Patch review ages")[0])
        check("shows the upstream commit that caused it",
              "0.7 Mbit/s" in text or "upload slowdown" in text)

        # The check above fires on watch_paths chosen with hindsight (upstream
        # ADDED patches/ and scripts/build-wineserver.sh in this very commit).
        # The honest question is whether keywords alone catch it -- those are
        # derivable from bbtune's own description without knowing the future.
        manifest["patches"] = [{
            "id": "5-bbtune-keywords-only", "disposition": "retain",
            "watch_paths": [],
            "watch_keywords": ["upload", "throughput", "socket", "send buffer",
                               "sndbuf", "connection"],
            "min_keyword_hits": 2,
        }]
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        s2 = os.path.join(tmp, "summary2.md")
        pw.main(["--manifest", mpath,
                 "--old", "2e5508abdaebf1ec4d4b2044a9f770f1ef8a1c50",
                 "--new", "0e0206b070a2f6242180713865768666a333f372",
                 "--work-dir", os.path.join(tmp, "mirror"), "--summary-out", s2])
        check("KEYWORDS ALONE catch it (no hindsight paths)",
              "5-bbtune-keywords-only" in open(s2, encoding="utf-8").read().split("Patch review ages")[0],
              "gate would not have caught the real miss without hindsight")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    test_path_matching()
    test_keyword_matching()
    test_threshold_behaviour()
    test_ignore_paths()
    test_coverage_check()
    test_local_repo_end_to_end()
    if not args.offline:
        test_real_backblaze_regression()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, detail in FAIL:
        print(f"  FAILED: {name} {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
