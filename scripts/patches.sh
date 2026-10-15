#!/usr/bin/env bash
# The patch series in patches/ is a set of `git format-patch` files, applied in
# filename order as commits on top of the upstream code (upstream.json).
#
#   scripts/patches.sh fetch <commit> <dir>  clone upstream at <commit> into <dir>
#   scripts/patches.sh apply <dir>           apply every patch to <dir> as a commit
#   scripts/patches.sh prepare [ref]         fetch upstream (newest release, or ref) into
#                                            work/upstream and apply the patches, ready to edit
#   scripts/patches.sh export                write work/upstream's commits back to patches/
#
# Changing a patch: run prepare, then edit work/upstream with ordinary git (amend
# or fix up the commit that owns the change, or add a commit for a new patch),
# run export, review `git diff patches/` and commit. One commit = one patch; the
# commit subject becomes the file name.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCHES="$ROOT/patches"
WORK="$ROOT/work/upstream"
IDENTITY=(-c user.name=Crunch41 -c user.email=70764418+Crunch41@users.noreply.github.com)

py() { if python3 -c '' 2>/dev/null; then python3 "$@"; else python "$@"; fi; }
upstream_url() { (cd "$ROOT" && py -c 'import json; c=json.load(open("upstream.json")); print(c.get("forge","https://github.com").rstrip("/")+"/"+c["repository"]+".git")'); }

fetch() {
  local commit="$1" dir="$2" url
  url="$(upstream_url)"
  rm -rf "$dir"
  # A blob-less clone downloads file contents only when needed, which keeps the
  # clone small while still giving `git am -3` the blobs a 3-way merge needs.
  if ! git clone -q --filter=blob:none --no-checkout "$url" "$dir" 2>/dev/null; then
    git clone -q --no-checkout "$url" "$dir"
  fi
  git -C "$dir" -c advice.detachedHead=false checkout -q "$commit"
  git -C "$dir" update-ref refs/patches/base HEAD
}

apply() {
  # One `git am` over the whole series: on a conflict it stops at the failing
  # patch, and `git am --continue` carries on with the rest after a fix.
  local dir="$1" report="${PATCH_REPORT:-}" patches
  shopt -s nullglob
  patches=("$PATCHES"/*.patch)
  git -C "$dir" update-ref refs/patches/base HEAD
  [[ ${#patches[@]} -gt 0 ]] || { echo "No patches."; return 0; }
  if git -C "$dir" "${IDENTITY[@]}" am -3 --quiet "${patches[@]}"; then
    echo "Applied ${#patches[@]} patches to upstream $(git -C "$dir" rev-parse --short=10 refs/patches/base)."
    return 0
  fi
  local index name conflicts
  index="$(cat "$(git -C "$dir" rev-parse --absolute-git-dir)/rebase-apply/next")"
  name="$(basename "${patches[$((index - 1))]}")"
  conflicts="$(git -C "$dir" diff --name-only --diff-filter=U)"
  echo "::error::$name no longer applies to upstream $(git -C "$dir" rev-parse --short=10 refs/patches/base)"
  {
    echo "### \`$name\` no longer applies"
    echo
    echo "Upstream changed code this patch edits. Files with conflicts:"
    echo
    printf -- '- `%s`\n' ${conflicts:-"(none: the code the patch expects is gone)"}
    echo
    echo "<details><summary>Conflicts</summary>"
    echo
    echo '```diff'
    git -C "$dir" diff | head -c 60000
    echo '```'
    echo "</details>"
    echo
    echo "To fix: \`scripts/patches.sh prepare\` stops at this patch; resolve it in \`work/upstream\`,"
    echo "run \`git -C work/upstream am --continue\`, then \`scripts/patches.sh export\` and commit."
  } | tee ${report:+"$report"}
  [[ -n "${KEEP_CONFLICT:-}" ]] || git -C "$dir" "${IDENTITY[@]}" am --abort
  return 1
}

prepare() {
  local ref="${1:-}" upstream commit
  upstream="$(cd "$ROOT" && py scripts/upstream.py resolve ${ref:+--ref "$ref"})"
  commit="$(printf '%s' "$upstream" | py -c 'import json,sys; print(json.load(sys.stdin)["commit"])')"
  echo "Upstream: $(printf '%s' "$upstream" | py -c 'import json,sys; u=json.load(sys.stdin); print(u["repository"], u["ref"])')"
  fetch "$commit" "$WORK"
  KEEP_CONFLICT=1 apply "$WORK" || { echo "Resolve the conflicts in work/upstream, then: git -C work/upstream am --continue" >&2; return 1; }
  echo "Ready: edit work/upstream, then run scripts/patches.sh export"
}

export_patches() {
  [[ -d "$WORK/.git" ]] || { echo "Run scripts/patches.sh prepare first." >&2; exit 1; }
  local tmp n=0 file subject
  tmp="$(mktemp -d)"
  git -C "$WORK" format-patch -q --full-index --zero-commit --no-signature --no-numbered \
    -o "$tmp" refs/patches/base..HEAD
  rm -f "$PATCHES"/*.patch
  mkdir -p "$PATCHES"
  for file in "$tmp"/*.patch; do
    n=$((n + 1))
    # File name: number plus the commit subject, cut at a word boundary.
    subject="$(sed -n 's/^Subject: \[PATCH\] //p' "$file" | head -1 | tr '[:upper:]' '[:lower:]' \
      | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')"
    if (( ${#subject} > 50 )); then subject="${subject:0:50}"; subject="${subject%-*}"; fi
    mv "$file" "$PATCHES/$(printf '%03d' "$n")-$subject.patch"
  done
  rm -rf "$tmp"
  echo "Exported $n patches. Patch ids in patch-watch.json must match the file names:"
  ls "$PATCHES"
}

case "${1:-}" in
  fetch) fetch "$2" "$3" ;;
  apply) apply "$2" ;;
  prepare) prepare "${2:-}" ;;
  export) export_patches ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
