from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from personal_vault import __version__
from personal_vault.analysis_report import (
    AnalysisReportError,
    build_snapshot_analysis_report,
)
from personal_vault.chatgpt_export import (
    ChatGPTImportError,
    doctor_chatgpt_export,
    import_chatgpt_export,
)
from personal_vault.database import DatabaseSchemaError
from personal_vault.evidence import (
    EvidenceError,
    build_manifest,
    load_manifest,
    verify_manifest,
    write_manifest,
)
from personal_vault.export_archive import (
    ArchiveExportError,
    export_conversation_html,
    export_conversation_pdf,
    export_snapshot_html_archive,
    verify_snapshot_html_archive,
)
from personal_vault.extension_recovery import (
    ExtensionRecoveryError,
    compare_replay_exports,
    confirm_missing_database_preflight,
    discover_export_run,
    install_recovery_indexeddb,
    load_replay_workspace,
    prepare_replay_workspaces,
    run_isolated_chrome,
    validate_export_run,
)
from personal_vault.reader import ReaderError, create_reader_server
from personal_vault.memory_store import (
    MemoryStoreError,
    decide_memory_candidate,
    export_confirmed_profile,
    export_memory_review_html,
    scan_memory_candidates,
)
from personal_vault.memory_review import MemoryReviewError, create_memory_review_server
from personal_vault.migration import (
    MigrationError,
    build_chatgpt_migration_pack,
)
from personal_vault.interchange import InterchangeError, validate_interchange_jsonl
from personal_vault.workflow import WorkflowError, update_chatgpt_official


def _replay_payload(workspace: object) -> dict[str, object]:
    return {
        "replay_id": workspace.replay_id,
        "phase": workspace.phase,
        "root": str(workspace.root),
        "output_directory": str(workspace.output_directory),
        "target_extension_id": workspace.target_extension_id,
        "working_copy_evidence_tree_sha256": (
            workspace.working_copy_evidence_tree_sha256
        ),
        "source_payload_sha256": workspace.source_payload_sha256,
        "production_bundle_sha256": workspace.production_bundle_sha256,
        "preparation_sha256": workspace.preparation_sha256,
        "recovery_state_sha256": workspace.recovery_state_sha256,
        "browser": {
            "flavor": workspace.browser_flavor,
            "version": workspace.browser_version,
            "binary_sha256": workspace.browser_binary_sha256,
        },
    }


def _validated_export_payload(export: object) -> dict[str, object]:
    return {
        "status": "validated",
        "replay_id": export.replay_id,
        "snapshot_id": export.snapshot_id,
        "manifest_sha256": export.manifest_sha256,
        "file_count": export.file_count,
        "byte_count": export.byte_count,
        "row_count": export.row_count,
        "row_sha256": export.row_sha256,
        "store_sha256": export.store_sha256,
        "binary_sha256": export.binary_sha256,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personal-vault",
        description=(
            "Create manifests, validate snapshots, and import a "
            "source-preserving conversation vault."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the program version and exit.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    manifest_parser = commands.add_parser(
        "manifest", help="Create or verify a deterministic evidence manifest."
    )
    manifest_commands = manifest_parser.add_subparsers(
        dest="manifest_command", required=True
    )

    create = manifest_commands.add_parser(
        "create", help="Hash an immutable source tree."
    )
    create.add_argument(
        "root", type=Path, help="Source directory to hash without following symlinks."
    )
    create.add_argument(
        "output", type=Path, help="Manifest JSON path outside the source directory."
    )
    create.add_argument(
        "--source-id", required=True, help="Stable identifier for this source snapshot."
    )
    create.add_argument(
        "--source-kind", required=True, help="Provider-specific source kind."
    )
    create.add_argument(
        "--captured-at",
        required=True,
        help="Capture date or timezone-aware ISO 8601 timestamp.",
    )

    verify = manifest_commands.add_parser(
        "verify", help="Verify a tree against a manifest."
    )
    verify.add_argument("root", type=Path, help="Source directory to verify.")
    verify.add_argument("manifest", type=Path, help="Evidence Manifest JSON path.")

    import_parser = commands.add_parser(
        "import", help="Import a verified Source Snapshot into the vault."
    )
    import_commands = import_parser.add_subparsers(
        dest="import_command", required=True
    )
    chatgpt_import = import_commands.add_parser(
        "chatgpt-official", help="Import an official ChatGPT account export."
    )
    chatgpt_import.add_argument(
        "root", type=Path, help="Verified ChatGPT official-export directory."
    )
    chatgpt_import.add_argument(
        "manifest", type=Path, help="Evidence Manifest JSON path for the export."
    )
    chatgpt_import.add_argument(
        "output", type=Path, help="Vault root; must not be inside the evidence root."
    )
    chatgpt_import.add_argument(
        "--identity-scope",
        required=True,
        help="Stable account scope shared by snapshots from the same account.",
    )

    doctor_parser = commands.add_parser(
        "doctor", help="Validate a Source Snapshot without writing vault artifacts."
    )
    doctor_commands = doctor_parser.add_subparsers(
        dest="doctor_command", required=True
    )
    chatgpt_doctor = doctor_commands.add_parser(
        "chatgpt-official", help="Validate an official ChatGPT account export."
    )
    chatgpt_doctor.add_argument(
        "root", type=Path, help="Verified ChatGPT official-export directory."
    )
    chatgpt_doctor.add_argument(
        "manifest", type=Path, help="Evidence Manifest JSON path for the export."
    )

    recovery_parser = commands.add_parser(
        "extension-recovery",
        help="Prepare and validate isolated dual-replay IndexedDB recovery.",
    )
    recovery_commands = recovery_parser.add_subparsers(
        dest="recovery_command", required=True
    )

    recovery_prepare = recovery_commands.add_parser(
        "prepare", help="Create two bound replay workspaces without launching Chrome."
    )
    recovery_prepare.add_argument(
        "working_copy", type=Path, help="Writable IndexedDB-only Recovery Working Copy."
    )
    recovery_prepare.add_argument(
        "evidence_manifest", type=Path, help="Verified v2 manifest for the working copy."
    )
    recovery_prepare.add_argument(
        "destination", type=Path, help="New destination for both replay workspaces."
    )
    recovery_prepare.add_argument(
        "extension_manifest",
        type=Path,
        help="Audited archive-only extension manifest.json.",
    )
    recovery_prepare.add_argument(
        "--chrome-binary",
        type=Path,
        required=True,
        help="Chrome for Testing or compatible unbranded Chromium binary.",
    )
    recovery_prepare.add_argument(
        "--master-root",
        type=Path,
        action="append",
        required=True,
        help="Protected immutable master root; repeat for every master.",
    )
    recovery_prepare.add_argument(
        "--active-profile-root",
        type=Path,
        action="append",
        required=True,
        help="Protected daily Chrome profile root; repeat when needed.",
    )

    recovery_status = recovery_commands.add_parser(
        "status", help="Validate and report a prepared replay's current phase."
    )
    recovery_status.add_argument("replay", type=Path, help="Prepared replay workspace.")

    recovery_launch = recovery_commands.add_parser(
        "launch", help="Run the bound offline Chrome and wait for its complete exit."
    )
    recovery_launch.add_argument("replay", type=Path, help="Prepared replay workspace.")
    recovery_launch.add_argument(
        "--bwrap-binary",
        default="bwrap",
        help="Compatible bubblewrap executable (default: bwrap).",
    )

    recovery_confirm = recovery_commands.add_parser(
        "confirm-preflight",
        help="Record the missing-database result after the preflight Chrome exits.",
    )
    recovery_confirm.add_argument("replay", type=Path, help="Prepared replay workspace.")
    recovery_confirm.add_argument(
        "--runtime-extension-id",
        required=True,
        help="Extension ID displayed by the preflight page.",
    )
    recovery_confirm.add_argument(
        "--observed-database-name",
        action="append",
        default=[],
        help="Database name displayed by preflight; repeat once per name.",
    )

    recovery_install = recovery_commands.add_parser(
        "install", help="Origin-rename and install the staged IndexedDB after preflight."
    )
    recovery_install.add_argument("replay", type=Path, help="Prepared replay workspace.")

    recovery_validate = recovery_commands.add_parser(
        "validate", help="Host-validate one closed browser export without printing rows."
    )
    recovery_validate.add_argument("replay", type=Path, help="Prepared replay workspace.")
    recovery_validate.add_argument(
        "--run",
        type=Path,
        help="Export run directory; default requires exactly one run in the bound output.",
    )

    recovery_compare = recovery_commands.add_parser(
        "compare", help="Validate and deterministically compare both replay exports."
    )
    recovery_compare.add_argument("first_replay", type=Path)
    recovery_compare.add_argument("second_replay", type=Path)
    recovery_compare.add_argument("--first-run", type=Path)
    recovery_compare.add_argument("--second-run", type=Path)

    read_parser = commands.add_parser(
        "read", help="Serve the canonical archive through a local read-only UI."
    )
    read_commands = read_parser.add_subparsers(dest="read_command", required=True)
    read_serve = read_commands.add_parser(
        "serve",
        help="Start the offline archive reader on 127.0.0.1.",
        description="Start the offline archive reader on 127.0.0.1 only.",
    )
    read_serve.add_argument(
        "vault_root",
        type=Path,
        help="Vault root containing canonical/archive.sqlite and assets/sha256/.",
    )
    read_serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Loopback TCP port (default: 8765; use 0 to choose a free port).",
    )

    update_parser = commands.add_parser(
        "update", help="Run a recurring source capture, import, and validation workflow."
    )
    update_commands = update_parser.add_subparsers(
        dest="update_command", required=True
    )
    update_chatgpt = update_commands.add_parser(
        "chatgpt-official",
        help="Manifest, validate, import, and verify one official ChatGPT export.",
    )
    update_chatgpt.add_argument("root", type=Path, help="New extracted export directory.")
    update_chatgpt.add_argument("vault_root", type=Path, help="Persistent vault root.")
    update_chatgpt.add_argument(
        "--identity-scope",
        required=True,
        help="Stable account scope shared by every snapshot from this account.",
    )
    update_chatgpt.add_argument(
        "--source-id",
        required=True,
        help="Stable safe snapshot ID, for example chatgpt-official-2026-08-11.",
    )
    update_chatgpt.add_argument(
        "--captured-at",
        required=True,
        help="Export capture date or timezone-aware ISO 8601 timestamp.",
    )
    update_chatgpt.add_argument(
        "--manifest",
        type=Path,
        help="Optional manifest path; defaults inside VAULT_ROOT/manifests/sources/.",
    )

    export_parser = commands.add_parser(
        "export", help="Create bounded human-readable derivatives from the archive."
    )
    export_commands = export_parser.add_subparsers(
        dest="export_command", required=True
    )
    export_html = export_commands.add_parser(
        "conversation-html",
        help="Create one portable conversation directory with local attachments.",
    )
    export_html.add_argument("vault_root", type=Path)
    export_html.add_argument("snapshot_id", type=int)
    export_html.add_argument("conversation_identity_key")
    export_html.add_argument("output_directory", type=Path)

    export_pdf = export_commands.add_parser(
        "conversation-pdf", help="Print an existing static conversation HTML to PDF."
    )
    export_pdf.add_argument("html_path", type=Path)
    export_pdf.add_argument("pdf_path", type=Path)
    export_pdf.add_argument(
        "--chrome-binary",
        type=Path,
        required=True,
        help="Chrome for Testing or compatible unbranded Chromium binary.",
    )
    export_snapshot = export_commands.add_parser(
        "snapshot-html",
        help="Export an indexed portable HTML archive for every conversation in a snapshot.",
    )
    export_snapshot.add_argument("vault_root", type=Path)
    export_snapshot.add_argument("snapshot_id", type=int)
    export_snapshot.add_argument("output_directory", type=Path)
    verify_snapshot = export_commands.add_parser(
        "verify-snapshot-html",
        help="Re-hash a portable snapshot archive without printing private content.",
    )
    verify_snapshot.add_argument("archive_root", type=Path)

    memory_parser = commands.add_parser(
        "memory", help="Create and review evidence-linked personal memory candidates."
    )
    memory_commands = memory_parser.add_subparsers(
        dest="memory_command", required=True
    )
    memory_scan = memory_commands.add_parser(
        "scan", help="Create pending candidates from explicit current-branch user statements."
    )
    memory_scan.add_argument("vault_root", type=Path)
    memory_scan.add_argument("--snapshot-id", type=int, required=True)
    memory_decide = memory_commands.add_parser(
        "decide", help="Confirm, reject, or return one candidate to pending."
    )
    memory_decide.add_argument("vault_root", type=Path)
    memory_decide.add_argument("candidate_id")
    memory_decide.add_argument(
        "--status", choices=("confirmed", "rejected", "pending"), required=True
    )
    memory_decide.add_argument("--note")
    memory_profile = memory_commands.add_parser(
        "profile", help="Render only confirmed candidates as a source-linked Markdown profile."
    )
    memory_profile.add_argument("vault_root", type=Path)
    memory_profile.add_argument("--output", type=Path)
    memory_review = memory_commands.add_parser(
        "review", help="Render pending candidates as a portable, script-free HTML snapshot."
    )
    memory_review.add_argument("vault_root", type=Path)
    memory_review.add_argument("--output", type=Path)
    memory_serve = memory_commands.add_parser(
        "serve",
        help="Run the interactive review workbench on 127.0.0.1.",
        description="Run the auto-saving interactive review workbench on 127.0.0.1.",
    )
    memory_serve.add_argument("vault_root", type=Path)
    memory_serve.add_argument(
        "--port", type=int, default=8766, help="Loopback TCP port; use 0 for an available port."
    )

    migration_parser = commands.add_parser(
        "migration", help="Build provider-specific continuity packages from confirmed memories."
    )
    migration_commands = migration_parser.add_subparsers(
        dest="migration_command", required=True
    )
    migration_chatgpt = migration_commands.add_parser(
        "chatgpt", help="Build a ChatGPT project/upload package with source references."
    )
    migration_chatgpt.add_argument("vault_root", type=Path)
    migration_chatgpt.add_argument("output_directory", type=Path)

    analyze_parser = commands.add_parser(
        "analyze", help="Write deterministic non-LLM archive analysis reports."
    )
    analyze_commands = analyze_parser.add_subparsers(
        dest="analyze_command", required=True
    )
    analyze_snapshot = analyze_commands.add_parser(
        "snapshot", help="Export JSON and Markdown metrics for one snapshot."
    )
    analyze_snapshot.add_argument("vault_root", type=Path)
    analyze_snapshot.add_argument("snapshot_id", type=int)
    analyze_snapshot.add_argument("output_directory", type=Path)

    adapter_parser = commands.add_parser(
        "adapter", help="Validate source-neutral output from future provider adapters."
    )
    adapter_commands = adapter_parser.add_subparsers(
        dest="adapter_command", required=True
    )
    adapter_validate = adapter_commands.add_parser(
        "validate-jsonl", help="Validate a pmv.interchange-event.v1 JSONL stream."
    )
    adapter_validate.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest" and args.manifest_command == "create":
            manifest = build_manifest(
                args.root,
                source_id=args.source_id,
                source_kind=args.source_kind,
                captured_at=args.captured_at,
            )
            write_manifest(manifest, args.output, evidence_root=args.root)
            print(
                json.dumps(
                    {
                        "status": "created",
                        "output": str(args.output.resolve()),
                        "tree_sha256": manifest["tree_sha256"],
                        "totals": manifest["totals"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "manifest" and args.manifest_command == "verify":
            result = verify_manifest(args.root, load_manifest(args.manifest))
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 0 if result.ok else 1
        if args.command == "import" and args.import_command == "chatgpt-official":
            result = import_chatgpt_export(
                evidence_root=args.root,
                manifest_path=args.manifest,
                output_root=args.output,
                identity_scope=args.identity_scope,
            )
            payload = asdict(result)
            payload["database_path"] = str(result.database_path)
            payload["ndjson_path"] = str(result.ndjson_path)
            payload["source_records_ndjson_path"] = str(
                result.source_records_ndjson_path
            )
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        if args.command == "doctor" and args.doctor_command == "chatgpt-official":
            result = doctor_chatgpt_export(
                evidence_root=args.root, manifest_path=args.manifest
            )
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "prepare"
        ):
            preparation = prepare_replay_workspaces(
                args.working_copy,
                args.destination,
                args.extension_manifest,
                chrome_binary=args.chrome_binary,
                master_roots=args.master_root,
                active_profile_roots=args.active_profile_root,
                evidence_manifest=args.evidence_manifest,
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "root": str(preparation.root),
                        "source_extension_id": preparation.source_extension_id,
                        "target_extension_id": preparation.target_extension_id,
                        "source_tree_sha256": preparation.source_tree_sha256,
                        "source_payload_sha256": preparation.source_payload_sha256,
                        "production_bundle_sha256": (
                            preparation.production_bundle_sha256
                        ),
                        "browser_binary_sha256": preparation.browser_binary_sha256,
                        "replays": [
                            _replay_payload(replay) for replay in preparation.replays
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "status"
        ):
            print(
                json.dumps(
                    {"status": "ok", **_replay_payload(load_replay_workspace(args.replay))},
                    ensure_ascii=False,
                )
            )
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "launch"
        ):
            result = run_isolated_chrome(
                args.replay, bwrap_binary=args.bwrap_binary
            )
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "confirm-preflight"
        ):
            workspace = confirm_missing_database_preflight(
                args.replay,
                runtime_extension_id=args.runtime_extension_id,
                observed_database_names=args.observed_database_name,
                chrome_exited=True,
            )
            print(
                json.dumps(
                    {"status": "confirmed", **_replay_payload(workspace)},
                    ensure_ascii=False,
                )
            )
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "install"
        ):
            workspace = install_recovery_indexeddb(
                args.replay, chrome_exited=True
            )
            print(
                json.dumps(
                    {"status": "installed", **_replay_payload(workspace)},
                    ensure_ascii=False,
                )
            )
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "validate"
        ):
            run = args.run or discover_export_run(args.replay)
            export = validate_export_run(
                run, replay_root=args.replay, chrome_exited=True
            )
            print(json.dumps(_validated_export_payload(export), ensure_ascii=False))
            return 0
        if (
            args.command == "extension-recovery"
            and args.recovery_command == "compare"
        ):
            first_run = args.first_run or discover_export_run(args.first_replay)
            second_run = args.second_run or discover_export_run(args.second_replay)
            first = validate_export_run(
                first_run, replay_root=args.first_replay, chrome_exited=True
            )
            second = validate_export_run(
                second_run, replay_root=args.second_replay, chrome_exited=True
            )
            comparison = compare_replay_exports(first, second)
            print(
                json.dumps(
                    {
                        "status": (
                            "extraction_complete"
                            if comparison.extraction_complete
                            else "mismatch"
                        ),
                        "comparison": asdict(comparison),
                        "first": _validated_export_payload(first),
                        "second": _validated_export_payload(second),
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if comparison.extraction_complete else 1
        if args.command == "read" and args.read_command == "serve":
            vault_root = args.vault_root.resolve(strict=True)
            database_path = vault_root / "canonical" / "archive.sqlite"
            server = create_reader_server(database_path, vault_root, port=args.port)
            try:
                host, port = server.server_address[:2]
                print(
                    json.dumps(
                        {
                            "status": "serving",
                            "url": f"http://{host}:{port}/",
                            "database_path": str(database_path),
                            "read_only": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                server.serve_forever()
            except KeyboardInterrupt:
                return 0
            finally:
                server.server_close()
            return 0
        if args.command == "update" and args.update_command == "chatgpt-official":
            result = update_chatgpt_official(
                evidence_root=args.root,
                vault_root=args.vault_root,
                identity_scope=args.identity_scope,
                source_id=args.source_id,
                captured_at=args.captured_at,
                manifest_path=args.manifest,
            )
            print(
                json.dumps(
                    {
                        "status": result.status,
                        "source_id": result.source_id,
                        "manifest_path": str(result.manifest_path),
                        "manifest_status": result.manifest_status,
                        "evidence_tree_sha256": result.evidence_tree_sha256,
                        "import_status": result.import_result.status,
                        "counts": {
                            "source_records": result.import_result.source_records,
                            "conversations": result.import_result.conversations,
                            "nodes": result.import_result.nodes,
                            "messages": result.import_result.messages,
                            "group_threads": result.import_result.group_threads,
                            "group_messages": result.import_result.group_messages,
                            "warnings": result.import_result.warnings,
                        },
                        "archive_validation_ok": result.validation.ok,
                        "report_path": str(result.report_path),
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if result.validation.ok else 1
        if args.command == "export" and args.export_command == "conversation-html":
            result = export_conversation_html(
                vault_root=args.vault_root,
                snapshot_id=args.snapshot_id,
                conversation_identity_key=args.conversation_identity_key,
                output_directory=args.output_directory,
            )
            print(
                json.dumps(
                    {
                        "status": "exported",
                        "format": "html",
                        "root": str(result.root),
                        "html_path": str(result.html_path),
                        "manifest_path": str(result.manifest_path),
                        "snapshot_id": result.snapshot_id,
                        "snapshot_key": result.snapshot_key,
                        "conversation_identity_key": result.conversation_identity_key,
                        "index_sha256": result.index_sha256,
                        "asset_count": result.asset_count,
                        "asset_bytes": result.asset_bytes,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "export" and args.export_command == "conversation-pdf":
            result = export_conversation_pdf(
                html_path=args.html_path,
                pdf_path=args.pdf_path,
                chrome_binary=args.chrome_binary,
            )
            print(
                json.dumps(
                    {
                        "status": "exported",
                        "format": "pdf",
                        "pdf_path": str(result.pdf_path),
                        "html_path": str(result.html_path),
                        "pdf_sha256": result.pdf_sha256,
                        "pdf_bytes": result.pdf_bytes,
                        "browser_version": result.browser_version,
                        "browser_binary_sha256": result.browser_binary_sha256,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "export" and args.export_command == "snapshot-html":
            result = export_snapshot_html_archive(
                vault_root=args.vault_root,
                snapshot_id=args.snapshot_id,
                output_directory=args.output_directory,
            )
            print(
                json.dumps(
                    {
                        **asdict(result),
                        "root": str(result.root),
                        "index_path": str(result.index_path),
                        "manifest_path": str(result.manifest_path),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "export" and args.export_command == "verify-snapshot-html":
            result = verify_snapshot_html_archive(args.archive_root)
            print(
                json.dumps(
                    {
                        **asdict(result),
                        "root": str(result.root),
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if result.ok else 1
        if args.command == "memory" and args.memory_command == "scan":
            result = scan_memory_candidates(
                vault_root=args.vault_root, snapshot_id=args.snapshot_id
            )
            print(json.dumps({**asdict(result), "memory_database_path": str(result.memory_database_path)}, ensure_ascii=False))
            return 0
        if args.command == "memory" and args.memory_command == "decide":
            result = decide_memory_candidate(
                vault_root=args.vault_root,
                candidate_id=args.candidate_id,
                status=args.status,
                note=args.note,
            )
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 0
        if args.command == "memory" and args.memory_command == "profile":
            result = export_confirmed_profile(
                vault_root=args.vault_root, output_path=args.output
            )
            print(json.dumps({**asdict(result), "path": str(result.path)}, ensure_ascii=False))
            return 0
        if args.command == "memory" and args.memory_command == "review":
            result = export_memory_review_html(
                vault_root=args.vault_root, output_path=args.output
            )
            print(json.dumps({**asdict(result), "path": str(result.path)}, ensure_ascii=False))
            return 0
        if args.command == "memory" and args.memory_command == "serve":
            vault_root = args.vault_root.resolve(strict=True)
            server = create_memory_review_server(vault_root, port=args.port)
            try:
                host, port = server.server_address[:2]
                print(
                    json.dumps(
                        {
                            "status": "serving",
                            "url": f"http://{host}:{port}/",
                            "memory_database_path": str(vault_root / "memory" / "memory.sqlite"),
                            "auto_save": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                server.serve_forever()
            except KeyboardInterrupt:
                return 0
            finally:
                server.server_close()
            return 0
        if args.command == "migration" and args.migration_command == "chatgpt":
            result = build_chatgpt_migration_pack(
                vault_root=args.vault_root, output_directory=args.output_directory
            )
            print(
                json.dumps(
                    {
                        **asdict(result),
                        "root": str(result.root),
                        "manifest_path": str(result.manifest_path),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "analyze" and args.analyze_command == "snapshot":
            result = build_snapshot_analysis_report(
                vault_root=args.vault_root,
                snapshot_id=args.snapshot_id,
                output_directory=args.output_directory,
            )
            print(
                json.dumps(
                    {
                        **asdict(result),
                        "root": str(result.root),
                        "json_path": str(result.json_path),
                        "markdown_path": str(result.markdown_path),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "adapter" and args.adapter_command == "validate-jsonl":
            result = validate_interchange_jsonl(args.path)
            print(
                json.dumps(
                    {**asdict(result), "path": str(result.path)},
                    ensure_ascii=False,
                )
            )
            return 0
    except (
        EvidenceError,
        ChatGPTImportError,
        DatabaseSchemaError,
        ExtensionRecoveryError,
        ReaderError,
        WorkflowError,
        ArchiveExportError,
        MemoryStoreError,
        MemoryReviewError,
        MigrationError,
        AnalysisReportError,
        InterchangeError,
        OSError,
        sqlite3.Error,
    ) as error:
        parser.exit(2, f"error: {error}\n")

    raise AssertionError(f"unhandled command: {args.command!r}")
