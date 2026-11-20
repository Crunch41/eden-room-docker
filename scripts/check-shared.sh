#!/usr/bin/env bash
# The scripts listed in scripts/shared.sha256 are shared by the Crunch41 patch
# repositories as identical copies. This fails if a copy in this repository
# differs from the listed hash. Files a repository doesn't use are skipped.
#
# To change a shared file: change it in one repository and test it, update its
# line in shared.sha256 (sha256sum <file>), then copy the file and
# shared.sha256 to the other repositories.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

checked=0 bad=0
while read -r hash path; do
  [[ -z "$hash" || "$hash" == \#* ]] && continue
  path="${path#\*}"  # sha256sum on Windows marks binary mode with *
  [[ -f "$path" ]] || continue
  actual="$(sha256sum "$path" | cut -d' ' -f1)"
  if [[ "$actual" != "$hash" ]]; then
    echo "::error file=$path::$path differs from the shared copy in scripts/shared.sha256"
    bad=$((bad + 1))
  fi
  checked=$((checked + 1))
done < scripts/shared.sha256

if (( bad )); then
  echo "$bad of $checked shared file(s) differ."
  exit 1
fi
echo "All $checked shared file(s) match scripts/shared.sha256."
