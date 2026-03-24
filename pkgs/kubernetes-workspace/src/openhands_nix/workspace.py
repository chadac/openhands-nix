"""NixEnvironment: base class defining what Nix environment a workspace needs.

This is the shared configuration layer. Subclasses define *how* the
environment gets provisioned (K8s entrypoint, nix-csi, local shell).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NixEnvironment(BaseModel):
    """Describes a Nix environment for an OpenHands workspace.

    At least one of `packages`, `flake_ref`, or `nix_expr` should be
    specified. They can be combined — packages are installed alongside
    whatever the flake/expression provides.

    Examples:
        # Individual packages from nixpkgs
        NixEnvironment(packages=["nixpkgs#nodejs", "nixpkgs#ripgrep"])

        # A flake dev shell
        NixEnvironment(flake_ref="github:myorg/myproject#devShell")

        # Raw Nix expression
        NixEnvironment(nix_expr="with import <nixpkgs> {}; buildEnv { ... }")

        # Pinned nixpkgs
        NixEnvironment(
            packages=["nixpkgs#nodejs"],
            nixpkgs_ref="github:NixOS/nixpkgs/nixos-24.11",
        )
    """

    packages: list[str] = Field(
        default_factory=list,
        description=(
            "Nix installables to add to the environment. "
            "e.g. ['nixpkgs#nodejs', 'nixpkgs#ripgrep', 'github:my/flake#tool']"
        ),
    )
    flake_ref: str | None = Field(
        default=None,
        description=(
            "Flake reference for the environment. "
            "e.g. 'github:myorg/myproject#devShell' or './my-flake#packages.x86_64-linux.default'"
        ),
    )
    nix_expr: str | None = Field(
        default=None,
        description=(
            "Raw Nix expression that evaluates to a derivation. "
            "e.g. 'with import <nixpkgs> {}; buildEnv { name = \"env\"; paths = [ nodejs ripgrep ]; }'"
        ),
    )
    nixpkgs_ref: str = Field(
        default="github:NixOS/nixpkgs/nixos-unstable",
        description="Nixpkgs flake reference for resolving 'nixpkgs#...' packages.",
    )

    @property
    def has_nix_config(self) -> bool:
        """True if any Nix environment config is specified."""
        return bool(self.packages or self.flake_ref or self.nix_expr)

    def to_install_args(self) -> list[str]:
        """Convert to arguments for `nix profile install`."""
        args = []
        for pkg in self.packages:
            # Resolve bare nixpkgs# refs against the pinned nixpkgs
            if pkg.startswith("nixpkgs#"):
                attr = pkg[len("nixpkgs#"):]
                args.append(f"{self.nixpkgs_ref}#{attr}")
            else:
                args.append(pkg)
        if self.flake_ref:
            args.append(self.flake_ref)
        return args

    def to_nix_shell_args(self) -> list[str]:
        """Convert to arguments for `nix shell`."""
        return self.to_install_args()

    def to_csi_volume_attributes(self) -> dict[str, str]:
        """Convert to nix-csi CSI volumeAttributes.

        nix-csi supports three mutually exclusive modes (by priority):
        1. storePath (direct /nix/store/... path)
        2. flakeRef (flake reference to build)
        3. nixExpr (raw Nix expression)

        For multiple packages, we generate a nixExpr that builds a
        combined environment.
        """
        if self.nix_expr:
            return {"nixExpr": self.nix_expr}

        if self.flake_ref and not self.packages:
            return {"flakeRef": self.flake_ref}

        # Build a Nix expression that combines all packages
        # into a single buildEnv derivation
        pkg_exprs = []
        for pkg in self.packages:
            if pkg.startswith("nixpkgs#"):
                attr = pkg[len("nixpkgs#"):]
                pkg_exprs.append(f"pkgs.{attr}")
            else:
                # For flake refs, use builtins.getFlake
                pkg_exprs.append(
                    f'(builtins.getFlake "{pkg}").packages.'
                    "${builtins.currentSystem}.default"
                )

        if self.flake_ref:
            pkg_exprs.append(
                f'(builtins.getFlake "{self.flake_ref}").packages.'
                "${builtins.currentSystem}.default"
            )

        nixpkgs_url = self.nixpkgs_ref
        expr = (
            "let\n"
            f'  nixpkgs = builtins.getFlake "{nixpkgs_url}";\n'
            "  pkgs = import nixpkgs { };\n"
            "in\n"
            "pkgs.buildEnv {\n"
            '  name = "openhands-workspace-env";\n'
            f"  paths = [ {' '.join(pkg_exprs)} ];\n"
            "}"
        )
        return {"nixExpr": expr}
