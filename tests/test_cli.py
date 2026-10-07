from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from personal_vault import __version__
from personal_vault.cli import build_parser, main
from personal_vault.database import DatabaseSchemaError


class CliTests(unittest.TestCase):
    def test_parser_has_stable_program_name(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.prog, "personal-vault")
        self.assertNotIn("read", parser.description.lower())

    def test_version_comes_from_package(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--version"])

        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue(), f"personal-vault {__version__}\n")

    def test_every_command_level_requires_a_subcommand(self) -> None:
        for argv in (
            [],
            ["manifest"],
            ["import"],
            ["doctor"],
            ["extension-recovery"],
            ["read"],
            ["update"],
            ["export"],
            ["memory"],
            ["migration"],
            ["analyze"],
            ["adapter"],
        ):
            with self.subTest(argv=argv):
                error = io.StringIO()
                with contextlib.redirect_stderr(error), self.assertRaises(
                    SystemExit
                ) as exit:
                    main(argv)

                self.assertEqual(exit.exception.code, 2)
                self.assertIn("required", error.getvalue())

    def test_import_help_describes_safety_sensitive_arguments(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["import", "chatgpt-official", "--help"])

        self.assertEqual(exit.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("must not be inside the evidence root", help_text)
        self.assertIn("Stable account scope", help_text)

    def test_extension_recovery_help_exposes_the_safe_phase_machine(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["extension-recovery", "--help"])

        self.assertEqual(exit.exception.code, 0)
        help_text = output.getvalue()
        for command in (
            "prepare",
            "status",
            "launch",
            "confirm-preflight",
            "install",
            "validate",
            "compare",
        ):
            self.assertIn(command, help_text)

    def test_reader_help_and_serve_are_loopback_only(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["read", "serve", "--help"])
        self.assertEqual(exit.exception.code, 0)
        self.assertIn("127.0.0.1", output.getvalue())

        server = mock.Mock()
        server.server_address = ("127.0.0.1", 4311)
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            (vault / "canonical").mkdir()
            with mock.patch(
                "personal_vault.cli.create_reader_server", return_value=server
            ) as create, contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["read", "serve", str(vault), "--port", "0"]), 0
                )
            expected_vault = vault.resolve()
        create.assert_called_once_with(
            expected_vault / "canonical" / "archive.sqlite",
            expected_vault,
            port=0,
        )
        server.server_close.assert_called_once_with()
        self.assertIn('"read_only": true', output.getvalue())

    def test_memory_review_serve_reports_auto_save(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["memory", "serve", "--help"])
        self.assertEqual(exit.exception.code, 0)
        self.assertIn("127.0.0.1", output.getvalue())

        server = mock.Mock()
        server.server_address = ("127.0.0.1", 8766)
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            with mock.patch(
                "personal_vault.cli.create_memory_review_server", return_value=server
            ) as create, contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["memory", "serve", str(vault), "--port", "0"]), 0
                )
            expected_vault = vault.resolve()
        create.assert_called_once_with(expected_vault, port=0)
        server.server_close.assert_called_once_with()
        self.assertIn('"auto_save": true', output.getvalue())

    def test_extension_recovery_errors_exit_cleanly(self) -> None:
        error = io.StringIO()
        with mock.patch(
            "personal_vault.cli.load_replay_workspace",
            side_effect=Exception("wrong exception type"),
        ), self.assertRaises(Exception):
            main(["extension-recovery", "status", "replay"])

        from personal_vault.extension_recovery import ExtensionRecoveryError

        with mock.patch(
            "personal_vault.cli.load_replay_workspace",
            side_effect=ExtensionRecoveryError("synthetic recovery boundary"),
        ), contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit:
            main(["extension-recovery", "status", "replay"])

        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(error.getvalue(), "error: synthetic recovery boundary\n")

    def test_expected_path_error_exits_cleanly(self) -> None:
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit:
            main(
                [
                    "doctor",
                    "chatgpt-official",
                    "/definitely/missing/evidence",
                    "/definitely/missing/manifest.json",
                ]
            )

        self.assertEqual(exit.exception.code, 2)
        self.assertTrue(error.getvalue().startswith("error: "))
        self.assertNotIn("Traceback", error.getvalue())

    def test_database_schema_error_exits_cleanly(self) -> None:
        error = io.StringIO()
        with mock.patch(
            "personal_vault.cli.import_chatgpt_export",
            side_effect=DatabaseSchemaError("synthetic schema mismatch"),
        ), contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit:
            main(
                [
                    "import",
                    "chatgpt-official",
                    "evidence",
                    "manifest.json",
                    "vault",
                    "--identity-scope",
                    "fixture",
                ]
            )

        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(error.getvalue(), "error: synthetic schema mismatch\n")

    def test_sqlite_error_exits_cleanly(self) -> None:
        error = io.StringIO()
        with mock.patch(
            "personal_vault.cli.import_chatgpt_export",
            side_effect=sqlite3.OperationalError("synthetic database failure"),
        ), contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit:
            main(
                [
                    "import",
                    "chatgpt-official",
                    "evidence",
                    "manifest.json",
                    "vault",
                    "--identity-scope",
                    "fixture",
                ]
            )

        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(error.getvalue(), "error: synthetic database failure\n")

    def test_interrupts_are_not_converted_to_cli_errors(self) -> None:
        with mock.patch(
            "personal_vault.cli.doctor_chatgpt_export", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            main(
                [
                    "doctor",
                    "chatgpt-official",
                    "evidence",
                    "manifest.json",
                ]
            )

        with mock.patch(
            "personal_vault.cli.doctor_chatgpt_export", side_effect=SystemExit(7)
        ), self.assertRaises(SystemExit) as exit:
            main(
                [
                    "doctor",
                    "chatgpt-official",
                    "evidence",
                    "manifest.json",
                ]
            )

        self.assertEqual(exit.exception.code, 7)

    def test_manifest_create_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            evidence = temporary_path / "evidence"
            evidence.mkdir()
            (evidence / "record.json").write_text("{}\n", encoding="utf-8")
            manifest = temporary_path / "manifest.json"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                create_status = main(
                    [
                        "manifest",
                        "create",
                        str(evidence),
                        str(manifest),
                        "--source-id",
                        "test",
                        "--source-kind",
                        "fixture",
                        "--captured-at",
                        "2026-08-10T00:00:00+08:00",
                    ]
                )
            self.assertEqual(create_status, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "created")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                verify_status = main(
                    ["manifest", "verify", str(evidence), str(manifest)]
                )
            self.assertEqual(verify_status, 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
