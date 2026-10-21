{
  lib,
  python3Packages,
  makeWrapper,
  curl,
  p7zip,
  android-tools,
  aapt,
}:
python3Packages.buildPythonApplication {
  pname = "rookie-cli";
  version = "1.0.0";
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
      --prefix PATH : ${lib.makeBinPath [curl p7zip android-tools aapt]}
  '';
  pythonImportsCheck = ["rookie"];
  meta = {
    description = "ROOKIE mirror downloader and Quest sideloader";
    mainProgram = "rookie";
    platforms = lib.platforms.linux;
  };
}
