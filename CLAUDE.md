# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Testing (run before committing)
dotfiles test              # Tier 1: fast local validation (<15s)
dotfiles test --full       # Tier 1 + Tier 2 Docker integration tests

# Health check
dotfiles doctor            # Verify tools, symlinks, configs

# Installation
dotfiles install           # Set up symlinks and configuration
dotfiles fixup             # Integrate installer pollution into .shrc.local

# Dependencies
sudo ~/.dotfiles/install-deps.sh    # Install system packages
```

A pre-commit hook runs `dotfiles test` automatically.

## Architecture

### Shell Configuration Hierarchy

```
~/.zshrc → shell/zshrc           # Zsh entry point
~/.bashrc → shell/bashrc         # Bash entry point
~/.profile → shell/profile       # Login shell entry point
    ↓
~/.shrc → shell/shrc             # POSIX-compatible, sourced by all shells
    ↓
~/.shrc.local                    # Machine-specific (gitignored)
```

Key principle: `shell/shrc` must remain POSIX sh compatible (no `[[`, arrays, `local` in functions, or process substitution).

### Symlink Management

All dotfiles in `~/` are symlinks to `~/.dotfiles/`. The `_link()` function in `bin/dotfiles` handles creation with backup of existing files to `~/.local/share/dotfiles/backups/`.

### Machine-Specific vs Shared Configuration

| Committed (shared) | Gitignored (machine-specific) |
|-------------------|-------------------------------|
| `shell/shrc` | `~/.shrc.local` |
| `shell/platform/*.sh` | `~/.gitconfig.local` |
| `git/config` | `~/.secrets` |

### Versioned Tools System

Tools installed in `~/tools/<tool>/<version>/` with `latest` symlinks. Per-project tool versions via direnv:

```bash
# In .envrc files:
use_llvm ">=21.0.0"    # Minimum version constraint
use_cmake "4.2.0"      # Exact version
use_ninja              # Latest
source_local_envrc     # Must be last - prints env summary
```

The `tools/direnvrc` provides `use_*` functions. Tool environment is configured in `~/tools/<tool>/env.sh`.

### Testing Infrastructure

**Tier 1** (`test/run-tier1.sh`): Fast local checks
- Shell syntax validation (POSIX, bash, zsh)
- Symlink target verification
- POSIX compliance (no bash-isms in POSIX files)
- Secret detection
- shellcheck analysis
- Tool smoketests (`tools/*/smoketest.sh`)

**Tier 2** (`test/run-tier2.sh`): Docker integration
- Fresh Ubuntu container
- Full `install-deps.sh` + `dotfiles install` flow
- Interactive shell startup verification

### Package Management

`lib/packages.sh` is the single source of truth for dependencies:
- `REQUIRED_PACKAGES`: Installation fails without these
- `RECOMMENDED_PACKAGES`: Warnings only
- `PKG_NAMES`: Package manager name mappings (e.g., `apt:rg` → `ripgrep`)
- `BIN_NAMES`: Binary name mappings (e.g., `apt:fd` → `fdfind`)

## Key Patterns

### Adding a New Symlink

1. Add `_link source dest` in `bin/dotfiles` `_create_symlinks()` function
2. The source path is relative to `~/.dotfiles/`
3. Tests automatically verify all `_link` targets exist

### Shell Config Installer Pollution

Installers that modify `~/.bashrc` or `~/.zshrc` pollute the git repo (they're symlinks). The fix:
1. `dotfiles doctor` detects uncommitted changes to shell configs
2. `dotfiles fixup` moves additions to `~/.shrc.local` and reverts the file

### direnv Environment Summary

When entering a project with `.envrc`, a single-line summary is printed:
```
[env] llvm:21.1.6 cmake:4.2.0 ninja:1.13.2 mold:2.40.4
```

This replaces verbose direnv logging (`DIRENV_LOG_FORMAT=""` in shrc).

## File Purposes

| File | Purpose |
|------|---------|
| `shell/shrc` | POSIX PATH/env setup (sourced by all shells) |
| `shell/zshrc` | Zsh-specific config, sources shrc and modules |
| `shell/zshrc.d/*.zsh` | Modular zsh features (fzf, completion, direnv) |
| `shell/platform/*.sh` | Platform-specific settings (linux, darwin, wsl) |
| `shell/aliases` | Cross-shell aliases |
| `tmux.conf` | Main tmux config with plugins (TPM, resurrect, menus) |
| `byobu/.tmux.conf` | Byobu config that sources main tmux.conf |
| `git/config` | Shared git config |
| `bin/dotfiles` | Main CLI for testing/maintenance |
| `bin/project-init` | Initialize project with direnv .envrc |
| `tools/direnvrc` | direnv functions for versioned tools |
| `lib/packages.sh` | Package definitions for install-deps.sh |

## Byobu/tmux Notes

- **Never use `byobu start-server`**: The byobu profile creates a `byobu-janitor` session that immediately exits, killing the server. Use `byobu new-session` directly.
- **Custom tmux**: We build a custom tmux in `deps/tmux/` with mouse handling fixes. Install with `~/.dotfiles/deps/install-tmux.sh --rebuild`.
- **Plugins**: After fresh install, start tmux and press `prefix + I` to install TPM plugins.

## Test Exclusions

The `deps/` directory contains git submodules (like `deps/tmux/`). Tests exclude `deps/**/*` to avoid recursing into submodules, but scripts directly in `deps/` are still checked.
