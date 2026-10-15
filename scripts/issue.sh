#!/usr/bin/env bash
# CI issues, one open issue per title.
#   scripts/issue.sh open <title> <body-file>   open the issue, or comment on the open one
#   scripts/issue.sh close <title> <comment>    close the open issue with a comment
set -euo pipefail
action="$1" title="$2"
repo="${GITHUB_REPOSITORY:?}"
number=$(gh issue list --repo "$repo" --state open --search "\"$title\" in:title" \
  --json number,title --jq "map(select(.title == \"$title\")) | .[0].number // empty")
case "$action" in
  open)
    if [ -n "$number" ]; then
      gh issue comment "$number" --repo "$repo" --body-file "$3"
    else
      gh issue create --repo "$repo" --title "$title" --body-file "$3"
    fi ;;
  close)
    [ -z "$number" ] || gh issue close "$number" --repo "$repo" --comment "$3" ;;
  *) echo "usage: issue.sh open|close <title> <body-file|comment>" >&2; exit 2 ;;
esac
