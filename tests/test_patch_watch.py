#!/usr/bin/env python3
"""Tests for scripts/patch-watch.py.

Everything runs against small local git repos, so the tests need no network
and don't depend on the machine's git configuration.

Run:  python3 tests/test_patch_watch.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "patch-watch.py"))
spec = importlib.util.spec_from_file_location("patch_watch", _SCRIPT)
pw = importlib.util.module_from_spec(spec)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules["patch_watch"] = pw
spec.loader.exec_module(pw)

# Keep the tests independent of the machine's git settings and hooks; this
# environment is inherited by patch-watch's own git calls too.
os.environ.update({
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
})

PASS, FAIL = [], []


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def git(args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo, path, text, message) -> str:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)
    git(["add", "-A"], repo)
    git(["commit", "-qm", message], repo)
    return git(["rev-parse", "HEAD"], repo)


def write_manifest(path, manifest):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


# ------------------------------------------------------------------- paths

def test_path_matching():
    print("\n[path matching]")
    check("exact file", pw.path_matches("src/net/socket.c", "src/net/socket.c"))
    check("dir prefix", pw.path_matches("src/net/socket.c", "src/net/"))
    check("dir without slash", pw.path_matches("src/net/socket.c", "src/net"))
    check("glob", pw.path_matches("src/Api/Foo.cs", "src/Api/*.cs"))
    check("basename glob", pw.path_matches("a/b/CustomFormat.cs", "CustomFormat.cs"))
    check("no false prefix", not pw.path_matches("src/net/socket_other.c", "src/net/socket.c"))
    check("unrelated", not pw.path_matches("docs/README.md", "src/"))
    check("leading ./ removed", pw.path_matches("./src/x.go", "src/x.go"))
    check("dot directory kept", pw.path_matches(".github/workflows/build.yml", ".github/"))
    check("dot directory not confused", not pw.path_matches("github/x", ".github/"))


# ---------------------------------------------------------------- keywords

def test_keyword_matching():
    print("\n[keyword matching]")
    hits = pw.keyword_hits("Fix upload throughput on slow sockets", ["upload", "throughput", "gui"])
    check("counts distinct hits", sorted(hits) == ["throughput", "upload"], str(hits))
    check("case insensitive", pw.keyword_hits("UPLOAD fixed", ["upload"]) == ["upload"])
    check("word boundary", pw.keyword_hits("preloader changes", ["load"]) == [])
    check("multiword phrase", pw.keyword_hits("the send buffer is pinned", ["send buffer"]) == ["send buffer"])
    check("no match", pw.keyword_hits("update translations", ["socket", "upload"]) == [])


# ---------------------------------------------------------------- evaluate

def test_threshold_behaviour():
    print("\n[threshold]")
    c = pw.Commit("a" * 40, "improve upload speed", "", [])
    two = [{"id": "p", "watch_keywords": ["upload", "socket"], "min_keyword_hits": 2}]
    check("one keyword is below the threshold", pw.evaluate(two, [c]) == [])
    c2 = pw.Commit("b" * 40, "improve upload socket speed", "", [])
    check("two keywords reach it", len(pw.evaluate(two, [c2])) == 1)
    one = [{"id": "p", "watch_keywords": ["upload"], "min_keyword_hits": 1}]
    check("min_keyword_hits=1 honoured", len(pw.evaluate(one, [c])) == 1)
    dropped = [{"id": "p", "disposition": "drop", "watch_keywords": ["upload"], "min_keyword_hits": 1}]
    check("disposition=drop is skipped", pw.evaluate(dropped, [c]) == [])
    pathp = [{"id": "p", "watch_paths": ["src/net/socket.c"]}]
    c3 = pw.Commit("c" * 40, "unrelated subject", "", ["src/net/socket.c"])
    check("a path hit alone is enough", len(pw.evaluate(pathp, [c3])) == 1)
    c4 = pw.Commit("d" * 40, "unrelated subject", "", ["docs/x.md"])
    check("no path hit, no keywords", pw.evaluate(pathp, [c4]) == [])


def test_reviewed_filter():
    print("\n[reviewed commits]")
    pathp = [{"id": "p", "watch_paths": ["src/x.go"]}]
    c = pw.Commit("e" * 40, "change x", "", ["src/x.go"])
    check("reviewed commit is skipped", pw.evaluate(pathp, [c], reviewed={"p": {c.sha}}) == [])
    check("other patches' reviews don't count", len(pw.evaluate(pathp, [c], reviewed={"q": {c.sha}})) == 1)


def test_ignore_paths():
    print("\n[ignore_paths]")
    patch = [{"id": "p", "watch_paths": ["src/i18n/locale/en.json", "src/real.ts"]}]
    noisy = pw.Commit("e" * 40, "chore: translations", "", ["src/i18n/locale/en.json"])
    check("ignored path does not fire", pw.evaluate(patch, [noisy], ["src/i18n/locale/*.json"]) == [])
    check("same commit fires without the ignore list", len(pw.evaluate(patch, [noisy])) == 1)
    mixed = pw.Commit("f" * 40, "feat", "", ["src/i18n/locale/en.json", "src/real.ts"])
    got = pw.evaluate(patch, [mixed], ["src/i18n/locale/*.json"])
    check("real path still fires next to an ignored one", len(got) == 1)
    check("ignored file left out of the evidence", got and "en.json" not in " ".join(got[0].matched))


def test_busy_paths():
    print("\n[busy_paths]")
    patch = [{"id": "p", "watch_paths": ["src/server.cpp", "src/clipboard/"], "busy_paths": ["src/server.cpp"],
              "watch_keywords": ["clipboard", "paste"], "min_keyword_hits": 2}]
    busy = pw.Commit("1" * 40, "add a calibration probe", "", ["src/server.cpp"])
    check("busy path alone does not fire", pw.evaluate(patch, [busy]) == [])
    hinted = pw.Commit("2" * 40, "fix clipboard request limits", "", ["src/server.cpp"])
    check("busy path with one keyword fires", len(pw.evaluate(patch, [hinted])) == 1)
    quiet = pw.Commit("3" * 40, "refactor", "", ["src/clipboard/x.h"])
    check("a quiet watched path still fires alone", len(pw.evaluate(patch, [quiet])) == 1)
    plain = [{k: v for k, v in patch[0].items() if k != "busy_paths"}]
    check("without busy_paths every watched path fires", len(pw.evaluate(plain, [busy])) == 1)


def test_coverage_check():
    print("\n[coverage check]")
    tmp = tempfile.mkdtemp(prefix="pw-cov-")
    try:
        pdir = os.path.join(tmp, "patches")
        os.makedirs(pdir)
        for n in ("001-alpha.patch", "002-beta.patch"):
            open(os.path.join(pdir, n), "w").close()
        check("complete manifest passes",
              pw.check_coverage({"patches": [{"id": "001-alpha"}, {"id": "002-beta"}]}, pdir) == 0)
        check("missing entry fails", pw.check_coverage({"patches": [{"id": "001-alpha"}]}, pdir) == 1)
        orphaned = {"patches": [{"id": "001-alpha"}, {"id": "002-beta"}, {"id": "003-ghost"}]}
        check("orphan entry warns but passes", pw.check_coverage(orphaned, pdir) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------- local repository

def test_end_to_end():
    print("\n[end to end, local repository]")
    tmp = tempfile.mkdtemp(prefix="pw-test-")
    try:
        up = os.path.join(tmp, "upstream")
        os.makedirs(up)
        git(["init", "-q", "-b", "main"], up)
        base = commit(up, "README.md", "base\n", "initial")
        commit(up, "docs.md", "docs\n", "Tidy the docs wording")
        reviewed = commit(up, "src/net/socket.c", "int a;\n", "Rework socket write notification")
        head = commit(up, "src/net/socket.c", "int b;\n", "Handle partial socket writes")

        mpath = os.path.join(tmp, "patch-watch.json")
        manifest = {
            "upstreams": {"main": {"url": up}},
            "patches": [
                {"id": "socket-patch", "disposition": "retain",
                 "watch_paths": ["src/net/socket.c"], "watch_keywords": []},
                {"id": "unrelated-patch", "disposition": "retain",
                 "watch_paths": ["gui/"], "watch_keywords": ["translation"]},
            ],
        }
        write_manifest(mpath, manifest)

        summary = os.path.join(tmp, "summary.md")
        findings = os.path.join(tmp, "findings.md")
        rc = pw.main(["--manifest", mpath, "--old", base, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror"),
                      "--summary-out", summary, "--findings-out", findings])
        text = read(summary)
        check("exit 0 in advisory mode", rc == 0, f"rc={rc}")
        check("flags the socket patch", "socket-patch" in text)
        check("does not flag the unrelated patch", "unrelated-patch" not in text.split("Last review per patch")[0])
        check("lists the upstream commits", "Tidy the docs wording" in text and "Handle partial socket writes" in text)
        check("writes the findings file", os.path.getsize(findings) > 0)
        check("report has no emoji", all(ord(ch) < 0x2190 for ch in text), "non-ASCII symbol in the report")

        manifest["patches"][0]["reviewed_against"] = reviewed[:9]
        write_manifest(mpath, manifest)
        s_rev = os.path.join(tmp, "s-reviewed.md")
        pw.main(["--manifest", mpath, "--old", base, "--new", head,
                 "--work-dir", os.path.join(tmp, "mirror"), "--summary-out", s_rev])
        text = read(s_rev).split("Last review per patch")[0]
        check("reviewed commit is not flagged again", "Rework socket write notification" not in text.split("### Patches to review")[-1])
        check("a later commit is still flagged", "socket-patch" in text and "Handle partial socket writes" in text)

        manifest["patches"][0]["reviewed_against"] = head
        write_manifest(mpath, manifest)
        s_all = os.path.join(tmp, "s-all.md")
        pw.main(["--manifest", mpath, "--old", base, "--new", head,
                 "--work-dir", os.path.join(tmp, "mirror"), "--summary-out", s_all])
        check("reviewed against the new commit: no findings", "### No findings" in read(s_all))

        del manifest["patches"][0]["reviewed_against"]
        manifest["strict"] = True
        write_manifest(mpath, manifest)
        rc = pw.main(["--manifest", mpath, "--old", base, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror2"),
                      "--summary-out", os.path.join(tmp, "s2.md")])
        check("strict mode exits 1", rc == 1, f"rc={rc}")

        manifest["strict"] = False
        manifest["patches"] = [{"id": "quiet", "watch_paths": ["nothing/"], "watch_keywords": []}]
        write_manifest(mpath, manifest)
        s3 = os.path.join(tmp, "s3.md")
        rc = pw.main(["--manifest", mpath, "--old", base, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror3"), "--summary-out", s3])
        check("clean range exits 0", rc == 0)
        check("clean range says no findings", "### No findings" in read(s3))

        s4 = os.path.join(tmp, "s4.md")
        rc = pw.main(["--manifest", mpath, "--old", "f" * 40, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror4"), "--summary-out", s4])
        check("unknown commit exits 0", rc == 0, f"rc={rc}")
        check("unknown commit is explained", "Could not check this range" in read(s4))

        s5 = os.path.join(tmp, "s5.md")
        rc = pw.main(["--manifest", mpath, "--old", head, "--new", head,
                      "--work-dir", os.path.join(tmp, "mirror5"), "--summary-out", s5])
        check("same commit exits 0", rc == 0)
        check("same commit says unchanged", "unchanged" in read(s5))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [test_path_matching, test_keyword_matching, test_threshold_behaviour, test_reviewed_filter,
             test_ignore_paths, test_busy_paths, test_coverage_check, test_end_to_end]
    for test in tests:
        # patch-watch prints GitHub annotations (::error::, ::warning::) for the
        # cases these tests cause on purpose. Keep them out of the log; show a
        # failing test's output with the annotations defused.
        output, failures = io.StringIO(), len(FAIL)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            test()
        if len(FAIL) > failures:
            print(output.getvalue().replace("::", ": :"))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, detail in FAIL:
        print(f"  FAILED: {name} {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
