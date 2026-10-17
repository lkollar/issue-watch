{
  description = "GitHub issue search email digests";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      packageFor =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        pkgs.python315.pkgs.buildPythonApplication {
          pname = "issue-watch";
          version = "0.1.0";
          pyproject = true;
          src = self;
          build-system = [ pkgs.python315.pkgs.setuptools ];
          nativeCheckInputs = [ pkgs.ruff ];
          checkPhase = ''
            runHook preCheck
            export PYTHONPATH="$PWD/src"
            python -m unittest discover -s tests -v
            ruff check .
            ruff format --check .
            runHook postCheck
          '';
          pythonImportsCheck = [ "issue_watch.cli" ];
          meta = {
            description = "Email digests for new GitHub issue search matches";
            mainProgram = "issue-watch";
            platforms = nixpkgs.lib.platforms.linux;
          };
        };
    in
    {
      packages = forAllSystems (system: {
        default = packageFor system;
      });
      checks = forAllSystems (system: {
        default = packageFor system;
      });
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python315
              pkgs.uv
              pkgs.ruff
              pkgs.nixfmt
            ];
            UV_PYTHON_DOWNLOADS = "never";
            UV_PYTHON = "${pkgs.python315}/bin/python3.15";
          };
        }
      );
      nixosModules.default = import ./nix/module.nix self;
    };
}
