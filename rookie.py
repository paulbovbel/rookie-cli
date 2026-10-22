"""Download ROOKIE mirror archives and manage Quest apps with ADB."""

import argparse
import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlparse


USER_AGENT = "rclone/v1.75.1"


def release_id(release):
    return hashlib.md5((release + "\n").encode(), usedforsecurity=False).hexdigest()


def load_config(path):
    data = json.loads(path.read_text())
    base = data["baseUri"]
    if urlparse(base).scheme not in ("http", "https") or not urlparse(base).netloc:
        raise ValueError("baseUri must be an HTTP(S) URL")
    password = base64.b64decode(data["password"], validate=True).decode()
    if not password:
        raise ValueError("Mirror password is empty")
    request_options = {
        "api_key": os.environ.get("ROOKIE_API_KEY", ""),
        "user_agent": USER_AGENT,
    }
    for value in request_options.values():
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("Mirror request settings must be single-line strings")
    return base.rstrip("/") + "/", password, request_options


def curl_args(url, api_key="", user_agent=USER_AGENT):
    return [
        "curl",
        "--fail",
        "--location",
        "--retry",
        "3",
        "--connect-timeout",
        "30",
        "--speed-limit",
        "1",
        "--speed-time",
        "120",
        "--user-agent",
        user_agent,
        *(["--header", f"X-API-Key: {api_key}"] if api_key else []),
        url,
    ]


def download(url, destination, **request_options):
    """Keep partial transfers separate and resume them on subsequent attempts."""
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    subprocess.run(
        [
            *curl_args(url, **request_options),
            "--continue-at",
            "-",
            "--output",
            str(partial),
        ],
        check=True,
    )
    partial.replace(destination)


def archive_parts(html, identifier):
    names = set(re.findall(rf"\b{re.escape(identifier)}\.7z\.[0-9]+\b", html))
    parts = sorted(names, key=lambda name: int(name.rsplit(".", 1)[1]))
    numbers = [int(name.rsplit(".", 1)[1]) for name in parts]
    if not parts or numbers != list(range(1, len(parts) + 1)):
        raise ValueError("Mirror listing has no archives or is missing archive parts")
    return parts


def extract(archive, destination, password):
    destination.mkdir(parents=True, exist_ok=True)
    print(f"Extracting {archive.name} to {destination}", file=sys.stderr, flush=True)
    result = subprocess.run(
        [
            "7z",
            "x",
            "-y",
            "-bsp2",
            "-bse1",
            f"-p{password}",
            f"-o{destination}",
            str(archive),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        # Do not include the command (and thus its password) in error messages.
        raise ValueError(f"Archive extraction failed:\n{result.stdout}")


def read_catalog(path):
    releases = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream, delimiter=";")
        next(rows, None)
        for number, row in enumerate(rows, 2):
            if not row:
                continue
            if len(row) < 6 or not row[1]:
                raise ValueError(f"Invalid catalog row {number}")
            releases.append(
                dict(
                    zip(
                        ("name", "release", "package", "version", "updated", "size"),
                        row[:6],
                    )
                )
            )
    if not releases:
        raise ValueError("Mirror catalog is empty")
    return releases


def update_catalog(base, password, cache, **request_options):
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        directory = Path(temporary)
        archive = directory / "meta.7z"
        download(base + "meta.7z", archive, **request_options)
        extract(archive, directory / "metadata", password)
        catalogs = sorted((directory / "metadata").glob("*ameList.txt"))
        if len(catalogs) != 1:
            raise ValueError(
                "Expected one *ameList.txt catalog in the metadata archive"
            )
        catalog = catalogs[0]
        releases = read_catalog(catalog)
        catalog.replace(cache / "VRP-GameList.txt")
    return releases


def select_release(releases, query):
    exact = [r for r in releases if query in (release_id(r["release"]), r["release"])]
    matches = exact or [r for r in releases if query.casefold() in r["name"].casefold()]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one release, found {len(matches)}; use an ID from 'rookie search'"
        )
    return matches[0]


def fetch_release(
    base, password, release, cache, output, extract_files=True, **request_options
):
    identifier = release_id(release["release"])
    directory_url = base + quote(identifier) + "/"
    response = subprocess.run(
        [*curl_args(directory_url, **request_options), "--silent", "--show-error"],
        check=True,
        capture_output=True,
        text=True,
    )
    parts = archive_parts(response.stdout, identifier)
    for name in parts:
        print(f"Downloading {name}", flush=True)
        download(
            directory_url + quote(name), cache / identifier / name, **request_options
        )
    if extract_files:
        # Isolate each release so multiple downloads cannot overwrite one another.
        destination = output / identifier
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            staged = Path(temporary) / "extracted"
            backup = Path(temporary) / "previous"
            extract(cache / identifier / parts[0], staged, password)
            if destination.exists():
                destination.rename(backup)
            try:
                staged.rename(destination)
            except OSError:
                if backup.exists():
                    backup.rename(destination)
                raise
        print(f"Extracted to {destination}")
        return destination
    print(f"Archives saved to {cache / identifier}")
    return None


def select_device(serial=None):
    if serial:
        return serial
    result = subprocess.run(
        ["adb", "devices"], check=True, capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            return fields[0]
    raise ValueError(
        "No authorized ADB devices available; connect and authorize a headset or specify --serial"
    )


def headset_storage(serial=None):
    adb = ["adb", "-s", select_device(serial)]
    result = subprocess.run(
        [*adb, "shell", "df", "-k", "/data"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = result.stdout.splitlines()
    fields = rows[-1].split() if rows else []
    if len(fields) < 6 or not all(value.isdigit() for value in fields[1:4]):
        raise ValueError("Cannot determine free space on headset /data partition")
    total, used, available = (int(value) * 1024 for value in fields[1:4])
    if total == 0:
        raise ValueError("Cannot determine free space on headset /data partition")
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": round(100 * used / total, 1),
    }


def apk_matches(apk, adb):
    metadata = subprocess.run(
        ["aapt2", "dump", "badging", str(apk)],
        check=True,
        capture_output=True,
        text=True,
    )
    package = re.search(
        r"^package: name='([A-Za-z_][A-Za-z0-9_.]*)'", metadata.stdout, re.MULTILINE
    )
    if not package:
        raise ValueError(f"Cannot determine Android package for {apk.name}")
    installed_packages = subprocess.run(
        [*adb, "shell", "pm", "list", "packages", package[1]],
        check=True,
        capture_output=True,
        text=True,
    )
    if f"package:{package[1]}" not in installed_packages.stdout.splitlines():
        return False
    result = subprocess.run(
        [*adb, "shell", "pm", "path", package[1]],
        check=True,
        capture_output=True,
        text=True,
    )
    paths = [
        line.removeprefix("package:").strip()
        for line in result.stdout.splitlines()
        if line.startswith("package:")
    ]
    if len(paths) != 1:
        return False
    remote = subprocess.run(
        [*adb, "shell", "sha256sum", shlex.quote(paths[0])],
        check=True,
        capture_output=True,
        text=True,
    )
    digest = remote.stdout.split()
    if not digest or not re.fullmatch(r"[0-9a-fA-F]{64}", digest[0]):
        raise ValueError(f"Cannot determine installed APK hash for {package[1]}")
    with apk.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == digest[0].lower()


def install(directory, serial=None, skip_space_check=False):
    apks = sorted(directory.rglob("*.apk"))
    obbs = sorted(directory.rglob("*.obb"))
    if not apks:
        raise ValueError(f"No APKs found in {directory}")
    # OBBs conventionally live under a directory named for their Android package.
    for obb in obbs:
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", obb.parent.name):
            raise ValueError(f"Cannot determine Android package for {obb.name}")
    resource_dirs = sorted({obb.parent for obb in obbs})
    serial = select_device(serial)
    adb = ["adb", "-s", serial]
    pending_apks = []
    for apk in apks:
        if apk_matches(apk, adb):
            print(f"Skipping unchanged APK: {apk.name}")
        else:
            pending_apks.append(apk)
    if not pending_apks and not resource_dirs:
        return
    required = (
        2 * sum(apk.stat().st_size for apk in pending_apks)
        + sum(
            resource.stat().st_size
            for resource_dir in resource_dirs
            for resource in resource_dir.rglob("*")
            if resource.is_file()
        )
        + 256 * 1024 * 1024
    )
    available = None if skip_space_check else headset_storage(serial)["available_bytes"]
    if available is not None and available < required:
        raise ValueError(
            f"Not enough space on headset: {available / 1024**2:.1f} MiB free, "
            f"{required / 1024**2:.1f} MiB required (including installation overhead); "
            "use --skip-space-check to bypass this conservative estimate"
        )
    for apk in pending_apks:
        subprocess.run([*adb, "install", "-r", str(apk)], check=True)
    for resource_dir in resource_dirs:
        remote = "/sdcard/Android/obb/"
        directories = [
            resource_dir,
            *sorted(path for path in resource_dir.rglob("*") if path.is_dir()),
        ]
        remote_dirs = []
        for local_dir in directories:
            remote_dir = f"{remote}{resource_dir.name}"
            if local_dir != resource_dir:
                remote_dir += f"/{local_dir.relative_to(resource_dir).as_posix()}"
            remote_dirs.append(shlex.quote(remote_dir))
        subprocess.run([*adb, "shell", "mkdir", "-p", *remote_dirs], check=True)
        subprocess.run([*adb, "push", "--sync", str(resource_dir), remote], check=True)


def uninstall(query, cache, serial=None):
    package_pattern = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    if re.fullmatch(package_pattern, query):
        package = query
    else:
        catalog = cache / "VRP-GameList.txt"
        if not catalog.exists():
            raise ValueError(
                "No cached catalog; run 'rookie update' or supply an Android package name"
            )
        package = select_release(read_catalog(catalog), query)["package"]
    if not re.fullmatch(package_pattern, package):
        raise ValueError(f"Invalid Android package name: {package!r}")
    adb = ["adb", "-s", select_device(serial)]
    subprocess.run([*adb, "uninstall", package], check=True)
    print(f"Uninstalled {package}")


def installed(serial=None, include_system=False):
    serial = select_device(serial)
    adb = ["adb", "-s", serial]
    result = subprocess.run(
        [*adb, "shell", "pm", "list", "packages", *([] if include_system else ["-3"])],
        check=True,
        capture_output=True,
        text=True,
    )
    packages = sorted(
        {
            line.removeprefix("package:").strip()
            for line in result.stdout.splitlines()
            if line.startswith("package:")
        }
    )
    return {"packages": packages, "storage": headset_storage(serial)}


def parser():
    result = argparse.ArgumentParser(prog="rookie", description=__doc__)
    result.add_argument(
        "--config",
        type=Path,
        default=os.environ.get("ROOKIE_CONFIG") or None,
        help="mirror JSON containing baseUri and a Base64-encoded password (env: ROOKIE_CONFIG)",
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "rookie",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("update", help="refresh the mirror catalog")
    search = commands.add_parser("search", help="search the cached catalog")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--json", action="store_true")
    fetch = commands.add_parser("download", help="download a release by ID or name")
    fetch.add_argument("query")
    fetch.add_argument("--output", type=Path, default=Path.cwd() / "rookie-downloads")
    fetch.add_argument("--no-extract", action="store_true")
    fetch.add_argument("--install", action="store_true", help="also sideload with ADB")
    fetch.add_argument("--serial", help="ADB device serial")
    fetch.add_argument(
        "--skip-space-check",
        action="store_true",
        help="bypass the conservative installation space estimate",
    )
    local = commands.add_parser("install", help="sideload an extracted directory")
    local.add_argument("directory", type=Path)
    local.add_argument("--serial", help="ADB device serial")
    local.add_argument(
        "--skip-space-check",
        action="store_true",
        help="bypass the conservative installation space estimate",
    )
    remove = commands.add_parser(
        "uninstall", help="remove an app and its local data from the headset"
    )
    remove.add_argument(
        "query",
        help="Android package name, cached release ID, or unambiguous game name",
    )
    remove.add_argument("--serial", help="ADB device serial")
    apps = commands.add_parser("installed", help="list apps installed on the headset")
    apps.add_argument("--serial", help="ADB device serial")
    apps.add_argument("--all", action="store_true", help="include system apps")
    apps.add_argument(
        "--json",
        action="store_true",
        help="output packages and storage utilization as JSON",
    )
    return result


def main(argv=None):
    cli = parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        cli.print_help()
        return 0
    args = cli.parse_args(argv)
    if args.command == "download" and args.install and args.no_extract:
        cli.error("--install cannot be combined with --no-extract")
    try:
        if args.command == "installed":
            report = installed(args.serial, args.all)
            if args.json:
                print(json.dumps(report))
            else:
                for package in report["packages"]:
                    print(package)
                storage = report["storage"]
                print(
                    f"\nStorage: {storage['used_bytes'] / 1024**3:.1f} GiB / "
                    f"{storage['total_bytes'] / 1024**3:.1f} GiB used "
                    f"({storage['used_percent']:.1f}%); "
                    f"{storage['available_bytes'] / 1024**3:.1f} GiB free"
                )
            return 0
        if args.command == "install":
            install(args.directory, args.serial, args.skip_space_check)
            return 0
        if args.command == "uninstall":
            uninstall(args.query, args.cache_dir, args.serial)
            return 0
        catalog = args.cache_dir / "VRP-GameList.txt"
        if args.command in ("update", "download") or not catalog.exists():
            if args.config is None:
                cli.error("mirror configuration required: use --config or ROOKIE_CONFIG")
            base, password, request_options = load_config(args.config)
        if args.command == "update" or not catalog.exists():
            releases = update_catalog(base, password, args.cache_dir, **request_options)
        else:
            releases = read_catalog(catalog)
        if args.command == "update":
            print(f"Catalog updated: {len(releases)} releases")
        elif args.command == "search":
            matches = [
                r for r in releases if args.query.casefold() in r["name"].casefold()
            ]
            if args.json:
                print(
                    json.dumps([dict(r, id=release_id(r["release"])) for r in matches])
                )
            else:
                for release in matches:
                    print(
                        f"{release_id(release['release'])}  {release['name']}  "
                        f"v{release['version']}  {release['size']} bytes"
                    )
        elif args.command == "download":
            release = select_release(releases, args.query)
            destination = fetch_release(
                base,
                password,
                release,
                args.cache_dir,
                args.output.resolve(),
                not args.no_extract,
                **request_options,
            )
            if args.install:
                install(destination, args.serial, args.skip_space_check)
        return 0
    except subprocess.CalledProcessError as error:
        print(
            f"rookie: {Path(error.cmd[0]).name} failed (exit {error.returncode})",
            file=sys.stderr,
        )
        if Path(error.cmd[0]).name == "adb":
            for output in (error.stdout, error.stderr):
                if output:
                    print(output.rstrip(), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"rookie: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted; rerun the command to resume downloads.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
