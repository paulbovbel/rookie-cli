{fetchurl, runCommand, python3}:
let
  release = fetchurl {
    url = "https://github.com/nerdunit/androidsideloader/releases/download/v3.4/AndroidSideloader.exe";
    hash = "sha256-DkOXxus3sQemkwc2uJ5Ug2y/yw8zyZ+7sAvLdrg0XlE=";
  };
in
  runCommand "rookie-api-key-v3.4" {nativeBuildInputs = [python3];} ''
    python ${./extract-api-key.py} ${release} "$out"
  ''
