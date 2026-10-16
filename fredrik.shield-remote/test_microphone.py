import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import router


PLUGIN = Path(__file__).resolve().parent
SERVICE = "atvvoice-shield-remote.service"


class MicrophoneInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.config = self.base / "config"
        self.data = self.base / "data with spaces%"
        self.runtime = self.base / "runtime"
        self.tools = self.base / "tools"
        self.tools.mkdir()
        configuration = self.config / "fredrik.shield-remote"
        configuration.mkdir(parents=True)
        (configuration / "config.json").write_text(json.dumps({
            "schema": 4,
            "remote_address": "00:11:22:33:44:55",
            "shield_host": "",
            "paired_host": "",
            "tv_output": "",
            "soundbar_host": "",
        }))
        self.environment = {
            **os.environ,
            "PATH": f"{self.tools}:{os.environ['PATH']}",
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_DATA_HOME": str(self.data),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "INSTALL_LOG": str(self.base / "commands.jsonl"),
        }
        self.tool("cargo", """
            import json, os
            from pathlib import Path
            import sys
            with open(os.environ["INSTALL_LOG"], "a") as log:
                log.write(json.dumps(["cargo", *sys.argv[1:]]) + "\\n")
            if os.environ.get("FAIL_BUILD"):
                sys.exit("fixture build failed")
            target = Path(sys.argv[sys.argv.index("--target-dir") + 1])
            binary = target / "release/atvvoice"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\\necho fixture-atvvoice\\n")
            binary.chmod(0o755)
        """)
        self.tool("systemctl", """
            import json, os
            from pathlib import Path
            import sys
            with open(os.environ["INSTALL_LOG"], "a") as log:
                log.write(json.dumps(["systemctl", *sys.argv[1:]]) + "\\n")
            if "enable" in sys.argv:
                units = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user"
                link = units / "graphical-session.target.wants" / sys.argv[-1]
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.is_symlink():
                    link.symlink_to(units / sys.argv[-1])
        """)
        self.tool("pkexec", """
            import subprocess, sys
            if sys.argv[1:4] == ["/usr/bin/udevadm", "control", "--reload-rules"]:
                sys.exit(0)
            if sys.argv[1:4] != ["/usr/bin/install", "-m", "644"]:
                sys.exit("unexpected privileged operation")
            subprocess.run(sys.argv[1:], check=True)
        """)

    def tool(self, name, body):
        executable = self.tools / name
        executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        executable.chmod(0o755)

    def install(self):
        return subprocess.run(
            [sys.executable, str(PLUGIN / "router.py"), "install-microphone"],
            env=self.environment, capture_output=True, text=True, timeout=15,
        )

    def commands(self):
        log = self.base / "commands.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_install_builds_bundled_microphone_and_enables_remote_specific_service(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        application = self.data / "fredrik.shield-remote"
        executable = application / "bin/atvvoice"
        self.assertTrue(os.access(executable, os.X_OK))
        service = self.config / "systemd/user" / SERVICE
        unit = service.read_text()
        escaped = str(executable).replace("%", "%%")
        self.assertIn(f'ExecStart="{escaped}" --device 00:11:22:33:44:55 ', unit)
        self.assertIn('--name shield-remote --description "NVIDIA Shield Remote Microphone" --mic-on-demand', unit)
        self.assertIn("KillSignal=SIGINT", unit)
        self.assertIn("PartOf=graphical-session.target", unit)
        self.assertEqual(self.commands(), [
            ["cargo", "build", "--manifest-path", str(PLUGIN / "microphone/Cargo.toml"),
             "--locked", "--release", "--target-dir", str(application / "cargo-target")],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", SERVICE],
            ["systemctl", "--user", "restart", SERVICE],
        ])
        enabled = service.parent / "graphical-session.target.wants" / SERVICE
        self.assertEqual(enabled.resolve(), service)

    def test_plugin_install_includes_microphone_before_completing_input_setup(self):
        paths = router.Paths(self.config / "fredrik.shield-remote", self.runtime)
        paths.prepare()
        with (
            patch.dict(os.environ, self.environment),
            patch.object(router, "RULE", self.base / "remote.rules"),
            patch.object(router.sys.stdin, "isatty", return_value=False),
        ):
            router.install(paths, router.load_config(paths))
            self.assertTrue(router.setup_complete(paths, "00:11:22:33:44:55"))
        self.assertTrue((self.config / "systemd/user" / SERVICE).is_file())
        self.assertIn(["systemctl", "--user", "restart", SERVICE], self.commands())
        self.assertIn(["systemctl", "--user", "restart", router.SERVICE], self.commands())

    def test_setup_requires_matching_enabled_microphone_and_executable(self):
        paths = router.Paths(self.config / "fredrik.shield-remote", self.runtime)
        paths.prepare()
        with (
            patch.dict(os.environ, self.environment),
            patch.object(router, "RULE", self.base / "remote.rules"),
            patch.object(router.sys.stdin, "isatty", return_value=False),
        ):
            router.install(paths, router.load_config(paths))
            enabled = paths.microphone_service.parent / "graphical-session.target.wants" / SERVICE
            enabled.unlink()
            self.assertFalse(router.setup_complete(paths, "00:11:22:33:44:55"))
            enabled.symlink_to(paths.microphone_service)
            unit = paths.microphone_service.read_text()
            paths.microphone_service.write_text(unit.replace("00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF"))
            self.assertFalse(router.setup_complete(paths, "00:11:22:33:44:55"))
            paths.microphone_service.write_text(unit)
            paths.microphone_binary.chmod(0o600)
            self.assertFalse(router.setup_complete(paths, "00:11:22:33:44:55"))
            paths.microphone_binary.chmod(0o755)
            self.assertTrue(router.setup_complete(paths, "00:11:22:33:44:55"))

    def test_reinstall_rebuilds_and_keeps_only_microphone_service_changes(self):
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sum(command[0] == "cargo" for command in self.commands()), 2)
        self.assertFalse((self.config / "systemd/user" / router.SERVICE).exists())
        self.assertFalse((self.config / "fredrik.shield-remote/input-setup.json").exists())

    def test_failed_build_preserves_installed_binary_and_service(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        binary = self.data / "fredrik.shield-remote/bin/atvvoice"
        binary.write_text("#!/bin/sh\necho previous-working-build\n")
        service = self.config / "systemd/user" / SERVICE
        previous_unit = service.read_text()
        previous_commands = len(self.commands())
        self.environment["FAIL_BUILD"] = "1"
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture build failed", result.stderr)
        self.assertEqual(binary.read_text(), "#!/bin/sh\necho previous-working-build\n")
        self.assertEqual(service.read_text(), previous_unit)
        self.assertEqual(len(self.commands()), previous_commands + 1)
        self.assertEqual(self.commands()[-1][0], "cargo")

    def test_full_setup_build_failure_preserves_existing_input_and_audio_installation(self):
        paths = router.Paths(self.config / "fredrik.shield-remote", self.runtime)
        paths.prepare()
        rule = self.base / "remote.rules"
        with (
            patch.dict(os.environ, self.environment),
            patch.object(router, "RULE", rule),
            patch.object(router.sys.stdin, "isatty", return_value=False),
        ):
            config = router.load_config(paths)
            router.install(paths, config)
            files = [
                rule, paths.config / "input-setup.json", paths.service,
                paths.microphone_service, paths.microphone_binary,
            ]
            previous = {file: file.read_bytes() for file in files}
            previous_commands = len(self.commands())
            os.environ["FAIL_BUILD"] = "1"
            with self.assertRaises(subprocess.CalledProcessError):
                router.install(paths, config)
            self.assertEqual({file: file.read_bytes() for file in files}, previous)
            self.assertTrue(router.setup_complete(paths, config.remote_address))
        self.assertEqual(len(self.commands()), previous_commands + 1)
        self.assertEqual(self.commands()[-1][0], "cargo")

    def test_existing_standalone_shield_service_is_migrated(self):
        service = self.config / "systemd/user" / SERVICE
        service.parent.mkdir(parents=True)
        service.write_text(
            "[Unit]\nDescription=NVIDIA Shield Remote microphone (ATVVoice)\n"
            "Requires=pipewire.service\nAfter=graphical-session.target pipewire.service\n"
            "PartOf=graphical-session.target\n\n[Service]\nType=simple\n"
            "ExecStart=%h/.local/bin/atvvoice --device 00:11:22:33:44:55 "
            '--name shield-remote --description "NVIDIA Shield Remote Microphone" --mic-on-demand\n'
            "Restart=on-failure\nRestartSec=5\nKillSignal=SIGINT\nTimeoutStopSec=10\n"
            "UMask=0077\n\n[Install]\nWantedBy=graphical-session.target\n"
        )
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(service.read_text().startswith("# Managed by fredrik.shield-remote\n"))
        self.assertNotIn("%h/.local/bin/atvvoice", service.read_text())

    def test_unrelated_or_symlinked_services_are_never_overwritten(self):
        service = self.config / "systemd/user" / SERVICE
        service.parent.mkdir(parents=True)
        service.write_text("[Service]\nExecStart=/another/application\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated microphone service", result.stdout + result.stderr)
        self.assertEqual(service.read_text(), "[Service]\nExecStart=/another/application\n")
        service.unlink()
        destination = self.base / "shared.service"
        destination.write_text("shared configuration\n")
        service.symlink_to(destination)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked microphone user service", result.stdout + result.stderr)
        self.assertEqual(destination.read_text(), "shared configuration\n")
        self.assertEqual(self.commands(), [])

    def test_symlinked_application_storage_is_not_used(self):
        self.data.mkdir()
        shared = self.base / "shared"
        shared.mkdir()
        (self.data / "fredrik.shield-remote").symlink_to(shared)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machine-local", result.stdout + result.stderr)
        self.assertEqual(list(shared.iterdir()), [])
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
