#!/usr/bin/env bash
# Syntax gate for EngageMate's tracked shell and Python files.
# Run pytest and the app tests/build separately; this checks parsing only.
#
# Portable to bash 3.2 (macOS): no mapfile, no readarray.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1

status=0

while IFS= read -r f; do
    [ -f "$f" ] || continue
    if ! bash -n "$f" 2>/dev/null; then
        echo "shell syntax error: $f" >&2
        bash -n "$f" 2>&1 | head -3 >&2
        status=1
    fi
done < <(git ls-files '*.sh')

while IFS= read -r f; do
    [ -f "$f" ] || continue
    if ! python3 -c 'import pathlib, sys; p = pathlib.Path(sys.argv[1]); compile(p.read_bytes(), str(p), "exec")' "$f" 2>/dev/null; then
        echo "python syntax error: $f" >&2
        python3 -c 'import pathlib, sys; p = pathlib.Path(sys.argv[1]); compile(p.read_bytes(), str(p), "exec")' "$f" 2>&1 | tail -3 >&2
        status=1
    fi
done < <(git ls-files '*.py')

if [ "$status" -eq 0 ]; then
    echo "verify: all tracked shell and python files parse"
fi
exit "$status"
