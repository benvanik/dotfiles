# macOS-specific shell configuration (synced).
# Sourced automatically on macOS systems.

# Homebrew.
_add_path "/opt/homebrew/bin"
_add_path "/opt/homebrew/sbin"

# Disable Homebrew auto-update (run manually: brew update).
export HOMEBREW_NO_AUTO_UPDATE=1

# Homebrew LLVM (if installed via brew).
_add_path "/opt/homebrew/opt/llvm/bin"
