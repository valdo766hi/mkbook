{ pkgs, ... }:

{
  packages = with pkgs; [
    epubcheck
    git
  ];

  languages.python = {
    enable = true;
    package = pkgs.python312;
    venv.enable = true;
    uv = {
      enable = true;
      package = pkgs.uv;
      sync = {
        enable = true;
        arguments = [ "--frozen" ];
      };
    };
  };
}
