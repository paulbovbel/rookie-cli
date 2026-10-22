# ROOKIE CLI

Download ROOKIE mirror releases and manage Quest apps over ADB. Requires Python
3.11 or newer, curl, 7-Zip (`7z`), ADB, and `aapt2` on PATH.

Install with `pip install .`, or run `nix run . -- <command>` to include all
runtime dependencies. Build with `nix build`.

## Usage

```bash
rookie update
rookie search "game name"
rookie download <release-id> --output ~/Games/Quest --install
rookie install ~/Games/Quest/<release-id> --serial <adb-device-serial>
rookie installed
rookie uninstall com.example.game
```

- Search fetches the catalog on first use; `update` refreshes it. Downloads accept
  release IDs or unambiguous game names.
- Downloads resume automatically and retain archives in `$XDG_CACHE_HOME/rookie`
  (default `~/.cache/rookie`). Extraction defaults to `./rookie-downloads/<release-id>/`.
  Use `--no-extract` for archives only.
- ADB requires an authorized, developer-mode headset. Commands use the first
  authorized device; use `--serial` to select another (`adb devices` lists them).
- Installation checks free space, skips identical APKs, and syncs OBB resource
  directories, including DLC. The space estimate counts all resources, even those
  already installed. Split APKs and custom `install.txt` steps require manual installation.
  Use `--skip-space-check` with `install` or `download --install` to bypass the
  conservative estimate when replacing existing resources.
- `installed` lists non-system packages and headset storage; `--all` includes
  system apps. Both `search` and `installed` support `--json`.
- `uninstall` removes the app and its local data. It also accepts release IDs or
  unambiguous names from the cached catalog. PC downloads are retained.

## Configuration

The JSON contains `baseUri`, a Base64-encoded `password`, and optional `apiKey`
and `userAgent` fields. Preserve the current mirror's API key and user agent;
incorrect values can cause HTTP 403.

Provide the config with `ROOKIE_CONFIG` or `--config <json>`, and override the API key
with `ROOKIE_API_KEY`. Global flags (`--config`, `--cache-dir`) precede the
subcommand. See `rookie <command> --help` for command options.

Nix consumers can configure a default path with
`rookie-cli.packages.${system}.default.override { configPath = "/path/to/mirror.json"; }`.
This sets a wrapper default for `ROOKIE_CONFIG`; the environment and `--config`
can override it. The config file itself is read at runtime.

## Development

Run `nix develop`, then `python -m unittest discover -s tests`.
Tests use a local mirror fixture and mocked ADB calls. `nix flake check` builds
the package and runs the tests with their external dependencies.

GitHub Actions runs tests in the Nix development environment, checks and builds
the flake, and smoke-tests the packaged CLI. Keep `flake.lock` committed to pin
the environment. Dependabot groups weekly Nix and GitHub Actions updates into
one multi-ecosystem PR.
