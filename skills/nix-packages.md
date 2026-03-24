---
name: nix-packages
type: knowledge
version: 1.0.0
agent: CodeActAgent
triggers:
- install
- uninstall
- package
- apt
- apt-get
- brew
- yum
- dnf
- pacman
- apk
- upgrade
- nix
---

# Package Management in This Environment

This environment uses **Nix** for package management. Traditional package
managers (`apt`, `apt-get`, `brew`, `yum`, `dnf`, `pacman`, `apk`) are
**not available**. Use the Nix commands below instead.

## Installing packages

```bash
# Search for a package
nix search nixpkgs <query>

# Install a package
nix profile install nixpkgs#<package>

# Examples
nix profile install nixpkgs#jq
nix profile install nixpkgs#ripgrep
nix profile install nixpkgs#nodejs_22
nix profile install nixpkgs#python312
nix profile install nixpkgs#go
nix profile install nixpkgs#rustc
nix profile install nixpkgs#gcc
```

## Listing installed packages

```bash
nix profile list
```

## Removing packages

```bash
# Remove by package name (use the index from `nix profile list`)
nix profile remove <index>
```

## Upgrading packages

```bash
# Upgrade all installed packages
nix profile upgrade '.*'
```

## Tips

- Package names in nixpkgs may differ from apt/brew names. Use
  `nix search nixpkgs <query>` to find the correct name.
- Nix packages are immutable and isolated — installing a package never
  breaks existing ones.
- For Python libraries, prefer `pip install` inside a virtualenv rather
  than installing via Nix. Use Nix for system-level tools and runtimes.
- For Node.js packages, use `npm install` for project dependencies.
  Use Nix for the Node.js runtime itself (`nixpkgs#nodejs_22`).
- Multiple versions of the same tool can coexist in the Nix store,
  but only one version is active in your profile at a time.
