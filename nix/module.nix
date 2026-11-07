# NixOS module: services.pebble-ring
#
# Secrets are declared one option per key under `secretFiles`, so the full list of keys
# the service needs lives here. Each is a path to a file holding only the value (e.g. a
# sops-nix secret path). They reach the process as systemd credentials (LoadCredential),
# never as Environment= lines, and app/__init__.py turns them into env vars.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.pebble-ring;
  inherit (lib) mkOption types;

  secretOption =
    { required, description }:
    mkOption {
      type = if required then types.path else types.nullOr types.path;
      description = "File containing ${description}." + lib.optionalString (!required) " Optional.";
    }
    // lib.optionalAttrs (!required) { default = null; };

  credentials = lib.filterAttrs (_: p: p != null) cfg.secretFiles;
  stateDir = "/var/lib/pebble-ring";
in
{
  options.services.pebble-ring = {
    enable = lib.mkEnableOption "pebble-ring webhook receiver for the Index 01 ring";

    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Address uvicorn binds to. Expose it with a reverse proxy / tailscale serve.";
    };

    port = mkOption {
      type = types.port;
      default = 8000;
    };

    whisperModel = mkOption {
      type = types.path;
      default = cfg.package.passthru.whisperModel;
      defaultText = lib.literalExpression "cfg.package.passthru.whisperModel";
      description = "ggml whisper model file.";
    };

    keepAudio = mkOption {
      type = types.bool;
      default = false;
      description = "Keep .m4a files after processing. Off by default: the phone already has them.";
    };

    settings = mkOption {
      type = types.attrsOf (
        types.oneOf [
          types.str
          types.int
          types.float
          types.bool
        ]
      );
      default = { };
      example = {
        LLM_URL = "http://127.0.0.1:11434/v1";
        NOTES_TZ = "America/Los_Angeles";
        HA_DRY_RUN = true;
      };
      description = ''
        Non-secret environment variables (see the Configuration table in CLAUDE.md).
        Booleans become "1"/"0". Do not put tokens here: they would land in the Nix store.
      '';
    };

    secretFiles = {
      INDEX_WEBHOOK_SECRET = secretOption {
        required = true;
        description = "the ring's \"Sign requests\" HMAC secret";
      };
      HA_TOKEN = secretOption {
        required = false;
        description = "a Home Assistant long-lived access token (needed for the `home` category)";
      };
      GITEA_TOKEN = secretOption {
        required = false;
        description = "a Gitea token with write:repository scope (needed for the `note` category)";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.pebble-ring = {
      description = "pebble-ring webhook receiver";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      environment =
        lib.mapAttrs (_: v: if lib.isBool v then (if v then "1" else "0") else toString v) cfg.settings
        // {
          RECORDINGS_DIR = "${stateDir}/recordings";
          WHISPER_MODEL = toString cfg.whisperModel;
          KEEP_AUDIO = if cfg.keepAudio then "1" else "0";
          PYTHONUNBUFFERED = "1"; # results are printed; get them into the journal immediately
        };

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} --host ${cfg.host} --port ${toString cfg.port}";
        LoadCredential = lib.mapAttrsToList (name: path: "${name}:${path}") credentials;
        Restart = "on-failure";
        RestartSec = 5;

        DynamicUser = true;
        StateDirectory = "pebble-ring";
        StateDirectoryMode = "0700";
        WorkingDirectory = stateDir;

        # Hardening. whisper/ffmpeg only need CPU, the state dir and the network.
        CapabilityBoundingSet = "";
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        SystemCallArchitectures = "native";
        UMask = "0077";
      };
    };
  };
}
