{
  description = "ROOKIE mirror downloader and Quest sideloader";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = {
    self,
    nixpkgs,
  }: let
    forAllSystems = nixpkgs.lib.genAttrs ["x86_64-linux" "aarch64-linux"];
  in {
    packages = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
      rookie = pkgs.callPackage ./package.nix {
        apiKeyFile = pkgs.callPackage ./nix/api-key.nix {};
      };
    in {
      inherit rookie;
      default = rookie;
    });
    checks = forAllSystems (system: {inherit (self.packages.${system}) rookie;});
    devShells = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      default = pkgs.mkShell {
        packages = with pkgs; [python3 python3Packages.build python3Packages.setuptools curl p7zip android-tools aapt ruff];
      };
    });
  };
}
