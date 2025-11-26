#!/bin/bash

# Read JSON input from Claude Code
input=$(cat)

# Extract information from JSON
cwd=$(echo "$input" | jq -r '.workspace.current_dir')
model_name=$(echo "$input" | jq -r '.model.display_name')

# Get current user and hostname (from PS1 \u@\h)
username=$(whoami)
hostname=$(hostname -s)

# Handle debian_chroot like original PS1
debian_chroot=""
if [[ -r /etc/debian_chroot ]]; then
    debian_chroot=$(cat /etc/debian_chroot)
fi

# Get git branch (if in a git repo)
git_branch=""
if git -C "$cwd" rev-parse --git-dir >/dev/null 2>&1; then
    git_branch=$(git -C "$cwd" branch --show-current 2>/dev/null || echo "detached")
fi

# Calculate approximate context usage based on transcript size
transcript_path=$(echo "$input" | jq -r '.transcript_path')

# Build status line matching PS1 format: ${debian_chroot:+($debian_chroot)}\u@\h:\w plus extras
# Using printf with ANSI colors (will be dimmed by terminal)

# Add debian_chroot if present
if [[ -n "$debian_chroot" ]]; then
    printf "(%s)" "$debian_chroot"
fi

# Main PS1 format: \u@\h:\w with colors
printf "\033[01;32m%s@%s\033[00m:\033[01;34m%s\033[00m" "$username" "$hostname" "$cwd"

# Add git branch if available
if [[ -n "$git_branch" ]]; then
    printf " (%s)" "$git_branch"
fi

# Add model (shortened)
model_short=$(echo "$model_name" | sed 's/Claude //' | sed 's/ /-/g' | tr '[:upper:]' '[:lower:]')
printf " [%s] $" "$model_short"