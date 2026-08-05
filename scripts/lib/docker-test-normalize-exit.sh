#!/usr/bin/env bash
set +e
"$@"
status=$?
[ "$status" -ne 75 ] || exit 74
exit "$status"
