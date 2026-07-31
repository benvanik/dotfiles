# Hugging Face CLI payload validation.
#
# The raw executable deliberately stays off PATH. bin/hf is the only supported
# entry point because it owns credential, cache, telemetry, and update policy.
if [ -n "${HF_ROOT:-}" ] && [ ! -x "$HF_ROOT/bin/hf" ]; then
    printf 'Hugging Face CLI root is incomplete: %s\n' "$HF_ROOT" >&2
    return 1
fi
