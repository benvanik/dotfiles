# Dotfiles

Personal shell configuration.

## About

This is an opinionated dotfiles setup optimized for:
- Compiler development (MLIR/IREE/ROCm workflows)
- Multi-version tool management via direnv

Feel free to fork and adapt. Project-specific scripts can be removed if you
don't work on those projects.

## Quick Start

```bash
# 1. Clone dotfiles
git clone https://github.com/USER/dotfiles ~/.dotfiles

# 2. Install dependencies (requires sudo)
sudo ~/.dotfiles/install-deps.sh

# 3. Set up symlinks and configuration
~/.dotfiles/bin/dotfiles install

# 4. Set zsh as default shell
chsh -s $(which zsh)

# 5. Set terminal font to "MesloLGS NF" (installed by install-deps.sh)

# 6. Start new shell
zsh
```

`dotfiles install` will:
- Create all symlinks (~/.zshrc, ~/.gitconfig, etc.)
- Prompt for your identity (name, email, GitHub username)
- Configure git SSH signing automatically
- Create templates for machine-specific files

## Shell Features

- **Fuzzy finder**: `Ctrl-R` (history), `Ctrl-T` (files), `Alt-C` (directories)
- **Find in files**: `Ctrl-G` (ripgrep + fzf)
- **Autosuggestions**: Fish-style command suggestions from history
- **Syntax highlighting**: Real-time command highlighting
- **Smart completion**: Tab completion with flag parsing
- **Powerlevel10k**: Fast, customizable prompt

## Dotfiles CLI

The `dotfiles` command provides testing and maintenance:

```bash
dotfiles test              # Fast local validation (<5s)
dotfiles test --full       # Full Docker-based integration tests
dotfiles doctor            # Health check (tools, symlinks, configs)
dotfiles update            # Pull latest changes from git
dotfiles install           # Set up symlinks and configuration
dotfiles deps              # Run install-deps.sh (install packages)
```

## Project-Specific Tools (Optional)

Scripts in `bin/` prefixed with project names are optional and can be removed:

| Prefix | Project | Purpose |
|--------|---------|---------|
| `iree-*` | [IREE](https://github.com/iree-org/iree) | Compiler worktree and build management |
| `therock-*` | [TheRock](https://github.com/ROCm/TheRock) | ROCm/HIP compiler development |
| `vulkan-*` | Vulkan SDK | SDK installation and layer building |

These scripts assume specific directory layouts (`~/src/iree/`, `~/src/rocm/`, etc.).
If you don't work on these projects, delete the scripts or ignore them.

## Platform Support

| Platform | Status |
|----------|--------|
| Linux (apt) | Full support |
| Linux (dnf) | Full support |
| macOS (brew) | Full support |
| WSL | Should work (untested) |

---

## Manual Configuration

The sections below cover manual setup for advanced users or when you need to
customize machine-specific settings.

### Machine-Specific Files

Files matching `*.local` are gitignored (machine-specific):

| File | Purpose |
|------|---------|
| `~/.shrc.local` | Machine-specific PATH entries |
| `~/.gitconfig.local` | User identity and SSH signing key |
| `~/.secrets` | API keys and tokens |

### Customizing Machine Settings

1. Edit `~/.shrc.local` with machine-specific PATHs:
   ```bash
   # Linux
   _add_path "/snap/bin"
   _add_path "$HOME/tools/llvm/bin"

   # macOS
   _add_path "/opt/homebrew/bin"
   ```
2. Re-run `dotfiles install` to regenerate `~/.gitconfig.local`
3. Edit `~/.secrets` with API keys

### Git Commit Signing

SSH signing is configured automatically by `dotfiles install`. Manual setup:

```bash
# Automatic setup (finds SSH key, creates allowed_signers)
git-setup-signing

# Or manual
git config --global user.signingkey ~/.ssh/id_ed25519.pub
echo "your@email $(cat ~/.ssh/id_ed25519.pub)" >> ~/.ssh/allowed_signers
```

### Fonts

Powerlevel10k requires **MesloLGS NF** font for icons. `install-deps.sh` installs it automatically.

**Set your terminal font to "MesloLGS NF":**
- **GNOME Terminal**: Preferences → Profile → Custom font
- **Konsole**: Settings → Edit Profile → Appearance → Font
- **VSCode**: Settings → `terminal.integrated.fontFamily` → `MesloLGS NF`
- **Ghostty**: `font-family = "MesloLGS NF"` in config
- **iTerm2** (macOS): Preferences → Profiles → Text → Font

---

## Contributing

### Testing Changes

Before committing, run the test suite:

```bash
dotfiles test              # Fast local validation
dotfiles test --full       # Full Docker integration tests
```

A pre-commit hook runs `dotfiles test` automatically.

### Test Tiers

**Tier 1 (fast, local)**:
- Shell syntax validation (bash, zsh, POSIX)
- Symlink target verification
- POSIX compliance checks
- Secret detection
- shellcheck (if installed)

**Tier 2 (Docker)** - on-demand with `--full`:
- Full install on pristine Alpine/Ubuntu containers
- Package installation validation
- Interactive shell startup tests

### Security: Never Commit Secrets

**CRITICAL**: The following should NEVER be committed:

- SSH keys (`*.pem`, `*.key`, `id_*`)
- API keys and tokens
- Passwords and credentials
- `~/.secrets` or any file containing secrets
- `*.local` files (machine-specific, may contain paths or identities)

The `.gitignore` covers common patterns, but always review `git status` and
`git diff` before committing. The pre-commit hook includes secret detection
but is not foolproof.

If you accidentally commit secrets:
1. **Immediately** rotate the compromised credentials
2. Use `git filter-branch` or BFG Repo-Cleaner to remove from history
3. Force push (coordinate with other users if the repo is shared)

---

## Directory Structure

```
~/.dotfiles/
├── shell/           # Shell configs (.zshrc, .shrc, .aliases, etc.)
│   └── zshrc.d/     # Modular zsh configs (fzf, completion, etc.)
├── themes/          # Powerlevel10k themes
├── git/             # Git configuration
│   └── hooks/       # Git hooks (pre-commit)
├── bin/             # User scripts (on PATH)
│   └── dotfiles     # Main CLI for testing/maintenance
├── test/            # Testing infrastructure
├── claude/          # Claude Code settings
├── install-deps.sh  # Package installation
└── secrets.template # API keys template
```

## Required Packages

Installed automatically by `install-deps.sh`:

| Package | Purpose | apt | brew |
|---------|---------|-----|------|
| zsh | Shell | `apt install zsh` | `brew install zsh` |
| fzf | Fuzzy finder | `apt install fzf` | `brew install fzf` |
| ripgrep | Fast grep | `apt install ripgrep` | `brew install ripgrep` |
| jq | JSON processor | `apt install jq` | `brew install jq` |
| git | Version control | `apt install git` | `brew install git` |

## Recommended Packages

| Package | Purpose | apt | brew |
|---------|---------|-----|------|
| fd-find | Fast find | `apt install fd-find` | `brew install fd` |
| bat | Syntax highlighting | `apt install bat` | `brew install bat` |
| eza | Modern ls | `apt install eza` | `brew install eza` |
| zsh-autosuggestions | Fish-style suggestions | `apt install zsh-autosuggestions` | `brew install zsh-autosuggestions` |

## License

Personal configuration - use at your own discretion.
