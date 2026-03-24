---
name: nix-environment
type: repo
version: 1.0.0
agent: CodeActAgent
---

# Nix Environment

This workspace runs in a Nix-managed environment. Key details:

- **Package manager:** Nix (use `nix profile install nixpkgs#<pkg>` to add tools)
- **No root access or sudo:** Packages are installed to the user's Nix profile, not system-wide.
- **No apt/brew/yum:** Traditional package managers are unavailable. Use `nix search nixpkgs <query>` to find packages.
- **Pre-installed tools:** git, tmux, bash, curl, Python, and core utilities are already available.
- **Workspace directory:** `/workspace` is the working directory for project files.
