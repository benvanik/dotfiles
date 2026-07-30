# Bazel tools environment.
# Sourced by tools.sh and direnvrc after BAZEL_ROOT is selected.
# shellcheck disable=SC2154
if [ -n "$BAZEL_ROOT" ]; then
    export PATH="$BAZEL_ROOT/bin:$PATH"
fi
