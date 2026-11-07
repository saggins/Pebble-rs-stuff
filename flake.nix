{
  description = "pebble-ring: FastAPI webhook server";

  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0";

  outputs =
    { self, ... }@inputs:
    let
      inherit (inputs.nixpkgs) lib;

      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      forEachSupportedSystem =
        f:
        lib.genAttrs supportedSystems (
          system:
          f {
            inherit system;
            pkgs = import inputs.nixpkgs {
              inherit system;
              config.allowUnfree = true;
            };
          }
        );
    in
    {
      devShells = forEachSupportedSystem (
        { pkgs, system }:
        {
          default = pkgs.mkShellNoCC {
            # System-level tools only. Python packages live in .venv via requirements.txt.
            packages = with pkgs; [
              python313
              whisper-cpp # whisper-cli + model downloader
              ffmpeg-headless # m4a -> 16kHz wav for whisper
              curl
              jq
              self.formatter.${system}
            ];

            shellHook = ''
              export VENV_DIR="$PWD/.venv"
              if [ ! -d "$VENV_DIR" ]; then
                echo "Creating venv at $VENV_DIR"
                python -m venv "$VENV_DIR"
              fi
              source "$VENV_DIR/bin/activate"

              # Reinstall only when requirements.txt changes.
              stamp="$VENV_DIR/.requirements.sha256"
              want="$(sha256sum requirements.txt | cut -d' ' -f1)"
              if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
                echo "Installing requirements.txt into venv"
                pip install --quiet --upgrade pip
                pip install --quiet -r requirements.txt && echo "$want" > "$stamp"
              fi

              # Whisper model (ggml). One-time download into ./models.
              export WHISPER_MODEL_NAME="''${WHISPER_MODEL_NAME:-base.en}"
              export WHISPER_MODEL="$PWD/models/ggml-$WHISPER_MODEL_NAME.bin"
              if [ ! -f "$WHISPER_MODEL" ]; then
                mkdir -p "$PWD/models"
                whisper-cpp-download-ggml-model "$WHISPER_MODEL_NAME" "$PWD/models"
              fi
            '';
          };
        }
      );

      formatter = forEachSupportedSystem ({ pkgs, ... }: pkgs.nixfmt);

      # Production package (nixpkgs Python, whisper model baked in): nix build .#default
      packages = forEachSupportedSystem (
        { pkgs, ... }:
        {
          default = pkgs.callPackage ./nix/package.nix { };
        }
      );

      # services.pebble-ring. The module builds the package with the *importing* system's
      # pkgs, so a NixOS config doesn't pull this flake's nixpkgs into its closure.
      nixosModules.default = ./nix/module.nix;
    };
}
