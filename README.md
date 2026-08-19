# Dotfiles

Personal shell configuration.

## About

This is an opinionated dotfiles setup optimized for:
- Compiler development (HRX/ROCm workflows)
- Multi-version tool management via direnv

Feel free to fork and adapt. Project-specific scripts can be removed if you
don't work on those projects.

## Quick Start

```bash
# 1. Clone dotfiles
git clone https://github.com/USER/dotfiles ~/.dotfiles

# 2. Install dependencies (prompts for sudo when installing system packages)
~/.dotfiles/install-deps.sh

# 3. Set up symlinks and configuration
~/.dotfiles/bin/dotfiles install

# 4. Set zsh as default shell
chsh -s $(which zsh)

# 5. Set terminal font to "MesloLGS NF" (installed by install-deps.sh)

# 6. Start new shell
zsh
```

`dotfiles install` will:
- Install managed configuration files (using symlinks where appropriate)
- Prompt for your identity (name, email, GitHub username)
- Configure git SSH signing automatically
- Create templates for machine-specific files

`install-deps.sh` treats user-level bootstrap code as reviewed software:
Oh My Zsh, Powerlevel10k, and TPM are clean detached checkouts of pinned
commits; Meslo fonts are pinned and checksummed; and the hardened NVM installer
selects exact Node `24.11.1`. The multiplexer updater likewise starts from
reviewed tmux, Byobu, and TPM identities, publishes tmux + Byobu through one
atomic generation selector, and updates plugins in an off-path whole-root
snapshot. A dirty checkout, foreign origin, or unverified existing font fails
without being overwritten.

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
dotfiles test              # Fast local validation
dotfiles test --full       # Full Docker-based integration tests
dotfiles doctor            # Health check, including managed agent contracts
dotfiles update            # Pull changes and publish agent contracts
dotfiles install           # Set up symlinks and configuration
dotfiles deps              # Run install-deps.sh (install packages)
```

## Project Tools

The generic `project-*` commands initialize development environments, launch
editor and tmux sessions, and manage sibling Git worktrees. The worktree
commands use a `<project>/main` primary checkout and create feature worktrees
at `<project>/<name>`:

```bash
mkdir -p ~/src/my-project/main
cd ~/src/my-project/main
project-init
project-worktree-init users/me/feature feature
direnv allow ../feature
cd ../feature
project-dev
project-code .
cd ../main
project-worktree-deinit feature
```

In a new directory named `main`, `project-init` initializes Git on branch
`main` and creates the first commit from its generated, committable files. This
is the commit boundary Git requires before a sibling worktree can exist.
Existing repositories keep their history; ordinary directories retain the
environment-only behavior. `--repository` requires the primary layout and
`--no-repository` suppresses the inferred bootstrap. Generated environments
put the repository's `build_tools/bin` directory on `PATH` whenever that
convention is present and automatically refresh when a checkout adds or removes
it.

When `main/AGENTS.override.md`, `main/.bazelrc.cache`, `main/.bazelrc.local`, or
`main/.beads` exists, `project-worktree-init` links the shared local state into
each new worktree. `.bazelrc.cache` owns the project's cache location while
`.bazelrc.local` carries other machine-local Bazel policy, so the home fallback
can apply to unmanaged repositories without overriding managed placement.
The main worktree must own `.beads` as a physical directory; sibling links make
one issue database authoritative without copying or reconciling it. Sibling
Bazel worktrees keep independent output bases and servers while sharing the
primary checkout's repository and disk-cache policy.
Deinitialization refuses a worktree containing any other modified, untracked,
or ignored files, stops its exact tmux session, and rechecks the complete
worktree before removal. Default tmux names carry a readable project/worktree
prefix plus a digest of the physical project path, so identically named
repositories cannot attach to each other's sessions. Worktree lifetime and
project configuration are separate: the tracked `.envrc` follows each branch,
while `project-worktree-init` prints the explicit direnv authorization for the
new path.

`agents/WORKING_CONTRACT.md` is the canonical global agent contract.
`dotfiles install` and `dotfiles update` publish byte-identical regular-file
copies for Codex and Claude; `dotfiles doctor` reports missing, linked, or
drifted copies.

Machines with Bazel installed set `BAZEL_CACHE_ROOT` in `~/.shrc.local` to a
large writable filesystem outside `HOME`. `dotfiles install` generates a
machine-local `~/.bazelrc` that places unmanaged workspace state on that
filesystem and makes Bazel's default HOME output root read-only. The home rc
then imports a managed workspace's `.bazelrc.cache`, when present, so its
project-specific placement wins without loading the general `.bazelrc.local`
twice. `dotfiles doctor` checks both the machine rc and the HOME guard and
requires the guarded HOME root to contain no Bazel state.

## Optional Infrastructure and Specialized Tools

Some command families are useful only on machines doing GPU or compiler work:

| Prefix | Layer | Purpose |
|--------|-------|---------|
| `runpod*` | [RunPod](https://www.runpod.io/) | Generic GPU hosts, claims, SSH, and billing lifetime |
| `model-lab*`, `model-session` | Model lab | Private model services and isolated Pi sessions |
| `benchmark-lock` | [Benchmark broker](benchmarkd/README.md) | Process-scoped FIFO admission and exact host-policy restoration |
| `therock-*` | [TheRock](https://github.com/ROCm/TheRock) | ROCm/HIP compiler development |
| `vulkan-*` | Vulkan SDK | SDK installation and layer building |

RunPod and model-lab keep machine-local state outside this repository and do
not own any project checkout or project instance. TheRock and Vulkan commands
are specialized development helpers and may assume their corresponding source
layouts.

The two GPU layers have separate ownership and operating guides:

- [`runpod/README.md`](runpod/README.md) covers provider resources, generic
  hosts, resource claims, SSH, and host retirement.
- [`model-lab/README.md`](model-lab/README.md) covers Hugging Face models,
  serving, caches, service idle lifetime, profiles, and isolated Pi sessions.

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
- Full install on a pristine Ubuntu container
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
├── agents/          # Provider-neutral global working contract
├── bin/             # User scripts (on PATH)
│   └── dotfiles     # Main CLI for testing/maintenance
├── test/            # Testing infrastructure
├── claude/          # Claude Code provider settings
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
| curl | HTTP transfers | `apt install curl` | `brew install curl` |
| direnv | Per-project environments | `apt install direnv` | `brew install direnv` |
| Python 3 | Standard-library command tools | `apt install python3` | `brew install python` |

## Recommended Packages

| Package | Purpose | apt | brew |
|---------|---------|-----|------|
| fd-find | Fast find | `apt install fd-find` | `brew install fd` |
| bat | Syntax highlighting | `apt install bat` | `brew install bat` |
| eza | Modern ls | `apt install eza` | `brew install eza` |
| shellcheck | Shell static analysis | `apt install shellcheck` | `brew install shellcheck` |
| zsh-autosuggestions | Fish-style suggestions | `apt install zsh-autosuggestions` | `brew install zsh-autosuggestions` |

## Multiplexer Build Dependencies

On Linux, `install-deps.sh` installs the compiler, Autotools, core utilities,
and development headers consumed by `dotfiles multiplexer`. The exact plans
are centralized in `lib/packages.sh`: `build-essential`, `libevent-dev`, and
`libncurses-dev` on apt; `gcc`, `make`, `libevent-devel`, and
`ncurses-devel` on dnf; and `base-devel`, `libevent`, and `ncurses` on pacman.
`dotfiles doctor` verifies the production command and pkg-config interfaces,
not merely package-manager records.

## License

Personal configuration - use at your own discretion.
