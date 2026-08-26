#!/usr/bin/env bash

# This file is sourced by the runnable scripts. Values in MOSS_ENV_FILE take
# precedence over inherited variables so one deployment has a single source of
# truth. Set MOSS_ENV_FILE=/dev/null to use command-line variables only.
MOSS_ENV_FILE="${MOSS_ENV_FILE:-$PROJECT_DIR/.env}"
if [[ -f "$MOSS_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$MOSS_ENV_FILE"
  set +a
fi
