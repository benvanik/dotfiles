# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Testing (run before committing)
dotfiles test              # Tier 1: comprehensive local validation
dotfiles test --full       # Tier 1 + Tier 2 Docker integration tests

# Health check
dotfiles doctor            # Verify tools, symlinks, configs

# Installation
dotfiles install           # Set up symlinks and configuration
dotfiles fixup             # Integrate installer pollution into .shrc.local

# Dependencies
~/.dotfiles/install-deps.sh         # Install system and user packages
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

### Managed File Installation

Most dotfiles in `~/` are symlinks to `~/.dotfiles/`. The `_link()` function
handles those paths with backup of existing files to
`~/.local/share/dotfiles/backups/`. `_copy()` installs the few agent clients
that do not reliably follow symlinks.

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

The `tools/direnvrc` provides `use_*` functions. Version payloads remain
machine-local under `~/tools/`, while environment definitions are tracked in
`tools/<tool>/env.sh` so a dotfiles update applies them on every machine.

### Testing Infrastructure

**Tier 1** (`test/run-tier1.sh`): Fast local checks
- Shell syntax validation (POSIX, bash, zsh)
- Symlink target verification
- Generic worktree lifecycle integration
- Portable tool environment integration
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
- `_pkg_resolve_name`: Portable package-manager name mappings
- `_pkg_resolve_bin`: Portable installed-binary name mappings

## Key Patterns

### Adding a Managed File

1. Add `_link source dest`, or `_copy source dest` for a required regular
   file, in `bin/dotfiles` `_create_symlinks()`
2. The source path is relative to `~/.dotfiles/`
3. Tests automatically verify every `_link` and `_copy` source exists

### Shell Config Installer Pollution

Installers that modify `~/.bashrc` or `~/.zshrc` pollute the git repo (they're symlinks). The fix:
1. `dotfiles doctor` detects uncommitted changes to shell configs
2. `dotfiles fixup` moves additions to `~/.shrc.local` and reverts the file

### direnv Environment Summary

When entering a project with `.envrc`, a single-line summary is printed:
```
[env] llvm:21.1.6 cmake:4.2.0 ninja:1.13.2
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
| `bin/update-multiplexer` | Build and publish the pinned tmux/Byobu stack |
| `git/config` | Shared git config |
| `bin/dotfiles` | Main CLI for testing/maintenance |
| `bin/project-init` | Initialize project with direnv .envrc |
| `tools/direnvrc` | direnv functions for versioned tools |
| `lib/packages.sh` | Package definitions for install-deps.sh |
| `lib/bootstrap-pins.sh` | Reviewed shell, multiplexer, and Node identities |

## Byobu/tmux Notes

- **Never use `byobu start-server`**: The byobu profile creates a `byobu-janitor` session that immediately exits, killing the server. Use `byobu new-session` directly.
- **Attested releases**: `dotfiles multiplexer` defaults to the reviewed tmux and Byobu identities in `lib/bootstrap-pins.sh`. It builds one immutable generation below the XDG data root and switches the complete stack through one atomic selector; `~/.local` contains stable command/resource projections. Explicit version options resolve and record one release digest and Git commit. First migration of legacy in-place paths requires `--force` and preserves them in a crash-recoverable backup.
- **Trustmux**: Byobu's mobile companion daemon is excluded from the normal terminal stack. Install it explicitly with `dotfiles multiplexer --enable-trustmux`.
- **Plugins**: The multiplexer updater copies the complete clean plugin root, transactionally restores TPM to its reviewed commit in that snapshot, updates every other installed plugin, then publishes the whole root once. TPM is excluded from its own mutable update path; dirty, symlinked, or non-Git plugin entries fail before active state changes.

## Test Exclusions

The local-only `deps/` directory may contain source experiments. Tests exclude it to avoid recursing into external checkouts.
