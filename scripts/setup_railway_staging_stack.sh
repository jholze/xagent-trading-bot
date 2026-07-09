#!/usr/bin/env bash
# Entry point for Railway staging stack setup.
exec "$(dirname "$0")/setup_railway_test_stack.sh" "$@"