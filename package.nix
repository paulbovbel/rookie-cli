{
  lib,
  python3Packages,
  makeWrapper,
  curl,
  p7zip,
  android-tools,
  aapt,
  configPath ? null,
  apiKeyFile ? null,
}:
python3Packages.buildPythonApplication {
  pname = "rookie-cli";
  version = "0.0.1";
  src = lib.cleanSource ./.;
  pyproject = true;
  build-system = [python3Packages.setuptools];
  nativeBuildInputs = [makeWrapper];
  nativeCheckInputs = [curl p7zip];
  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests
    runHook postCheck
  '';
  postInstall = ''
    wrapProgram "$out/bin/rookie" \
      --prefix PATH : ${lib.makeBinPath [curl p7zip android-tools aapt]} \
      ${lib.optionalString (apiKeyFile != null) ''--set-default ROOKIE_API_KEY "$(cat ${lib.escapeShellArg (toString apiKeyFile)})"''} \
      ${lib.optionalString (configPath != null) "--set-default ROOKIE_CONFIG ${lib.escapeShellArg (toString configPath)}"}
  '';
  pythonImportsCheck = ["rookie"];
  meta = {
    description = "ROOKIE mirror downloader and Quest sideloader";
    mainProgram = "rookie";
    platforms = ["x86_64-linux"];
  };
}
