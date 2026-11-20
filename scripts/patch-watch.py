#!/usr/bin/env python3
"""Flag upstream commits that may make a patch unnecessary.

A patch that upstream has fixed another way can keep applying cleanly, so
"does it still apply" is not enough. This script reads the upstream commits
between the last build and the new upstream commit and matches them against
each patch's watch rules in patch-watch.json (watched paths and keywords).

Commits that a patch's `reviewed_against` commit already contains are skipped
for that patch: they were checked when the patch was last reviewed.

Findings are advisory: they go to the step summary, as ::warning:: lines and
to a findings file that the workflow turns into an issue, and the build
carries on. Set "strict": true in the manifest to exit 1 on findings instead.

Usage:
  patch-watch.py --manifest patch-watch.json --old <sha> --new <sha>
                 [--upstream <name>] [--work-dir <dir>]
                 [--findings-out <path>] [--summary-out <path>]
  patch-watch.py --manifest patch-watch.json --check-coverage patches/

Exit codes:
  0  no findings, or findings in advisory mode
  1  findings in strict mode, or a patch file without an entry
  2  bad usage or unreadable manifest

If upstream can't be read (unreachable, unknown commits, shallow history), the
script prints a warning and exits 0, so a network problem never fails a build.
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
    """Upstream couldn't be read: warn and carry on."""


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
    """Blob-less bare mirror of upstream: enough for messages and file names."""
    if os.path.isdir(os.path.join(work_dir, "objects")):
        # An older mirror still works if it has both commits.
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

    # Changed paths. Merge commits list no files against their first parent,
    # which is fine: the work shows up in the merged commits themselves.
    out = run_git(["log", "--name-only", f"--format={RECORD}%H", rng], cwd=repo)
    for chunk in out.split(RECORD):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        sha, files = lines[0], lines[1:]
        if sha in commits:
            commits[sha].files = files

    return [commits[s] for s in order]


def reviewed_commits(repo: str, old: str, patches: list[dict]) -> dict[str, set[str]]:
    """For each patch, the commits after `old` that its reviewed_against contains."""
    reviewed: dict[str, set[str]] = {}
    for patch in patches:
        ref = str(patch.get("reviewed_against") or "").strip()
        if not ref:
            continue
        try:
            sha = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo).strip()
            out = run_git(["rev-list", f"{old}..{sha}"], cwd=repo)
        except Skip:
            continue  # unknown commit: review nothing away
        reviewed[patch.get("id") or ""] = set(out.split())
    return reviewed


def normalise(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def path_matches(file_path: str, pattern: str) -> bool:
    fp = normalise(file_path)
    pat = normalise(pattern)
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
        # Whole words only; a phrase matches with any whitespace between words.
        pattern = r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, lowered):
            hits.append(kw)
    return hits


def evaluate(
    patches: list[dict],
    commits: list[Commit],
    ignore_paths: list[str] | None = None,
    reviewed: dict[str, set[str]] | None = None,
) -> list[Finding]:
    """Match upstream commits against each patch's watch rules.

    ignore_paths: generated or shared files (locale bundles, API schemas, lock
    files) that most commits touch; they are never evidence on their own.
    reviewed: per patch id, commits already covered by its last review.
    """
    ignore_paths = ignore_paths or []
    reviewed = reviewed or {}
    findings: list[Finding] = []
    for patch in patches:
        pid = patch.get("id") or "(unnamed patch)"
        if patch.get("disposition") == "drop":
            continue
        watch_paths = patch.get("watch_paths") or []
        watch_keywords = patch.get("watch_keywords") or []
        min_hits = int(patch.get("min_keyword_hits", 2))
        # busy_paths: large shared files that most upstream commits touch. A
        # commit touching only those counts when it also matches a keyword.
        busy_paths = patch.get("busy_paths") or []
        done = reviewed.get(pid, set())

        for commit in commits:
            if commit.sha in done:
                continue
            hit_paths = [
                f for f in commit.files
                if any(path_matches(f, p) for p in watch_paths)
                and not any(path_matches(f, ig) for ig in ignore_paths)
            ]
            hits = keyword_hits(commit.text, watch_keywords)
            quiet = [f for f in hit_paths if not any(path_matches(f, b) for b in busy_paths)]
            if quiet or (hit_paths and hits):
                findings.append(
                    Finding(pid, commit, "touches a watched path", sorted(set(hit_paths))[:8])
                )
                continue
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
    lines.append(f"## Upstream changes: `{upstream_name}`")
    lines.append("")
    lines.append(f"`{old[:9]}` to `{new[:9]}`, {len(commits)} commit(s), {upstream_url}")
    lines.append("")

    if not commits:
        lines.append("No new upstream commits.")
        return "\n".join(lines)

    lines.append("### Upstream commits")
    lines.append("")
    lines.append("| Commit | Subject |")
    lines.append("|---|---|")
    for c in commits[:80]:
        lines.append(f"| `{c.short}` | {md_escape(c.subject)} |")
    if len(commits) > 80:
        lines.append(f"| ... | and {len(commits) - 80} more |")
    lines.append("")

    if findings:
        lines.append(f"### Patches to review: {len(findings)} finding(s)")
        lines.append("")
        lines.append(
            "These upstream commits touch files or topics a patch depends on. Check whether "
            "each patch is still needed, then set its `reviewed_against` in patch-watch.json "
            "to the upstream commit you checked."
        )
        lines.append("")
        lines.append("| Patch | Upstream commit | Why | Matched |")
        lines.append("|---|---|---|---|")
        for f in findings:
            lines.append(
                f"| `{f.patch_id}` | `{f.commit.short}` {md_escape(f.commit.subject)[:70]} "
                f"| {f.reason} | {md_escape(', '.join(f.matched))} |"
            )
        lines.append("")
    else:
        lines.append("### No findings")
        lines.append("")
        lines.append("No upstream commit touched a watched path or matched enough watched keywords.")
        lines.append("")

    live = [p for p in patches if p.get("disposition") != "drop"]
    if any(p.get("reviewed_against") and not str(new).startswith(str(p["reviewed_against"])) for p in live):
        lines.append("<details><summary>Last review per patch</summary>")
        lines.append("")
        lines.append("| Patch | Disposition | Reviewed against |")
        lines.append("|---|---|---|")
        for p in live:
            lines.append(
                f"| `{p.get('id')}` | {p.get('disposition', '?')} | "
                f"`{str(p.get('reviewed_against', '-'))[:9]}` |"
            )
        lines.append("</details>")

    return "\n".join(lines)


def check_coverage(manifest: dict, patches_dir: str) -> int:
    """Every patch file needs an entry in patch-watch.json."""
    try:
        on_disk = sorted(f for f in os.listdir(patches_dir) if f.endswith(".patch"))
    except OSError as exc:
        print(f"::error::cannot list {patches_dir}: {exc}", file=sys.stderr)
        return 2

    declared = {p.get("id") for p in manifest.get("patches", [])}
    missing = [f for f in on_disk if f[: -len(".patch")] not in declared]
    orphan = [pid for pid in declared if pid and f"{pid}.patch" not in on_disk]

    for fname in missing:
        print(f"::error::{fname} has no entry in patch-watch.json")
    for pid in orphan:
        print(f"::warning::patch-watch.json has an entry for '{pid}' but no such patch file")

    if missing:
        return 1
    print(f"patch-watch: all {len(on_disk)} patch file(s) have an entry.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--old", default=None)
    ap.add_argument("--new", default=None)
    ap.add_argument("--check-coverage", default=None, metavar="PATCHES_DIR",
                    help="check every .patch file has an entry, then exit")
    ap.add_argument("--upstream", default=None, help="upstream key in the manifest")
    ap.add_argument("--work-dir", default=".patch-watch-mirror")
    ap.add_argument("--findings-out", default=None)
    ap.add_argument("--summary-out", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    ap.add_argument("--upstream-url", default=None, help="override the manifest URL")
    args = ap.parse_args(argv)

    # The report is UTF-8; a developer console may not be.
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
        """A patch without an "upstream" key is checked against every upstream."""
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
            f"## Upstream changes: `{name}`\n\n"
            + ("No previous build recorded; nothing to compare.\n" if not old
               else "Upstream unchanged since the last build.\n")
        )
    else:
        try:
            mirror = ensure_mirror(url, args.work_dir)
            commits = read_commits(mirror, old, new)
            reviewed = reviewed_commits(mirror, old, patches)
            findings = evaluate(patches, commits, manifest.get("ignore_paths"), reviewed)
            report = build_report(name, url, old, new, commits, findings, patches)
        except Skip as exc:
            print(f"::warning::patch-watch skipped for {name}: {exc}")
            report = (
                f"## Upstream changes: `{name}`\n\n"
                f"Could not check this range: {exc}\n\n"
                "The build carried on. Run it again later, or check the range by hand.\n"
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
            f"::warning::patch-watch: '{f.patch_id}' may be affected by upstream "
            f"{f.commit.short} ({f.commit.subject[:90]}): {f.reason}: {', '.join(f.matched)}"
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
