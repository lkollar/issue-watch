self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.issue-watch;
in
{
  options.services.issue-watch = {
    enable = lib.mkEnableOption "GitHub issue email digests";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The issue-watch package.";
    };
    configFile = lib.mkOption {
      type = lib.types.path;
      description = "TOML configuration path; must be readable by the service user.";
    };
    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Optional protected environment file containing GH_TOKEN.";
    };
    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/issue-watch";
      description = "Absolute state directory. Provision custom paths for the issue-watch user before starting.";
    };
    onCalendar = lib.mkOption {
      type = lib.types.str;
      default = "hourly";
      description = "Polling schedule in the machine's local timezone.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/" cfg.stateDirectory;
        message = "services.issue-watch.stateDirectory must be absolute";
      }
    ];
    users.groups.issue-watch = { };
    users.users.issue-watch = {
      isSystemUser = true;
      group = "issue-watch";
    };
    systemd.services.issue-watch = {
      description = "Send new GitHub issue matches by email";
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      environment.SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
      serviceConfig = {
        Type = "oneshot";
        User = "issue-watch";
        Group = "issue-watch";
        StateDirectory = lib.mkIf (cfg.stateDirectory == "/var/lib/issue-watch") "issue-watch";
        StateDirectoryMode = "0700";
        UMask = "0077";
        ExecStart = "${lib.getExe cfg.package} --config ${lib.escapeShellArg "${cfg.configFile}"} --state ${lib.escapeShellArg "${cfg.stateDirectory}/state.sqlite3"}";
        EnvironmentFile = lib.mkIf (cfg.environmentFile != null) (toString cfg.environmentFile);
        # sendmail needs its existing setuid wrapper to access the mail spool.
        PrivateTmp = true;
        TimeoutStartSec = "20min";
      };
    };
    systemd.timers.issue-watch = {
      description = "Poll GitHub issue searches";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.onCalendar;
        Persistent = true;
        RandomizedDelaySec = "5min";
      };
    };
  };
}
