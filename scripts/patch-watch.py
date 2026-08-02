#!/usr/bin/env python3
"""Flag patches that upstream may have made redundant.

The existing CI gates answer "does this patch still apply". They cannot answer
"does this patch still do anything", because a patch that upstream has
independently obsoleted keeps applying cleanly forever. This script closes that
gap: on every sync it reads the upstream commits between the last built marker
and the new HEAD, and matches them against per-patch watch rules declared in
patch-watch.json.

It is deliberately advisory. A false positive must never block a nightly image
build, so findings are reported (step summary, ::warning::, and a findings file
the workflow turns into a tracking issue) and the build carries on. Set
"strict": true in the manifest to exit non-zero on findings instead.

Usage:
  patch-watch.py --manifest patch-watch.json --old <sha> --new <sha>
                 [--upstream <name>] [--work-dir <dir>]
                 [--findings-out <path>] [--summary-out <path>]

Exit codes:
  0  no findings, or findings in advisory mode
  1  findings and the manifest asked for strict mode
  2  bad usage / unreadable manifest (never a patch verdict)

Infrastructure failures (upstream unreachable, unknown shas, shallow history)
report a warning and exit 0. Failing a publish because a mirror was briefly
unreachable would train exactly the habit this script exists to prevent.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

RECORD = "\x1e"
FIELD = "\x1f"


class Skip(Exception):
    """Recoverable problem: report it, do not fail the build."""


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    files: list[str] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:9]

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body}"


@dataclass
class Finding:
    patch_id: str
    commit: Commit
    reason: str
    matched: list[str]


def run_git(args: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise Skip(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def ensure_mirror(url: str, work_dir: str) -> str:
    """Blobless bare mirror of upstream. Enough for messages and file names."""
    if os.path.isdir(os.path.join(work_dir, "objects")):
        # A stale mirror is still usable if it has both endpoints.
        with contextlib.suppress(Skip):
            run_git(["fetch", "--quiet", "--force", "origin", "+refs/*:refs/*"], cwd=work_dir)
        return work_dir
    os.makedirs(os.path.dirname(work_dir) or ".", exist_ok=True)
    run_git(["clone", "--quiet", "--filter=blob:none", "--bare", url, work_dir])
    return work_dir


def read_commits(repo: str, old: str, new: str) -> list[Commit]:
    for sha in (old, new):
        try:
            run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
        except Skip as exc:
            raise Skip(
                f"commit {sha[:9]} is not in the upstream mirror "
                "(force-push, rebased branch, or shallow history)"
            ) from exc

    rng = f"{old}..{new}"
    out = run_git(["log", f"--format={RECORD}%H{FIELD}%s{FIELD}%b", rng], cwd=repo)
    commits: dict[str, Commit] = {}
    order: list[str] = []
    for chunk in out.split(RECORD):
        if not chunk.strip():
            continue
        parts = chunk.split(FIELD)
        sha = parts[0].strip()
        subject = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        commits[sha] = Commit(sha=sha, subject=subject, body=body)
        order.append(sha)

    # Second pass for changed paths. Merge commits list no files against their
    # first parent, which is what we want: the work shows up in the merged
    # commits themselves.
    out = run_git(["log", "--name-only", f"--format={RECORD}%H", rng], cwd=repo)
    for chunk in out.split(RECORD):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        sha, files = lines[0], lines[1:]
        if sha in commits:
            commits[sha].files = files

    return [commits[s] for s in order]


def path_matches(file_path: str, pattern: str) -> bool:
    fp = file_path.replace("\\", "/").lstrip("./")
    pat = pattern.replace("\\", "/").lstrip("./")
    if any(ch in pat for ch in "*?["):
        return fnmatch.fnmatch(fp, pat) or fnmatch.fnmatch(os.path.basename(fp), pat)
    if pat.endswith("/"):
        return fp.startswith(pat)
    return fp == pat or fp.startswith(pat + "/") or fp.endswith("/" + pat)


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    hits = []
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        # Word-boundary match so "poll" does not fire on "polling" only by luck,
        # but multi-word phrases still match literally.
        pattern = r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, lowered):
            hits.append(kw)
    return hits


def evaluate(
    patches: list[dict], commits: list[Commit], ignore_paths: list[str] | None = None
) -> list[Finding]:
    """Match upstream commits against each patch's watch rules.

    ignore_paths exists because aggregate/generated files (locale bundles, API
    schemas, lockfiles) get touched by almost every commit while carrying no
    signal about intent. Counting them buries the real findings.
    """
    ignore_paths = ignore_paths or []
    findings: list[Finding] = []
    for patch in patches:
        pid = patch.get("id") or "(unnamed patch)"
        if patch.get("disposition") == "drop":
            continue
        watch_paths = patch.get("watch_paths") or []
        watch_keywords = patch.get("watch_keywords") or []
        min_hits = int(patch.get("min_keyword_hits", 2))

        for commit in commits:
            hit_paths = [
                f for f in commit.files
                if any(path_matches(f, p) for p in watch_paths)
                and not any(path_matches(f, ig) for ig in ignore_paths)
            ]
            if hit_paths:
                findings.append(
                    Finding(pid, commit, "touches a watched path", sorted(set(hit_paths))[:8])
                )
                continue
            hits = keyword_hits(commit.text, watch_keywords)
            if len(hits) >= min_hits:
                findings.append(
                    Finding(pid, commit, f"matches {len(hits)} watched keywords", hits)
                )
    return findings


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", "").strip()


def build_report(
    upstream_name: str,
    upstream_url: str,
    old: str,
    new: str,
    commits: list[Commit],
    findings: list[Finding],
    patches: list[dict],
) -> str:
    lines: list[str] = []
    lines.append(f"## Upstream sync review — `{upstream_name}`")
    lines.append("")
    lines.append(f"`{old[:9]}` → `{new[:9]}` &nbsp;·&nbsp; {len(commits)} commit(s) &nbsp;·&nbsp; {upstream_url}")
    lines.append("")

    if not commits:
        lines.append("No new upstream commits.")
        return "\n".join(lines)

    lines.append("### What upstream changed")
    lines.append("")
    lines.append("| Commit | Subject |")
    lines.append("|---|---|")
    for c in commits[:80]:
        lines.append(f"| `{c.short}` | {md_escape(c.subject)} |")
    if len(commits) > 80:
        lines.append(f"| … | _and {len(commits) - 80} more_ |")
    lines.append("")

    if findings:
        lines.append(f"### ⚠️ {len(findings)} patch/commit pair(s) need a human look")
        lines.append("")
        lines.append("A patch that upstream has already fixed another way keeps applying cleanly forever. These upstream commits overlap with what a patch claims to do — confirm the patch is still earning its place.")
        lines.append("")
        lines.append("| Patch | Upstream commit | Why flagged | Matched |")
        lines.append("|---|---|---|---|")
        for f in findings:
            lines.append(
                f"| `{f.patch_id}` | `{f.commit.short}` {md_escape(f.commit.subject)[:70]} "
                f"| {f.reason} | {md_escape(', '.join(f.matched))} |"
            )
        lines.append("")
    else:
        lines.append("### ✅ No patch overlap detected")
        lines.append("")
        lines.append("No upstream commit touched a watched path or matched enough watched keywords.")
        lines.append("")

    stale = [
        p for p in patches
        if p.get("reviewed_against") and not str(new).startswith(str(p["reviewed_against"]))
        and p.get("disposition") != "drop"
    ]
    if stale:
        lines.append("<details><summary>Patch review ages</summary>")
        lines.append("")
        lines.append("| Patch | Disposition | Last reviewed against |")
        lines.append("|---|---|---|")
        for p in patches:
            if p.get("disposition") == "drop":
                continue
            lines.append(
                f"| `{p.get('id')}` | {p.get('disposition', '?')} | "
                f"`{str(p.get('reviewed_against', '—'))[:9]}` |"
            )
        lines.append("")
        lines.append("Bump `reviewed_against` in `patch-watch.json` when you have re-confirmed a patch is still needed.")
        lines.append("</details>")

    return "\n".join(lines)


def check_coverage(manifest: dict, patches_dir: str) -> int:
    """Every patch file must have watch rules, or the gate has a blind spot."""
    try:
        on_disk = sorted(
            f for f in os.listdir(patches_dir) if f.endswith(".patch")
        )
    except OSError as exc:
        print(f"::error::cannot list {patches_dir}: {exc}", file=sys.stderr)
        return 2

    declared = {p.get("id") for p in manifest.get("patches", [])}
    missing = []
    for fname in on_disk:
        stem = fname[: -len(".patch")]
        if stem not in declared:
            missing.append(fname)

    orphan = [
        pid for pid in declared
        if pid and f"{pid}.patch" not in on_disk
    ]

    for fname in missing:
        print(
            f"::error::{fname} has no entry in patch-watch.json — add watch rules "
            "so the intent gate can flag upstream work that may supersede it."
        )
    for pid in orphan:
        print(f"::warning::patch-watch.json declares '{pid}' but no such patch file exists")

    if missing:
        return 1
    print(f"patch-watch: all {len(on_disk)} patch file(s) have watch rules.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--old", default=None)
    ap.add_argument("--new", default=None)
    ap.add_argument("--check-coverage", default=None, metavar="PATCHES_DIR",
                    help="verify every .patch file has watch rules, then exit")
    ap.add_argument("--upstream", default=None, help="upstream key in the manifest")
    ap.add_argument("--work-dir", default=".patch-watch-mirror")
    ap.add_argument("--findings-out", default=None)
    ap.add_argument("--summary-out", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    ap.add_argument("--upstream-url", default=None, help="override the manifest URL")
    args = ap.parse_args(argv)

    # The report is UTF-8. CI runners are too, but a developer console may not
    # be, and a mojibake traceback must not look like a patch verdict.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::cannot read {args.manifest}: {exc}", file=sys.stderr)
        return 2

    if args.check_coverage:
        return check_coverage(manifest, args.check_coverage)

    if not args.old or not args.new:
        print("::error::--old and --new are required unless --check-coverage is used",
              file=sys.stderr)
        return 2

    upstreams = manifest.get("upstreams") or {}
    name = args.upstream or (next(iter(upstreams)) if upstreams else "upstream")
    entry = upstreams.get(name, {})
    url = args.upstream_url or entry.get("url")
    if not url:
        print(f"::error::no upstream URL for '{name}' in {args.manifest}", file=sys.stderr)
        return 2

    def applies(patch: dict) -> bool:
        """A patch with no explicit "upstream" is checked against every upstream.

        For a fork tracking two parents, either one could independently make a
        local fix redundant, so "unspecified" must mean "all", not "the first".
        """
        want = patch.get("upstream")
        if want is None or want == "*":
            return True
        if isinstance(want, list):
            return name in want
        return want == name

    patches = [p for p in manifest.get("patches", []) if applies(p)]

    old, new = args.old.strip(), args.new.strip()
    report: str
    findings: list[Finding] = []

    if not old or old == new:
        report = (
            f"## Upstream sync review — `{name}`\n\n"
            + ("No previous marker recorded; nothing to compare.\n" if not old
               else "Upstream unchanged since the last build.\n")
        )
    else:
        try:
            mirror = ensure_mirror(url, args.work_dir)
            commits = read_commits(mirror, old, new)
            findings = evaluate(patches, commits, manifest.get("ignore_paths"))
            report = build_report(name, url, old, new, commits, findings, patches)
        except Skip as exc:
            print(f"::warning::patch-watch skipped for {name}: {exc}")
            report = (
                f"## Upstream sync review — `{name}`\n\n"
                f"⚠️ Could not review this range: {exc}\n\n"
                "The build was not blocked. Re-run after the mirror settles, or "
                "check the range by hand.\n"
            )

    if args.summary_out:
        try:
            with open(args.summary_out, "a", encoding="utf-8") as fh:
                fh.write(report + "\n\n")
        except OSError as exc:
            print(f"::warning::could not write summary: {exc}")

    print(report)

    for f in findings:
        print(
            f"::warning::patch-watch: '{f.patch_id}' may be superseded by upstream "
            f"{f.commit.short} ({f.commit.subject[:90]}) — {f.reason}: {', '.join(f.matched)}"
        )

    if args.findings_out:
        with open(args.findings_out, "w", encoding="utf-8") as fh:
            if findings:
                fh.write(report)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"findings={'true' if findings else 'false'}\n")
                fh.write(f"findings_count={len(findings)}\n")

    if findings and manifest.get("strict"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
