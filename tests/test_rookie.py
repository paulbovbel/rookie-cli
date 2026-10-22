"""Offline mirror and headset tests for the Rookie CLI."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import rookie


STORAGE_OUTPUT = "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/data 1048576 524288 524288 50% /data\n"


def response(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout)


class DeviceTestCase(unittest.TestCase):
    def setUp(self):
        selector = patch(
            "rookie.select_device", side_effect=lambda serial=None: serial or "quest"
        )
        selector.start()
        self.addCleanup(selector.stop)


class RangeHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if getattr(self.server, "require_auth", False) and (
            self.headers.get("X-API-Key") != "test-api-key"
            or self.headers.get("User-Agent") != "rclone/v1.75.1"
        ):
            self.send_error(403)
            return
        if "Range" not in self.headers:
            return super().do_GET()
        path = Path(self.translate_path(self.path))
        offset = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
        data = path.read_bytes()
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {offset}-{len(data) - 1}/{len(data)}")
        self.send_header("Content-Length", str(len(data) - offset))
        self.end_headers()
        self.wfile.write(data[offset:])


class MirrorTests(unittest.TestCase):
    def test_extraction_publishes_only_complete_release(self):
        release = {"release": "Example v1"}
        identifier = rookie.release_id(release["release"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / identifier
            destination.mkdir()
            (destination / "stale.apk").write_bytes(b"old")

            def extract(archive, staged, password):
                staged.mkdir()
                (staged / "new.apk").write_bytes(b"new")

            with (
                patch(
                    "rookie.subprocess.run",
                    return_value=response(f"{identifier}.7z.001"),
                ),
                patch("rookie.download"),
                patch("rookie.extract", side_effect=extract),
                redirect_stdout(io.StringIO()),
            ):
                rookie.fetch_release(
                    "https://example.com/", "password", release, root / "cache", root
                )
            self.assertEqual([path.name for path in destination.iterdir()], ["new.apk"])
            with (
                patch(
                    "rookie.subprocess.run",
                    return_value=response(f"{identifier}.7z.001"),
                ),
                patch("rookie.download"),
                patch("rookie.extract", side_effect=ValueError("bad archive")),
            ):
                with self.assertRaisesRegex(ValueError, "bad archive"):
                    rookie.fetch_release(
                        "https://example.com/",
                        "password",
                        release,
                        root / "cache",
                        root,
                    )
            self.assertEqual((destination / "new.apk").read_bytes(), b"new")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mirror = self.root / "mirror"
        self.mirror.mkdir()
        self.cache = self.root / "cache"
        handler = functools.partial(RangeHandler, directory=str(self.mirror))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}/"

    @unittest.skipUnless(shutil.which("7z"), "7z is required")
    def test_extraction_streams_progress_to_stderr_and_keeps_stdout_clean(self):
        payload = self.root / "payload"
        payload.write_bytes(b"archive payload" * 10000)
        archive = self.root / "payload.7z"
        subprocess.run(
            ["7z", "a", "-ptest-password", str(archive), str(payload)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        destination = self.root / "extracted"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys, rookie; rookie.extract(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])",
                str(archive),
                str(destination),
                "test-password",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "")
        self.assertIn("Extracting payload.7z", result.stderr)
        self.assertRegex(result.stderr, r"\d+%")
        self.assertNotIn("test-password", result.stderr)
        self.assertEqual((destination / "payload").read_bytes(), payload.read_bytes())

    @unittest.skipUnless(shutil.which("curl"), "curl is required")
    def test_resume_and_atomic_download(self):
        data = b"download payload" * 100
        (self.mirror / "archive").write_bytes(data)
        self.cache.mkdir()
        target = self.cache / "archive"
        partial = self.cache / "archive.part"
        partial.write_bytes(data[:57])
        rookie.download(self.base + "archive", target)
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(partial.exists())

    @unittest.skipUnless(shutil.which("curl"), "curl is required")
    def test_failed_download_does_not_publish(self):
        target = self.cache / "missing"
        with self.assertRaises(subprocess.CalledProcessError):
            rookie.download(self.base + "missing", target)
        self.assertFalse(target.exists())

    @unittest.skipUnless(
        shutil.which("curl") and shutil.which("7z"), "curl and 7z are required"
    )
    def test_catalog_download_extract_search_and_release(self):
        environment = patch.dict("os.environ", {"ROOKIE_API_KEY": "test-api-key"})
        environment.start()
        self.addCleanup(environment.stop)
        self.server.require_auth = True
        source = self.root / "source"
        source.mkdir()
        release = "Example Game v1"
        identifier = rookie.release_id(release)
        catalog = source / "GameList.txt"
        catalog.write_text(
            "name;release;package;version;updated;size\n"
            f"Example Game;{release};com.example.game;1;2026-10-20;123\n"
        )
        subprocess.run(
            ["7z", "a", "-ptest-password", str(self.mirror / "meta.7z"), str(catalog)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "baseUri": self.base,
                    "password": base64.b64encode(b"test-password").decode(),
                }
            )
        )
        common = ["--config", str(config), "--cache-dir", str(self.cache)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rookie.main([*common, "update"]), 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(rookie.main([*common, "search", "example", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["id"], identifier)
        payload = source / "game.apk"
        payload.write_bytes(b"example APK")
        remote = self.mirror / identifier
        remote.mkdir()
        subprocess.run(
            [
                "7z",
                "a",
                "-ptest-password",
                "-v1k",
                str(remote / f"{identifier}.7z"),
                str(payload),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        destination = self.root / "downloads"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                rookie.main(
                    [*common, "download", identifier, "--output", str(destination)]
                ),
                0,
            )
        self.assertEqual(
            (destination / identifier / "game.apk").read_bytes(), b"example APK"
        )


class DeviceSelectionTests(unittest.TestCase):
    def test_installed_pins_first_device_for_app_and_storage_queries(self):
        responses = [
            subprocess.CompletedProcess(
                [], 0, "List of devices attached\nfirst\tdevice\nsecond\tdevice\n"
            ),
            subprocess.CompletedProcess([], 0, "package:com.example.game\n"),
            subprocess.CompletedProcess(
                [],
                0,
                "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/data 1000 500 500 50% /data\n",
            ),
        ]
        with (
            patch("rookie.subprocess.run", side_effect=responses) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(rookie.main(["installed"]), 0)
        self.assertEqual(
            [item.args[0] for item in run.call_args_list],
            [
                ["adb", "devices"],
                ["adb", "-s", "first", "shell", "pm", "list", "packages", "-3"],
                ["adb", "-s", "first", "shell", "df", "-k", "/data"],
            ],
        )

    def test_first_authorized_device_skips_offline_and_unauthorized(self):
        response = subprocess.CompletedProcess(
            [],
            0,
            "List of devices attached\noffline-device\toffline\nlocked-device\tunauthorized\nfirst\tdevice\nsecond\tdevice\n",
        )
        with patch("rookie.subprocess.run", return_value=response) as run:
            self.assertEqual(rookie.select_device(), "first")
        run.assert_called_once_with(
            ["adb", "devices"], check=True, capture_output=True, text=True
        )

    def test_explicit_serial_does_not_enumerate_devices(self):
        with patch("rookie.subprocess.run") as run:
            self.assertEqual(rookie.select_device("chosen"), "chosen")
        run.assert_not_called()

    def test_no_authorized_device_fails_before_querying_apps(self):
        response = subprocess.CompletedProcess(
            [], 0, "List of devices attached\nlocked\tunauthorized\n"
        )
        output = io.StringIO()
        with (
            patch("rookie.subprocess.run", return_value=response) as run,
            redirect_stderr(output),
        ):
            self.assertEqual(rookie.main(["installed"]), 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("No authorized ADB devices", output.getvalue())

    def test_adb_failure_includes_stdout_and_stderr(self):
        error = subprocess.CalledProcessError(
            1,
            ["adb", "devices"],
            output="ADB stdout diagnostic\n",
            stderr="error: device offline\n",
        )
        output = io.StringIO()
        with patch("rookie.subprocess.run", side_effect=error), redirect_stderr(output):
            self.assertEqual(rookie.main(["installed"]), 1)
        self.assertIn("adb failed (exit 1)", output.getvalue())
        self.assertIn("ADB stdout diagnostic", output.getvalue())
        self.assertIn("error: device offline", output.getvalue())


class InstalledTests(DeviceTestCase):
    storage_output = STORAGE_OUTPUT
    storage = {
        "total_bytes": 1073741824,
        "used_bytes": 536870912,
        "available_bytes": 536870912,
        "used_percent": 50.0,
    }

    def test_list_apps_without_mirror_configuration(self):
        output = io.StringIO()
        response = subprocess.CompletedProcess(
            [],
            0,
            "package:com.example.two\r\npackage:com.example.one\r\npackage:com.example.two\r\n",
        )
        with (
            patch(
                "rookie.subprocess.run",
                side_effect=[
                    response,
                    subprocess.CompletedProcess([], 0, self.storage_output),
                ],
            ) as run,
            patch(
                "rookie.load_config",
                side_effect=AssertionError("must not load mirror config"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(rookie.main(["installed", "--serial", "quest"]), 0)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["adb", "-s", "quest", "shell", "pm", "list", "packages", "-3"],
        )
        run.assert_called_with(
            ["adb", "-s", "quest", "shell", "df", "-k", "/data"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            output.getvalue(),
            "com.example.one\ncom.example.two\n\nStorage: 0.5 GiB / 1.0 GiB used (50.0%); 0.5 GiB free\n",
        )

    def test_all_apps_json_and_empty_headset(self):
        for stdout, expected in (
            ("package:com.android.settings\n", ["com.android.settings"]),
            ("", []),
        ):
            with self.subTest(stdout=stdout):
                output = io.StringIO()
                response = subprocess.CompletedProcess([], 0, stdout)
                with (
                    patch(
                        "rookie.subprocess.run",
                        side_effect=[
                            response,
                            subprocess.CompletedProcess([], 0, self.storage_output),
                        ],
                    ) as run,
                    redirect_stdout(output),
                ):
                    self.assertEqual(rookie.main(["installed", "--all", "--json"]), 0)
                self.assertEqual(
                    run.call_args_list[0].args[0],
                    ["adb", "-s", "quest", "shell", "pm", "list", "packages"],
                )
                run.assert_called_with(
                    ["adb", "-s", "quest", "shell", "df", "-k", "/data"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"packages": expected, "storage": self.storage},
                )

    def test_invalid_storage_does_not_publish_a_partial_report(self):
        output = io.StringIO()
        for stdout in ("invalid output", "/dev/data 0 0 0 0% /data"):
            with self.subTest(stdout=stdout):
                responses = [
                    subprocess.CompletedProcess([], 0, "package:com.example.game\n"),
                    subprocess.CompletedProcess([], 0, stdout),
                ]
                with (
                    patch("rookie.subprocess.run", side_effect=responses),
                    redirect_stdout(output),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(rookie.main(["installed", "--json"]), 1)
        self.assertEqual(output.getvalue(), "")

    def test_device_failure_does_not_print_an_empty_list(self):
        output = io.StringIO()
        error = subprocess.CalledProcessError(1, ["adb"])
        with (
            patch("rookie.subprocess.run", side_effect=error),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(rookie.main(["installed", "--json"]), 1)
        self.assertEqual(output.getvalue(), "")


class UninstallTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.args = [
            "--cache-dir",
            str(self.cache),
            "--config",
            str(self.cache / "missing.json"),
        ]

    def catalog(self):
        (self.cache / "VRP-GameList.txt").write_text(
            "name;release;package;version;updated;size\n"
            "Example Game;Example Game v1;com.example.game;1;2026-10-20;123\n"
            "Example Game Two;Example Game Two v1;com.example.two;1;2026-10-20;123\n"
        )

    def test_package_uninstall_without_catalog_or_config(self):
        with patch("rookie.subprocess.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(
                rookie.main(
                    [*self.args, "uninstall", "com.example.game", "--serial", "quest"]
                ),
                0,
            )
        run.assert_called_once_with(
            ["adb", "-s", "quest", "uninstall", "com.example.game"], check=True
        )

    def test_cached_release_id_and_unambiguous_name(self):
        self.catalog()
        for query in (rookie.release_id("Example Game v1"), "Game Two"):
            with self.subTest(query=query):
                with (
                    patch("rookie.subprocess.run") as run,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(rookie.main([*self.args, "uninstall", query]), 0)
                package = (
                    "com.example.two" if query == "Game Two" else "com.example.game"
                )
                run.assert_called_once_with(
                    ["adb", "-s", "quest", "uninstall", package], check=True
                )

    def test_ambiguous_or_missing_game_never_calls_adb(self):
        self.catalog()
        for query in ("Example", "Missing Game"):
            with self.subTest(query=query):
                with (
                    patch("rookie.subprocess.run") as run,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(rookie.main([*self.args, "uninstall", query]), 1)
                run.assert_not_called()

    def test_missing_catalog_never_calls_adb(self):
        output = io.StringIO()
        with patch("rookie.subprocess.run") as run, redirect_stderr(output):
            self.assertEqual(rookie.main([*self.args, "uninstall", "Example"]), 1)
        run.assert_not_called()
        self.assertIn("rookie update", output.getvalue())

    def test_failed_adb_does_not_report_success(self):
        error = subprocess.CalledProcessError(
            1, ["adb", "uninstall", "com.example.game"]
        )
        output = io.StringIO()
        with (
            patch("rookie.subprocess.run", side_effect=error),
            redirect_stderr(io.StringIO()),
            redirect_stdout(output),
        ):
            self.assertEqual(
                rookie.main([*self.args, "uninstall", "com.example.game"]), 1
            )
        self.assertNotIn("Uninstalled", output.getvalue())


class ApkMatchTests(unittest.TestCase):
    def test_hash_match_and_changed_apk(self):
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "game.apk"
            apk.write_bytes(b"local APK")
            for digest, expected in (
                (hashlib.sha256(b"local APK").hexdigest(), True),
                ("0" * 64, False),
            ):
                with self.subTest(expected=expected):
                    responses = [
                        subprocess.CompletedProcess(
                            [], 0, "package: name='com.example.game' versionCode='1'\n"
                        ),
                        subprocess.CompletedProcess(
                            [], 0, "package:com.example.game\n"
                        ),
                        subprocess.CompletedProcess(
                            [], 0, "package:/data/app/example/base.apk\n"
                        ),
                        subprocess.CompletedProcess(
                            [], 0, f"{digest}  /data/app/example/base.apk\n"
                        ),
                    ]
                    with patch("rookie.subprocess.run", side_effect=responses) as run:
                        self.assertEqual(
                            rookie.apk_matches(apk, ["adb", "-s", "quest"]), expected
                        )
                    self.assertEqual(
                        run.call_args_list[-1].args[0],
                        [
                            "adb",
                            "-s",
                            "quest",
                            "shell",
                            "sha256sum",
                            "/data/app/example/base.apk",
                        ],
                    )

    def test_missing_package_does_not_hash_remote(self):
        responses = [
            subprocess.CompletedProcess([], 0, "package: name='com.example.game'\n"),
            subprocess.CompletedProcess([], 0, ""),
        ]
        with patch("rookie.subprocess.run", side_effect=responses) as run:
            self.assertFalse(
                rookie.apk_matches(Path("game.apk"), ["adb", "-s", "quest"])
            )
        self.assertEqual(run.call_count, 2)

    def test_identical_apk_skips_install_but_still_syncs_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / "game.apk"
            apk.touch()
            resources = root / "com.example.game"
            resources.mkdir()
            (resources / "main.1.com.example.game.obb").touch()
            with (
                patch("rookie.apk_matches", return_value=True),
                patch(
                    "rookie.headset_storage", return_value={"available_bytes": 1024**3}
                ),
                patch("rookie.subprocess.run") as run,
                redirect_stdout(io.StringIO()),
            ):
                rookie.install(root, "quest")
            commands = [item.args[0] for item in run.call_args_list]
            self.assertFalse(any("install" in command for command in commands))
            self.assertIn(
                [
                    "adb",
                    "-s",
                    "quest",
                    "push",
                    "--sync",
                    str(resources),
                    "/sdcard/Android/obb/",
                ],
                commands,
            )

    def test_identical_apk_without_resources_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "game.apk").touch()
            with (
                patch("rookie.apk_matches", return_value=True),
                patch("rookie.subprocess.run") as run,
                redirect_stdout(io.StringIO()),
            ):
                rookie.install(root, "quest")
            run.assert_not_called()


class CatalogTests(unittest.TestCase):
    def test_request_settings_ignore_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "mirror.json"
            config.write_text(
                json.dumps(
                    {
                        "baseUri": "https://example.com/",
                        "password": base64.b64encode(b"password").decode(),
                        "apiKey": "config-key",
                        "userAgent": "custom-agent",
                    }
                )
            )
            with patch.dict("os.environ", {"ROOKIE_API_KEY": "override-key"}):
                _, _, options = rookie.load_config(config)
            self.assertEqual(
                options, {"api_key": "override-key", "user_agent": rookie.USER_AGENT}
            )
            with patch.dict("os.environ", {}, clear=True):
                _, _, options = rookie.load_config(config)
            self.assertEqual(
                options, {"api_key": "", "user_agent": rookie.USER_AGENT}
            )

    def test_network_errors_do_not_print_api_key(self):
        error = subprocess.CalledProcessError(
            22, ["curl", "--header", "X-API-Key: test-api-key"]
        )
        output = io.StringIO()
        with patch("rookie.load_config", side_effect=error), redirect_stderr(output):
            self.assertEqual(rookie.main(["--config", "/unused/config.json", "update"]), 1)
        self.assertIn("curl failed (exit 22)", output.getvalue())
        self.assertNotIn("test-api-key", output.getvalue())

    def test_release_id_includes_newline(self):
        self.assertEqual(rookie.release_id(""), "68b329da9893e34099c7d8ad5cb9c940")

    def test_listing_sorted_deduplicated_and_rejects_gaps(self):
        identifier = "a" * 32
        first, second = f"{identifier}.7z.001", f"{identifier}.7z.002"
        self.assertEqual(
            rookie.archive_parts(
                f'<a href="{second}">{second}</a>\n{first}', identifier
            ),
            [first, second],
        )
        with self.assertRaisesRegex(ValueError, "missing archive parts"):
            rookie.archive_parts(second, identifier)

    def test_ambiguous_names_require_id(self):
        releases = [{"name": "Example", "release": f"Example v{n}"} for n in (1, 2)]
        with self.assertRaisesRegex(ValueError, "found 2"):
            rookie.select_release(releases, "Example")
        self.assertEqual(
            rookie.select_release(releases, rookie.release_id("Example v2")),
            releases[1],
        )


class InstallTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        matcher = patch("rookie.apk_matches", return_value=False)
        matcher.start()
        self.addCleanup(matcher.stop)

    def test_space_check_override_still_installs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "game.apk").touch()
            with (
                patch(
                    "rookie.headset_storage",
                    side_effect=AssertionError("must skip storage query"),
                ),
                patch("rookie.subprocess.run") as run,
            ):
                self.assertEqual(
                    rookie.main(["install", str(root), "--skip-space-check"]), 0
                )
            run.assert_called_once_with(
                ["adb", "-s", "quest", "install", "-r", str(root / "game.apk")],
                check=True,
            )

    def test_adb_install_and_obb_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / "game.apk"
            apk.touch()
            obb = root / "com.example.game" / "main.1.com.example.game.obb"
            obb.parent.mkdir()
            obb.touch()
            (obb.parent / "song-without-extension").write_bytes(b"DLC data")
            (obb.parent / "patch.1.com.example.game.obb").touch()
            nested = obb.parent / "nested resources"
            nested.mkdir()
            (nested / "resource").write_bytes(b"nested DLC data")
            response = subprocess.CompletedProcess(
                [],
                0,
                "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/data 1000000 0 1000000 0% /data\n",
            )
            with patch("rookie.subprocess.run", return_value=response) as run:
                rookie.install(root, "quest")
            self.assertEqual(
                run.call_args_list[0].args[0],
                ["adb", "-s", "quest", "shell", "df", "-k", "/data"],
            )
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["adb", "-s", "quest", "install", "-r", str(apk)],
            )
            self.assertEqual(
                [item.args[0] for item in run.call_args_list[2:-1]],
                [
                    [
                        "adb",
                        "-s",
                        "quest",
                        "shell",
                        "mkdir",
                        "-p",
                        "/sdcard/Android/obb/com.example.game",
                        "'/sdcard/Android/obb/com.example.game/nested resources'",
                    ],
                ],
            )
            self.assertEqual(
                run.call_args_list[-1].args[0],
                [
                    "adb",
                    "-s",
                    "quest",
                    "push",
                    "--sync",
                    str(obb.parent),
                    "/sdcard/Android/obb/",
                ],
            )
            self.assertEqual(
                sum("push" in item.args[0] for item in run.call_args_list), 1
            )

    def test_install_stops_before_changes_when_space_is_insufficient_or_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "game.apk").write_bytes(b"a" * 1024)
            obb = root / "com.example.game" / "main.1.com.example.game.obb"
            obb.parent.mkdir()
            obb.write_bytes(b"b" * 2048)
            (obb.parent / "dlc-without-extension").write_bytes(b"c" * 4096)
            for stdout, message in (
                (
                    "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/data 1000000 737849 262151 74% /data\n",
                    "Not enough space",
                ),
                ("unexpected df output\n", "Cannot determine free space"),
                ("", "Cannot determine free space"),
            ):
                with self.subTest(stdout=stdout):
                    response = subprocess.CompletedProcess([], 0, stdout)
                    with patch("rookie.subprocess.run", return_value=response) as run:
                        with self.assertRaisesRegex(ValueError, message):
                            rookie.install(root)
                    run.assert_called_once_with(
                        ["adb", "-s", "quest", "shell", "df", "-k", "/data"],
                        check=True,
                        capture_output=True,
                        text=True,
                    )


if __name__ == "__main__":
    unittest.main()
