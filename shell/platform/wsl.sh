# WSL-specific shell configuration (synced).
# Sourced automatically on Windows Subsystem for Linux.

# WSL interop - access Windows programs.
_add_path "/mnt/c/Windows/System32"

# Disable Windows PATH pollution in some cases.
# export WSLENV=

# X11 display for GUI apps (WSLg).
export DISPLAY="${DISPLAY:-:0}"
