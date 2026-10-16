#!/usr/bin/env python3
"""Resolve the upstream code to build, and decide whether a build is needed.

upstream.json says what to follow: the newest release (the default) or the tip
of a branch. The published image is the build record: every image carries
labels naming the upstream commit and hashes of the other build inputs, so a
run only builds when something differs from the image published as :latest.
Nothing is committed back to the repository.

  upstream.py resolve [--ref REF]               the upstream to build, as JSON
  upstream.py decide --image IMAGE [--ref REF]  compare with IMAGE and write GitHub outputs
  upstream.py pin --dockerfile FILE             pin FILE's base images to the digests decide saw
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CONFIG = Path("upstream.json")
LABEL = "io.crunch41"
# Files that never change what gets built.
IGNORED = re.compile(r"(^|/)[^/]*\.md$|^docs/|^LICENSE$|^\.gitattributes$|^\.gitignore$")


def run(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, timeout=120).strip()


def config() -> dict:
    data = json.loads(CONFIG.read_text())
    data.setdefault("forge", "https://github.com")
    data.setdefault("track", "release")
    data.setdefault("prereleases", False)
    data.setdefault("dockerfiles", ["Dockerfile"])
    data.setdefault("refresh_days", 0)
    data["url"] = f'{data["forge"].rstrip("/")}/{data["repository"]}.git'
    return data


def api(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "crunch41-ci"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def releases(cfg: dict) -> list[dict]:
    if cfg["forge"] == "https://github.com":
        return api(f'https://api.github.com/repos/{cfg["repository"]}/releases?per_page=100')
    # Forgejo and Gitea serve the same fields under /api/v1.
    return api(f'{cfg["forge"].rstrip("/")}/api/v1/repos/{cfg["repository"]}/releases?limit=50')


def version_key(tag: str) -> tuple:
    """Order release tags by version: v4.0.20 > v4.0.9, 1.0.0 > 1.0.0-rc.2 > 1.0.0-rc.1.

    A fix released later for an older line (say 1.18.5 after 2.0.1) must not
    win just because it was published last. Tags without a version number sort
    lowest."""
    match = re.match(r"^\D*?(\d+(?:\.\d+)*)(.*)$", tag)
    if not match:
        return ((), 0, ())
    numbers = tuple(int(n) for n in match[1].split("."))
    numbers += (0,) * (6 - len(numbers))  # 1.2 == 1.2.0
    suffix = match[2].lower()
    if not suffix.strip("-+._"):
        return (numbers, 1, ())  # a final release outranks its pre-releases
    stage = 0 if "alpha" in suffix else 2 if "rc" in suffix else 1  # alpha < beta/preview < rc
    return (numbers, 0, (stage,) + tuple(int(n) for n in re.findall(r"\d+", suffix)))


def ls_remote(url: str, ref: str) -> str | None:
    rows = run(["git", "ls-remote", url, ref, f"{ref}^{{}}"]).splitlines()
    peeled = [r.split()[0] for r in rows if r.endswith("^{}")]
    plain = [r.split()[0] for r in rows if not r.endswith("^{}")]
    return (peeled or plain or [None])[0]


def resolve(cfg: dict, ref: str | None = None) -> dict:
    """Return the upstream to build: its ref (tag or branch), commit and version."""
    if not ref:
        if cfg["track"] == "branch":
            ref = cfg["branch"]
        else:
            candidates = [r for r in releases(cfg)
                          if not r.get("draft") and (cfg["prereleases"] or not r.get("prerelease"))]
            if not candidates:
                raise SystemExit(f'No published release found for {cfg["repository"]}')
            candidates.sort(key=lambda r: (version_key(r["tag_name"]), r.get("published_at") or ""), reverse=True)
            ref = candidates[0]["tag_name"]
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        commit = ref
    else:
        commit = ls_remote(cfg["url"], f"refs/tags/{ref}") or ls_remote(cfg["url"], f"refs/heads/{ref}")
    if not commit:
        raise SystemExit(f'{ref} not found in {cfg["url"]}')
    is_tag = ref != cfg.get("branch") and not re.fullmatch(r"[0-9a-f]{40}", ref)
    version = re.sub(r"^v(?=\d)", "", ref) if is_tag else commit[:10]
    return {"repository": cfg["repository"], "url": cfg["url"], "ref": ref, "commit": commit, "version": version}


def base_images(dockerfile: str) -> list[str]:
    stages, refs = set(), []
    for line in dockerfile.splitlines():
        match = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
        if not match:
            continue
        ref, stage = match.groups()
        if "$" in ref:
            raise SystemExit(f"FROM {ref}: build arguments in FROM lines are not supported")
        if ref not in stages and ref != "scratch":
            refs.append(ref)
        if stage:
            stages.add(stage)
    return sorted(set(refs))


def dockerfile_text(path: str, upstream: dict) -> str:
    # "upstream:<path>" names a Dockerfile in the upstream repository at the commit being built.
    if path.startswith("upstream:"):
        return run(["git", "-C", "upstream", "show", f'{upstream["commit"]}:{path[9:]}']) \
            if Path("upstream/.git").exists() else _raw_upstream_file(upstream, path[9:])
    return Path(path).read_text()


def _raw_upstream_file(upstream: dict, path: str) -> str:
    if "github.com" not in upstream["url"]:
        raise SystemExit("upstream: Dockerfiles outside GitHub need the upstream checkout first")
    url = f'https://raw.githubusercontent.com/{upstream["repository"]}/{upstream["commit"]}/{path}'
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def image_digest(ref: str) -> str:
    if "@sha256:" in ref:
        return ref.split("@", 1)[1]
    output = run(["docker", "buildx", "imagetools", "inspect", ref])
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", output, re.M)
    if not match:
        raise SystemExit(f"{ref}: the registry returned no digest")
    return match.group(1)


def source_hash() -> str:
    rows = [r for r in run(["git", "ls-tree", "-r", "HEAD"]).splitlines()
            if not IGNORED.search(r.split("\t", 1)[1])]
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def digest_of(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def published_labels(image: str) -> dict:
    """Labels of the published image, or {} when nothing is published yet."""
    try:
        raw = run(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Image}}"])
    except subprocess.CalledProcessError:
        return {}
    data = json.loads(raw or "{}")
    if "config" not in data and data:  # multi-platform index: {platform: image}
        data = next(iter(data.values()))
    return (data.get("config") or {}).get("Labels") or {}


def decision(current: dict, previous: dict, event: str, refresh_days: int, now: dt.datetime) -> list[str]:
    """Why this run should build; an empty list means it shouldn't."""
    reasons = []
    if event not in ("schedule", ""):
        reasons.append(f"{event} run")
    if not previous.get(f"{LABEL}.upstream.commit"):
        reasons.append("nothing published yet")
    else:
        for key, label in (("upstream", "upstream.commit"), ("bases", "inputs.bases"), ("source", "inputs.source")):
            if current[key] != previous.get(f"{LABEL}.{label}"):
                reasons.append(f"{key} changed")
        created = previous.get("org.opencontainers.image.created")
        if refresh_days and created:
            age = now - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            if age >= dt.timedelta(days=refresh_days) - dt.timedelta(hours=12):
                reasons.append(f"image older than {refresh_days} days")
    return reasons


def write_outputs(values: dict) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for key, value in values.items():
        if "\n" in str(value):
            lines.append(f"{key}<<EOF_{key}\n{value}\nEOF_{key}")
        else:
            lines.append(f"{key}={value}")
    if target:
        with open(target, "a") as out:
            out.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["resolve", "decide", "pin"])
    parser.add_argument("--ref", default="", help="build this tag, branch or commit instead")
    parser.add_argument("--image", help="published image to compare with, e.g. ghcr.io/owner/name:latest")
    parser.add_argument("--dockerfile", help="Dockerfile to pin (pin)")
    parser.add_argument("--state", default=os.path.join(os.environ.get("RUNNER_TEMP", "."), "upstream-state.json"))
    args = parser.parse_args()
    cfg = config()

    if args.action == "resolve":
        print(json.dumps(resolve(cfg, args.ref or None), indent=2))
        return 0

    if args.action == "pin":
        bases = json.loads(Path(args.state).read_text())["bases"]
        path = Path(args.dockerfile)
        text = path.read_text()
        for ref, digest in bases.items():
            if "@" not in ref:
                text = re.sub(rf"(?mi)^(\s*FROM\s+(?:--platform=\S+\s+)?){re.escape(ref)}(?=\s|$)",
                              lambda m: f"{m[1]}{ref}@{digest}", text)
        path.write_text(text)
        return 0

    upstream = resolve(cfg, args.ref or None)
    refs = sorted({ref for path in cfg["dockerfiles"] for ref in base_images(dockerfile_text(path, upstream))})
    bases = {ref: image_digest(ref) for ref in refs}
    current = {"upstream": upstream["commit"], "bases": digest_of(bases), "source": source_hash()}
    previous = published_labels(args.image) if args.image else {}
    reasons = decision(current, previous, os.environ.get("GITHUB_EVENT_NAME", ""), cfg["refresh_days"],
                       dt.datetime.now(dt.timezone.utc))
    Path(args.state).write_text(json.dumps({"upstream": upstream, "bases": bases}, indent=2) + "\n")
    labels = "\n".join([
        f'{LABEL}.upstream.repository={upstream["repository"]}',
        f'{LABEL}.upstream.ref={upstream["ref"]}',
        f'{LABEL}.upstream.commit={upstream["commit"]}',
        f'{LABEL}.inputs.bases={current["bases"]}',
        f'{LABEL}.inputs.source={current["source"]}',
    ])
    write_outputs({
        "build": str(bool(reasons)).lower(),
        "reasons": ", ".join(reasons) or "nothing changed",
        "ref": upstream["ref"],
        "commit": upstream["commit"],
        "version": upstream["version"],
        "url": upstream["url"],
        "previous_commit": previous.get(f"{LABEL}.upstream.commit", ""),
        "labels": labels,
    })
    print(f'Upstream {upstream["ref"]} ({upstream["commit"][:10]}): '
          f'{"build: " + ", ".join(reasons) if reasons else "nothing changed, no build"}', file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
