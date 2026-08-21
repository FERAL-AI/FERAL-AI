{
  description = "FERAL thin Nix foundation (brain + client + dev shell)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
    in
    (flake-utils.lib.eachSystem systems
      (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python311;
          pyPkgs = pkgs.python311Packages;
          node = pkgs.nodejs_20;
          feralPythonPackage = pyPkgs.buildPythonPackage rec {
            pname = "feral-ai";
            version = "1.2.0";
            src = ./feral-core;
            pyproject = true;
            nativeBuildInputs = with pyPkgs; [ setuptools wheel ];
            propagatedBuildInputs = with pyPkgs; [
              fastapi
              uvicorn
              pydantic
              websockets
              httpx
              pyyaml
              rich
              html2text
              openai
              numpy
              pillow
              aiohttp
            ];
            doCheck = false;
          };
        in
        {
          packages = rec {
            feral-brain = feralPythonPackage;

            # Repointed at feral-client-v2 when feral-client/ (v1) was
            # deleted in 2026.8.12. The attribute and app names are kept
            # so `nix run .#client` and `nix build .#feral-client` do not
            # break for anyone who has them in a script.
            feral-client = pkgs.writeShellApplication {
              name = "feral-client";
              runtimeInputs = [ node ];
              text = ''
                cd ${self}/feral-client-v2
                if [ ! -d node_modules ]; then
                  echo "node_modules not found. Run: npm install"
                  exit 1
                fi
                exec ${node}/bin/npm run dev -- --host
              '';
            };

            default = feral-brain;
          };

          apps = {
            brain = {
              type = "app";
              program = "${pkgs.writeShellScript "feral-brain-app" ''
                export FERAL_HOME="${FERAL_HOME:-$HOME/.feral}"
                export FERAL_HOST="${FERAL_HOST:-0.0.0.0}"
                export FERAL_PORT="${FERAL_PORT:-9090}"
                if [ -z "${FERAL_PUBLIC_BASE_URL:-}" ]; then
                  export FERAL_PUBLIC_BASE_URL="http://localhost:$FERAL_PORT"
                fi
                exec ${self.packages.${system}.feral-brain}/bin/feral serve --bind "$FERAL_HOST" --serve-port "$FERAL_PORT"
              ''}";
            };
            client = {
              type = "app";
              program = "${self.packages.${system}.feral-client}/bin/feral-client";
            };
            default = {
              type = "app";
              program = "${pkgs.writeShellScript "feral-brain-default-app" ''
                export FERAL_HOME="${FERAL_HOME:-$HOME/.feral}"
                export FERAL_HOST="${FERAL_HOST:-0.0.0.0}"
                export FERAL_PORT="${FERAL_PORT:-9090}"
                if [ -z "${FERAL_PUBLIC_BASE_URL:-}" ]; then
                  export FERAL_PUBLIC_BASE_URL="http://localhost:$FERAL_PORT"
                fi
                exec ${self.packages.${system}.feral-brain}/bin/feral serve --bind "$FERAL_HOST" --serve-port "$FERAL_PORT"
              ''}";
            };
          };

          devShells.default = pkgs.mkShell {
            packages = [
              python
              node
              pkgs.git
              pkgs.pkg-config
              pkgs.rustc
              pkgs.cargo
            ];
            shellHook = ''
              export FERAL_HOME="${FERAL_HOME:-$HOME/.feral}"
              export FERAL_HOST="${FERAL_HOST:-0.0.0.0}"
              export FERAL_PORT="${FERAL_PORT:-9090}"
              export FERAL_PUBLIC_BASE_URL="${FERAL_PUBLIC_BASE_URL:-http://localhost:$FERAL_PORT}"
              echo "FERAL dev shell ready"
              echo "Run brain: cd feral-core && python -m cli.main serve"
              echo "Run client: cd feral-client-v2 && npm install && npm run dev"
            '';
          };
        }))
    // {
      nixosModules.feral-brain = { config, lib, pkgs, ... }:
        let
          cfg = config.services.feral.brain;
        in
        {
          options.services.feral.brain = {
            enable = lib.mkEnableOption "FERAL brain service";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.feral-brain;
              description = "FERAL brain package to execute.";
            };
            host = lib.mkOption {
              type = lib.types.str;
              default = "0.0.0.0";
            };
            port = lib.mkOption {
              type = lib.types.port;
              default = 9090;
            };
            home = lib.mkOption {
              type = lib.types.str;
              default = "/var/lib/feral";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.feral = {
              isSystemUser = true;
              group = "feral";
              home = cfg.home;
              createHome = true;
            };
            users.groups.feral = { };

            systemd.services.feral-brain = {
              description = "FERAL Brain";
              after = [ "network-online.target" ];
              wantedBy = [ "multi-user.target" ];
              environment = {
                FERAL_HOME = cfg.home;
                FERAL_HOST = cfg.host;
                FERAL_PORT = toString cfg.port;
                FERAL_PUBLIC_BASE_URL = "http://localhost:${toString cfg.port}";
              };
              serviceConfig = {
                ExecStart = "${cfg.package}/bin/feral serve --bind ${cfg.host} --serve-port ${toString cfg.port}";
                Restart = "on-failure";
                User = "feral";
                Group = "feral";
                WorkingDirectory = cfg.home;
              };
            };
          };
        };
    };
}
