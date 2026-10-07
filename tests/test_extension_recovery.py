from __future__ import annotations

import base64
import json
import hashlib
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from personal_vault.extension_recovery import (
    ARCHIVE_EXTENSION_ID,
    DATABASE_NAME,
    DATABASE_VERSION,
    EXPECTED_DATABASE_SCHEMA,
    EXPECTED_STORE_NAMES,
    EXPORT_MANIFEST_SCHEMA,
    PRODUCTION_EXTENSION_FILES,
    ROW_ENVELOPE_SCHEMA,
    SOURCE_KIND,
    ExtensionRecoveryError,
    build_production_extension_bundle,
    build_isolated_chrome_argv,
    compare_replay_exports,
    confirm_missing_database_preflight,
    derive_extension_id,
    discover_export_run,
    install_recovery_indexeddb,
    inspect_compatible_chrome_binary,
    load_replay_workspace,
    prepare_replay_workspaces,
    run_isolated_chrome,
    validate_export_run,
    validate_production_extension_bundle,
    validate_recovery_working_copy,
)
from personal_vault.evidence import build_manifest, write_manifest


FIXED_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA3Mkr2uKCpk7x1r+8OXwy52Q9TQowsNVfXyIp3ASNMjtotjD4xYPTBlvZ6fP9rVfeh+wHiXJrDWPKbNVzB77ofadVC6w5nO9OPXu7qJ1ZgY7j8pJ1Yca2eCLTJsuTamNlxCn3sTTALkORx5TKwAPIjo31Tr8GaO8vXF7bYAljzdikjAmeN6Hn8aUfLqsGzWONsx7GztiTRMJA+k0AEnY2xJWw37nz5BERHyMNybKsuaeUW0055V9mPRafJ9Bn8iO1R6wynM0nSWhBCJUk2FxsRv6KKF+in1q1cSo4XF/ljpheaDT6iyeMIsrubWC2HHP0o01rq4/wLD0/vctqSGhhowIDAQAB"
)
TARGET_EXTENSION_ID = "ainoobmdpanhopangobnggdkpljnpmgl"
SOURCE_EXTENSION_ID = "a" * 32


class ExtensionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.extension = self.root / "extension"
        self.extension.mkdir()
        (self.extension / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "fixture",
                    "version": "0.0.0",
                    "description": "fixture",
                    "key": FIXED_PUBLIC_KEY,
                    "content_security_policy": {
                        "extension_pages": (
                            "default-src 'none'; script-src 'self'; style-src 'self'; "
                            "connect-src 'none'; object-src 'none'; base-uri 'none'; "
                            "form-action 'none'; frame-ancestors 'none'"
                        )
                    },
                }
            ),
            encoding="utf-8",
        )
        for name in ("export.html", "export.css", "export.js", "tagged-json.js"):
            (self.extension / name).write_text(f"fixture:{name}\n", encoding="utf-8")
        self.chrome_for_testing = self.root / "chrome-for-testing"
        self.chrome_for_testing.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'Google Chrome for Testing 151.0.7922.34'\n",
            encoding="utf-8",
        )
        self.chrome_for_testing.chmod(0o700)
        self.bubblewrap = self.root / "bubblewrap"
        self.bubblewrap.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'bubblewrap 0.11.0'\n",
            encoding="utf-8",
        )
        self.bubblewrap.chmod(0o700)
        self.working_copy = self.root / "recovery-working-copy"
        self._make_working_copy(self.working_copy)
        self._installed_validation_replays = None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_working_copy(self, root: Path) -> None:
        indexeddb = root / "IndexedDB"
        leveldb = indexeddb / (
            f"chrome-extension_{SOURCE_EXTENSION_ID}_0.indexeddb.leveldb"
        )
        blob = indexeddb / f"chrome-extension_{SOURCE_EXTENSION_ID}_0.indexeddb.blob"
        leveldb.mkdir(parents=True)
        (leveldb / "CURRENT").write_bytes(b"MANIFEST-000001\n")
        (leveldb / "MANIFEST-000001").write_bytes(b"leveldb-fixture")
        (blob / "1" / "00").mkdir(parents=True)
        (blob / "1" / "00" / "2").write_bytes(b"blob-fixture")

    def _prepare(self, name: str = "prepared"):
        return prepare_replay_workspaces(
            self.working_copy,
            self.root / name,
            self.extension / "manifest.json",
            chrome_binary=self.chrome_for_testing,
            master_roots=(),
            active_profile_roots=(),
            unsafe_synthetic_only=True,
        )

    def _default_validation_replays(self):
        if self._installed_validation_replays is None:
            self._installed_validation_replays = self._install_both_replays(
                "prepared-validation-default"
            )
        return self._installed_validation_replays

    def _validate(self, run: Path, replay=None):
        if replay is None:
            candidates = [
                item
                for item in self._default_validation_replays()
                if run.parent.resolve() == item.output_directory.resolve()
            ]
            self.assertEqual(len(candidates), 1)
            replay = candidates[0]
        return validate_export_run(
            run,
            replay_root=replay.root,
            chrome_exited=True,
        )

    def _install_both_replays(self, name: str = "prepared-exports"):
        prepared = self._prepare(name)
        installed = []
        for replay in prepared.replays:
            confirm_missing_database_preflight(
                replay.root,
                runtime_extension_id=TARGET_EXTENSION_ID,
                observed_database_names=(),
                chrome_exited=True,
            )
            installed.append(
                install_recovery_indexeddb(replay.root, chrome_exited=True)
            )
        return tuple(installed)

    @staticmethod
    def _provenance_for(replay):
        return {
            "replay_id": replay.replay_id,
            "challenge": replay.replay_challenge,
            "working_copy_evidence_tree_sha256": replay.working_copy_evidence_tree_sha256,
            "source_payload_sha256": replay.source_payload_sha256,
            "production_bundle_sha256": replay.production_bundle_sha256,
            "preparation_sha256": replay.preparation_sha256,
            "recovery_state_sha256": replay.recovery_state_sha256,
            "browser_flavor": replay.browser_flavor,
            "browser_version": replay.browser_version,
            "browser_binary_sha256": replay.browser_binary_sha256,
        }

    def _write_export(
        self,
        name: str,
        *,
        marker: str = "same",
        include_binary: bool = True,
        replay=None,
    ) -> Path:
        if replay is None:
            replay = self._default_validation_replays()[0]
        run = replay.output_directory / name
        stores_directory = run / "stores"
        stores_directory.mkdir(parents=True)
        store_manifests = []
        observed_schema = []
        for index, store_name in enumerate(EXPECTED_STORE_NAMES):
            schema = EXPECTED_DATABASE_SCHEMA[store_name]
            stem = f"{index:03d}-{store_name}"
            binary_parts = []
            if index == 0 and include_binary:
                relative = f"binary/{stem}/000000000000-value-000000.bin"
                payload = b"binary-fixture"
                binary_path = run / relative
                binary_path.parent.mkdir(parents=True)
                binary_path.write_bytes(payload)
                reference = {
                    "nodeId": 0,
                    "kind": "Blob",
                    "byteLength": len(payload),
                    "path": relative,
                }
                binary_parts.append(reference)
            value_nodes = []
            value_root: object = marker
            if binary_parts:
                part = binary_parts[0]
                value_root = {"$ref": 0}
                value_nodes.append(
                    {
                        "$type": "Blob",
                        "size": part["byteLength"],
                        "mediaType": "",
                        "data": {
                            "external": {
                                "path": part["path"],
                                "byteLength": part["byteLength"],
                            }
                        },
                        "properties": [],
                    }
                )
            value_graph = {
                "codec": "tagged-structured-clone-v1",
                "data": {"root": value_root, "nodes": value_nodes},
            }
            envelope = {
                "envelope_schema": ROW_ENVELOPE_SCHEMA,
                "snapshot_id": "extension-snapshot-fixture",
                "source_kind": SOURCE_KIND,
                "database_name": DATABASE_NAME,
                "database_version": DATABASE_VERSION,
                "store_name": store_name,
                "source_primary_key": {
                    "codec": "tagged-structured-clone-v1",
                    "data": {"root": index, "nodes": []},
                },
                "cursor_ordinal": 0,
                "source_record_schema_version": 1,
                "capture_schema_version": 2,
                "value": value_graph,
                "decoded_value_sha256": None,
                "decoded_value_hash_status": "deferred_to_host",
                "decode_status": "ok",
                "decoder_warnings": [],
                "binary_parts": binary_parts,
            }
            line = (
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            output_relative = f"stores/{stem}.ndjson"
            (run / output_relative).write_bytes(line)
            binary_bytes = sum(part["byteLength"] for part in binary_parts)
            binary_attempts = [
                {
                    "path": part["path"],
                    "cursor_ordinal": 0,
                    "role": "value",
                    "node_id": part["nodeId"],
                    "kind": part["kind"],
                    "byte_length": part["byteLength"],
                    "status": "complete",
                }
                for part in binary_parts
            ]
            store_manifests.append(
                {
                    "name": store_name,
                    "key_path": schema["key_path"],
                    "auto_increment": schema["auto_increment"],
                    "indexes": [dict(item) for item in schema["indexes"]],
                    "output_file": output_relative,
                    "cursor_batch_size": 1,
                    "count_before_export": 1,
                    "cursor_row_count": 1,
                    "encoded_row_count": 1,
                    "failed_row_count": 0,
                    "unencoded_cursor_row_count": 0,
                    "attempted_output_bytes": len(line),
                    "output_committed": True,
                    "external_binary_part_count": len(binary_parts),
                    "external_binary_bytes": binary_bytes,
                    "external_binary_attempt_count": len(binary_attempts),
                    "failed_external_binary_part_count": 0,
                    "external_binary_parts": binary_attempts,
                    "count_matches": True,
                    "status": "complete",
                    "error": None,
                }
            )
            observed_schema.append(
                {
                    "name": store_name,
                    "key_path": schema["key_path"],
                    "auto_increment": schema["auto_increment"],
                    "indexes": [dict(item) for item in schema["indexes"]],
                }
            )
        manifest = {
            "manifest_schema": EXPORT_MANIFEST_SCHEMA,
            "status": "complete",
            "browser_export_complete": True,
            "extraction_complete": False,
            "extraction_complete_status": "pending_host_hash_and_replay_validation",
            "snapshot_id": "extension-snapshot-fixture",
            "source_kind": SOURCE_KIND,
            "started_at": "2026-08-10T00:00:00Z",
            "completed_at": "2026-08-10T00:01:00Z",
            "output_directory_name": run.name,
            "database": {
                "name": DATABASE_NAME,
                "expected_version": DATABASE_VERSION,
                "observed_version": DATABASE_VERSION,
            },
            "recovery_extension": {
                "expected_id": TARGET_EXTENSION_ID,
                "runtime_id": TARGET_EXTENSION_ID,
                "version": "0.1.0",
                "manifest_version": 3,
            },
            "recovery_provenance": self._provenance_for(replay),
            "browser": {"user_agent": "fixture"},
            "preflight": {
                "database_names_observed": [DATABASE_NAME],
                "passed": True,
            },
            "expected_store_names": list(EXPECTED_STORE_NAMES),
            "observed_schema": observed_schema,
            "schema_issues": [],
            "stores": store_manifests,
            "error": None,
            "excluded_operational_sources": ["ExtensionStorage"],
            "historical_versions_may_have_been_overwritten": True,
            "checksums": {
                "status": "deferred_to_host_after_chrome_exit",
                "algorithm": "sha256",
            },
        }
        (run / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return run

    def test_derives_fixed_chromium_extension_id(self) -> None:
        self.assertEqual(ARCHIVE_EXTENSION_ID, TARGET_EXTENSION_ID)
        self.assertEqual(
            derive_extension_id(self.extension / "manifest.json"), TARGET_EXTENSION_ID
        )

    def test_production_bundle_rejects_a_different_manifest_key(self) -> None:
        manifest_path = self.extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["key"] = base64.b64encode(b"different public key").decode("ascii")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ExtensionRecoveryError, "fixed archive extension ID"):
            build_production_extension_bundle(
                self.extension, self.root / "wrong-extension-id"
            )

    def test_builds_an_exact_five_file_production_bundle(self) -> None:
        for name in ("export.html", "export.css", "export.js", "tagged-json.js"):
            (self.extension / name).write_text(f"fixture:{name}\n", encoding="utf-8")
        smoke = self.extension / "smoke"
        smoke.mkdir()
        (smoke / "smoke.js").write_text(
            "indexedDB.deleteDatabase('fixture')\n", encoding="utf-8"
        )

        destination = self.root / "production-extension"
        built = build_production_extension_bundle(self.extension, destination)

        self.assertEqual(
            {path.name for path in destination.iterdir()},
            set(PRODUCTION_EXTENSION_FILES),
        )
        self.assertFalse((destination / "smoke").exists())
        self.assertEqual(
            validate_production_extension_bundle(destination).tree_sha256,
            built.tree_sha256,
        )

    def test_production_bundle_rejects_unapproved_manifest_surfaces(self) -> None:
        for name in ("export.html", "export.css", "export.js", "tagged-json.js"):
            (self.extension / name).write_text(f"fixture:{name}\n", encoding="utf-8")
        manifest_path = self.extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["update_url"] = "https://example.invalid/update.xml"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ExtensionRecoveryError, "unapproved field"):
            build_production_extension_bundle(
                self.extension, self.root / "unsafe-production-extension"
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_production_bundle_rejects_symlinked_runtime_files(self) -> None:
        external = self.root / "external-export.js"
        external.write_text("fixture\n", encoding="utf-8")
        runtime = self.extension / "export.js"
        runtime.unlink()
        os.symlink(external, runtime)

        with self.assertRaisesRegex(ExtensionRecoveryError, "cannot open"):
            build_production_extension_bundle(
                self.extension, self.root / "symlinked-production-extension"
            )

    def test_binds_only_chrome_for_testing_or_compatible_chromium(self) -> None:
        chrome_for_testing = self.root / "chrome-for-testing"
        chrome_for_testing.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'Google Chrome for Testing 151.0.7922.34'\n",
            encoding="utf-8",
        )
        chrome_for_testing.chmod(0o700)

        identity = inspect_compatible_chrome_binary(chrome_for_testing)
        self.assertEqual(identity.flavor, "chrome-for-testing")
        self.assertEqual(identity.version, "151.0.7922.34")
        self.assertEqual(
            identity.binary_sha256,
            hashlib.sha256(chrome_for_testing.read_bytes()).hexdigest(),
        )

        chromium = self.root / "chromium"
        chromium.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'Chromium 151.0.7922.34 built on Linux'\n",
            encoding="utf-8",
        )
        chromium.chmod(0o700)
        chromium_identity = inspect_compatible_chrome_binary(chromium)
        self.assertEqual(chromium_identity.flavor, "chromium")
        self.assertEqual(chromium_identity.version, "151.0.7922.34")

        branded = self.root / "google-chrome"
        branded.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'Google Chrome 151.0.7922.34'\n",
            encoding="utf-8",
        )
        branded.chmod(0o700)
        with self.assertRaisesRegex(ExtensionRecoveryError, "branded Google Chrome"):
            inspect_compatible_chrome_binary(branded)
        with self.assertRaisesRegex(ExtensionRecoveryError, "compatible Chrome"):
            inspect_compatible_chrome_binary("/bin/true")

    def test_prepares_two_independent_verified_replay_copies(self) -> None:
        prepared = self._prepare()
        self.assertEqual(len(prepared.replays), 2)
        self.assertEqual(prepared.target_extension_id, TARGET_EXTENSION_ID)
        first_file = prepared.replays[0].staged_indexeddb_directory / (
            f"chrome-extension_{SOURCE_EXTENSION_ID}_0.indexeddb.leveldb/CURRENT"
        )
        second_file = prepared.replays[1].staged_indexeddb_directory / (
            f"chrome-extension_{SOURCE_EXTENSION_ID}_0.indexeddb.leveldb/CURRENT"
        )
        self.assertEqual(first_file.read_bytes(), second_file.read_bytes())
        first_file.write_bytes(b"changed-independent-copy")
        self.assertNotEqual(first_file.read_bytes(), second_file.read_bytes())
        self.assertEqual(prepared.replays[0].phase, "awaiting_missing_database_preflight")

    def test_preparation_binds_bundle_browser_source_and_dedicated_outputs(self) -> None:
        prepared = prepare_replay_workspaces(
            self.working_copy,
            self.root / "prepared-bound",
            self.extension / "manifest.json",
            chrome_binary=self.chrome_for_testing,
            master_roots=(),
            active_profile_roots=(),
            unsafe_synthetic_only=True,
        )

        first, second = prepared.replays
        self.assertNotEqual(first.replay_id, second.replay_id)
        self.assertNotEqual(first.preparation_sha256, second.preparation_sha256)
        self.assertEqual(
            first.production_bundle_sha256, second.production_bundle_sha256
        )
        self.assertEqual(first.source_payload_sha256, second.source_payload_sha256)
        self.assertEqual(
            first.working_copy_evidence_tree_sha256,
            second.working_copy_evidence_tree_sha256,
        )
        self.assertEqual(first.browser_binary_sha256, second.browser_binary_sha256)
        self.assertFalse(first.output_directory.is_relative_to(first.root))
        self.assertFalse(second.output_directory.is_relative_to(second.root))
        self.assertNotEqual(first.output_directory, second.output_directory)
        self.assertEqual(
            set(path.name for path in first.production_extension_directory.iterdir()),
            set(PRODUCTION_EXTENSION_FILES),
        )

    def test_replay_state_mutation_breaks_the_preparation_binding(self) -> None:
        replay = self._prepare("prepared-state-binding").replays[0]
        state = json.loads(replay.state_path.read_text(encoding="utf-8"))
        state["source_payload_sha256"] = "0" * 64
        replay.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ExtensionRecoveryError, "preparation hash"):
            load_replay_workspace(replay.root)

    def test_replay_state_rejects_unknown_fields(self) -> None:
        replay = self._prepare("prepared-state-schema").replays[0]
        state = json.loads(replay.state_path.read_text(encoding="utf-8"))
        state["unexpected"] = True
        replay.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ExtensionRecoveryError, "state fields"):
            load_replay_workspace(replay.root)

    def test_replay_state_rejects_forged_phase_evidence(self) -> None:
        replay = self._prepare("prepared-state-phase").replays[0]
        state = json.loads(replay.state_path.read_text(encoding="utf-8"))
        state["missing_database_preflight"] = {"forged": True}
        replay.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ExtensionRecoveryError, "phase evidence"):
            load_replay_workspace(replay.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_replay_output_directory_symlink_is_rejected(self) -> None:
        replay = self._prepare("prepared-output-symlink").replays[0]
        replay.output_directory.rmdir()
        os.symlink(replay.user_data_directory, replay.output_directory)

        with self.assertRaisesRegex(ExtensionRecoveryError, "symlink"):
            load_replay_workspace(replay.root)

    def test_rejects_immutable_master_even_when_layout_is_valid(self) -> None:
        with self.assertRaisesRegex(ExtensionRecoveryError, "immutable master"):
            validate_recovery_working_copy(
                self.working_copy,
                master_roots=(self.working_copy,),
                active_profile_roots=(),
                unsafe_synthetic_only=True,
            )

    def test_production_validation_requires_manifest_master_and_active_roots(self) -> None:
        with self.assertRaisesRegex(ExtensionRecoveryError, "evidence manifest"):
            validate_recovery_working_copy(
                self.working_copy,
                master_roots=(),
                active_profile_roots=(),
            )

    def test_production_preparation_requires_and_binds_verified_protected_inputs(self) -> None:
        evidence_manifest_path = self.root / "working-copy-evidence-manifest.json"
        evidence_manifest = build_manifest(
            self.working_copy,
            source_id="fixture-working-copy",
            source_kind="recovery_working_copy",
            captured_at="2026-08-10T00:00:00Z",
        )
        write_manifest(
            evidence_manifest,
            evidence_manifest_path,
            evidence_root=self.working_copy,
        )
        master = self.root / "immutable-master"
        active_profile = self.root / "active-profile"
        master.mkdir()
        active_profile.mkdir()

        prepared = prepare_replay_workspaces(
            self.working_copy,
            self.root / "prepared-production-contract",
            self.extension / "manifest.json",
            chrome_binary=self.chrome_for_testing,
            master_roots=(master,),
            active_profile_roots=(active_profile,),
            evidence_manifest=evidence_manifest_path,
        )

        self.assertEqual(
            prepared.source_tree_sha256, evidence_manifest["tree_sha256"]
        )
        self.assertEqual(
            prepared.replays[0].working_copy_evidence_tree_sha256,
            evidence_manifest["tree_sha256"],
        )

    def test_verifies_declared_working_copy_manifest_before_copying(self) -> None:
        evidence_manifest = self.root / "working-copy-manifest.json"
        manifest = build_manifest(
            self.working_copy,
            source_id="fixture-working-copy",
            source_kind="recovery_working_copy",
            captured_at="2026-08-10T00:00:00Z",
        )
        write_manifest(manifest, evidence_manifest, evidence_root=self.working_copy)
        validated = validate_recovery_working_copy(
            self.working_copy,
            master_roots=(),
            active_profile_roots=(),
            evidence_manifest=evidence_manifest,
            unsafe_synthetic_only=True,
        )
        self.assertIsNotNone(validated.evidence_manifest_sha256)
        leveldb = self.working_copy / "IndexedDB" / (
            f"chrome-extension_{SOURCE_EXTENSION_ID}_0.indexeddb.leveldb"
        )
        (leveldb / "CURRENT").write_bytes(b"changed")
        with self.assertRaisesRegex(ExtensionRecoveryError, "does not match"):
            validate_recovery_working_copy(
                self.working_copy,
                master_roots=(),
                active_profile_roots=(),
                evidence_manifest=evidence_manifest,
                unsafe_synthetic_only=True,
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_working_copy_evidence_manifest_symlink_is_rejected(self) -> None:
        external_manifest = self.root / "external-working-copy-manifest.json"
        manifest = build_manifest(
            self.working_copy,
            source_id="fixture-working-copy",
            source_kind="recovery_working_copy",
            captured_at="2026-08-10T00:00:00Z",
        )
        write_manifest(
            manifest,
            external_manifest,
            evidence_root=self.working_copy,
        )
        symlink_manifest = self.root / "working-copy-manifest-symlink.json"
        os.symlink(external_manifest, symlink_manifest)

        with self.assertRaisesRegex(ExtensionRecoveryError, "cannot safely open|symlink"):
            validate_recovery_working_copy(
                self.working_copy,
                master_roots=(),
                active_profile_roots=(),
                evidence_manifest=symlink_manifest,
                unsafe_synthetic_only=True,
            )

    def test_rejects_extension_storage_and_other_unexpected_files(self) -> None:
        (self.working_copy / "ExtensionStorage").mkdir()
        with self.assertRaisesRegex(ExtensionRecoveryError, "only an IndexedDB"):
            validate_recovery_working_copy(
                self.working_copy,
                master_roots=(),
                active_profile_roots=(),
                unsafe_synthetic_only=True,
            )
        (self.working_copy / "ExtensionStorage").rmdir()
        (self.working_copy / "notes.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "only an IndexedDB"):
            validate_recovery_working_copy(
                self.working_copy,
                master_roots=(),
                active_profile_roots=(),
                unsafe_synthetic_only=True,
            )

    def test_active_profile_blocks_preflight_and_install(self) -> None:
        replay = self._prepare().replays[0]
        marker = replay.user_data_directory / "SingletonLock"
        marker.write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "appears active"):
            confirm_missing_database_preflight(
                replay.root,
                runtime_extension_id=TARGET_EXTENSION_ID,
                observed_database_names=(),
                chrome_exited=True,
            )
        marker.unlink()
        ready = confirm_missing_database_preflight(
            replay.root,
            runtime_extension_id=TARGET_EXTENSION_ID,
            observed_database_names=(),
            chrome_exited=True,
        )
        self.assertEqual(ready.phase, "ready_to_install")
        marker.write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "appears active"):
            install_recovery_indexeddb(replay.root, chrome_exited=True)

    def test_preflight_rejects_existing_database_then_origin_is_renamed(self) -> None:
        replay = self._prepare().replays[0]
        with self.assertRaisesRegex(ExtensionRecoveryError, "existed before"):
            confirm_missing_database_preflight(
                replay.root,
                runtime_extension_id=TARGET_EXTENSION_ID,
                observed_database_names=(DATABASE_NAME,),
                chrome_exited=True,
            )
        confirm_missing_database_preflight(
            replay.root,
            runtime_extension_id=TARGET_EXTENSION_ID,
            observed_database_names=(),
            chrome_exited=True,
        )
        installed = install_recovery_indexeddb(replay.root, chrome_exited=True)
        self.assertEqual(installed.phase, "indexeddb_installed")
        target = replay.user_data_directory / "Default" / "IndexedDB"
        self.assertTrue(
            (
                target
                / f"chrome-extension_{TARGET_EXTENSION_ID}_0.indexeddb.leveldb"
            ).is_dir()
        )
        self.assertTrue(
            (target / f"chrome-extension_{TARGET_EXTENSION_ID}_0.indexeddb.blob").is_dir()
        )
        self.assertFalse(
            (target / f"chrome-extension_{SOURCE_EXTENSION_ID}_0.indexeddb.leveldb").exists()
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_default_profile_symlink_cannot_escape_replay_workspace(self) -> None:
        replay = self._prepare().replays[0]
        escaped = self.root / "outside-profile"
        escaped.mkdir()
        os.symlink(escaped, replay.user_data_directory / "Default")
        with self.assertRaisesRegex(ExtensionRecoveryError, "unsafe Default"):
            confirm_missing_database_preflight(
                replay.root,
                runtime_extension_id=TARGET_EXTENSION_ID,
                observed_database_names=(),
                chrome_exited=True,
            )

    def test_constructs_only_profile_scoped_network_isolated_chrome_argv(self) -> None:
        replay = self._prepare().replays[0]
        argv = build_isolated_chrome_argv(
            replay.root,
            bwrap_binary=self.bubblewrap,
        )
        self.assertIn("--unshare-net", argv)
        self.assertIn(f"--user-data-dir={replay.user_data_directory}", argv)
        self.assertIn(
            f"--load-extension={replay.production_extension_directory}", argv
        )
        self.assertNotIn(f"--load-extension={self.extension}", argv)
        self.assertIn(str(self.chrome_for_testing.resolve()), argv)
        self.assertTrue(
            any(
                item.startswith(
                    f"chrome-extension://{TARGET_EXTENSION_ID}/export.html?"
                )
                for item in argv
            )
        )
        self.assertFalse(any("remote-debugging" in item for item in argv))
        self.assertNotIn("--no-sandbox", argv)
        self.assertIn("--ozone-platform=wayland", argv)
        self.assertIn("--password-store=basic", argv)
        self.assertNotIn(str(replay.output_directory), argv)

        confirm_missing_database_preflight(
            replay.root,
            runtime_extension_id=TARGET_EXTENSION_ID,
            observed_database_names=(),
            chrome_exited=True,
        )
        installed = install_recovery_indexeddb(replay.root, chrome_exited=True)
        export_argv = build_isolated_chrome_argv(
            installed.root, bwrap_binary=self.bubblewrap
        )
        self.assertIn(str(installed.output_directory), export_argv)
        export_url = next(
            item
            for item in export_argv
            if item.startswith(
                f"chrome-extension://{TARGET_EXTENSION_ID}/export.html?"
            )
        )
        query = parse_qs(urlparse(export_url).query, strict_parsing=True)
        self.assertEqual(query["replay_id"], ["replay-1"])
        self.assertEqual(query["challenge"], [installed.replay_challenge])
        self.assertEqual(
            query["production_bundle_sha256"],
            [installed.production_bundle_sha256],
        )
        with self.assertRaisesRegex(ExtensionRecoveryError, "bubblewrap"):
            build_isolated_chrome_argv(installed.root, bwrap_binary="/bin/true")

    def test_runs_only_bound_interactive_phases_and_waits_for_exit(self) -> None:
        replay = self._prepare().replays[0]

        def completed(argv, **_kwargs):
            executable = Path(argv[0]).name
            if executable == self.chrome_for_testing.name:
                return mock.Mock(
                    returncode=0,
                    stdout="Google Chrome for Testing 151.0.7922.34\n",
                )
            if executable == self.bubblewrap.name and argv[1:] == ["--version"]:
                return mock.Mock(returncode=0, stdout="bubblewrap 0.11.0\n")
            self.assertEqual(argv[0], str(self.bubblewrap.resolve()))
            self.assertIn("--unshare-net", argv)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch(
            "personal_vault.extension_recovery.subprocess.run",
            side_effect=completed,
        ):
            result = run_isolated_chrome(
                replay.root, bwrap_binary=self.bubblewrap
            )
        self.assertEqual(result.replay_id, "replay-1")
        self.assertEqual(result.phase, "awaiting_missing_database_preflight")
        self.assertEqual(result.returncode, 0)

        ready = confirm_missing_database_preflight(
            replay.root,
            runtime_extension_id=TARGET_EXTENSION_ID,
            observed_database_names=(),
            chrome_exited=True,
        )
        with self.assertRaisesRegex(ExtensionRecoveryError, "preflight or installed"):
            run_isolated_chrome(ready.root, bwrap_binary=self.bubblewrap)

    def test_discovers_only_one_safe_export_run(self) -> None:
        replay = self._default_validation_replays()[0]
        with self.assertRaisesRegex(ExtensionRecoveryError, "exactly one"):
            discover_export_run(replay.root)

        run = self._write_export("sole-run", replay=replay)
        self.assertEqual(discover_export_run(replay.root), run.resolve())

        (replay.output_directory / "unexpected").mkdir()
        with self.assertRaisesRegex(ExtensionRecoveryError, "exactly one"):
            discover_export_run(replay.root)

    def test_validates_streamed_store_and_binary_file_closure(self) -> None:
        validated = self._validate(self._write_export("run-valid"))
        self.assertEqual(validated.row_count, len(EXPECTED_STORE_NAMES))
        self.assertEqual(len(validated.stores), len(EXPECTED_STORE_NAMES))
        self.assertEqual(sum(store.binary_part_count for store in validated.stores), 1)
        self.assertEqual(validated.file_count, len(EXPECTED_STORE_NAMES) + 2)

    def test_copied_export_cannot_impersonate_the_second_replay(self) -> None:
        first_replay, second_replay = self._install_both_replays(
            "prepared-copy-rejection"
        )
        first_run = self._write_export(
            "run-bound-first", replay=first_replay
        )

        validated = validate_export_run(
            first_run,
            replay_root=first_replay.root,
            chrome_exited=True,
        )
        copied_run = second_replay.output_directory / first_run.name
        shutil.copytree(first_run, copied_run)
        with self.assertRaisesRegex(ExtensionRecoveryError, "replay provenance"):
            validate_export_run(
                copied_run,
                replay_root=second_replay.root,
                chrome_exited=True,
            )
        self.assertEqual(validated.replay_id, "replay-1")
        forged_copy = replace(validated, root=copied_run)
        comparison = compare_replay_exports(validated, forged_copy)
        self.assertFalse(comparison.extraction_complete)
        self.assertTrue(
            any("replay identity" in item for item in comparison.differences)
        )

    def test_host_validation_requires_chrome_exit(self) -> None:
        run = self._write_export("run-browser-open")
        replay = self._default_validation_replays()[0]
        with self.assertRaisesRegex(ExtensionRecoveryError, "exit completely"):
            validate_export_run(
                run,
                replay_root=replay.root,
                chrome_exited=False,
            )

    def test_host_validation_rejects_unknown_manifest_fields(self) -> None:
        run = self._write_export("run-extra-manifest-field")
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["unbound_host_note"] = "must fail closed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ExtensionRecoveryError, "manifest fields"):
            self._validate(run)

    def test_host_validation_rejects_unclosed_nested_manifest_contracts(self) -> None:
        database_run = self._write_export("run-extra-database-field")
        database_manifest_path = database_run / "manifest.json"
        database_manifest = json.loads(
            database_manifest_path.read_text(encoding="utf-8")
        )
        database_manifest["database"]["unbound"] = True
        database_manifest_path.write_text(
            json.dumps(database_manifest), encoding="utf-8"
        )
        with self.assertRaisesRegex(ExtensionRecoveryError, "database contract"):
            self._validate(database_run)

        history_run = self._write_export("run-history-boundary")
        history_manifest_path = history_run / "manifest.json"
        history_manifest = json.loads(history_manifest_path.read_text(encoding="utf-8"))
        history_manifest["historical_versions_may_have_been_overwritten"] = False
        history_manifest_path.write_text(json.dumps(history_manifest), encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "historical-version"):
            self._validate(history_run)

    def test_host_validation_rejects_schema_warnings_and_unexpected_indexes(self) -> None:
        warning_run = self._write_export("run-schema-warning")
        warning_manifest_path = warning_run / "manifest.json"
        warning_manifest = json.loads(
            warning_manifest_path.read_text(encoding="utf-8")
        )
        warning_manifest["schema_issues"] = [
            {
                "severity": "warning",
                "code": "unexpected_store",
                "store_name": "unbound-store",
            }
        ]
        warning_manifest_path.write_text(
            json.dumps(warning_manifest), encoding="utf-8"
        )
        with self.assertRaisesRegex(ExtensionRecoveryError, "schema issues"):
            self._validate(warning_run)

        index_run = self._write_export("run-unexpected-index")
        index_manifest_path = index_run / "manifest.json"
        index_manifest = json.loads(index_manifest_path.read_text(encoding="utf-8"))
        index_manifest["observed_schema"][0]["indexes"].append(
            {
                "name": "unbound-index",
                "key_path": "unbound",
                "unique": False,
                "multi_entry": False,
            }
        )
        index_manifest_path.write_text(json.dumps(index_manifest), encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "observed schema"):
            self._validate(index_run)

    def test_rejects_manifest_path_traversal(self) -> None:
        run = self._write_export("run-traversal")
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["stores"][0]["output_file"] = "../escape.ndjson"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "unsafe store output path"):
            self._validate(run)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_host_validation_rejects_symlink_before_reading_manifest(self) -> None:
        run = self._write_export("run-manifest-symlink")
        manifest_path = run / "manifest.json"
        external_manifest = self.root / "external-export-manifest.json"
        external_manifest.write_bytes(manifest_path.read_bytes())
        manifest_path.unlink()
        os.symlink(external_manifest, manifest_path)

        with self.assertRaisesRegex(
            ExtensionRecoveryError, "cannot safely scan browser export"
        ):
            self._validate(run)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs unavailable")
    def test_host_validation_rejects_special_files_without_blocking(self) -> None:
        run = self._write_export("run-special-file")
        os.mkfifo(run / "unexpected.fifo", 0o600)

        with self.assertRaisesRegex(
            ExtensionRecoveryError, "cannot safely scan browser export"
        ):
            self._validate(run)

    def test_rejects_missing_store_and_count_not_closed(self) -> None:
        missing_run = self._write_export("run-missing")
        missing_manifest_path = missing_run / "manifest.json"
        missing_manifest = json.loads(missing_manifest_path.read_text(encoding="utf-8"))
        missing_manifest["stores"].pop()
        missing_manifest["observed_schema"].pop()
        missing_manifest_path.write_text(json.dumps(missing_manifest), encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "store set is not closed"):
            self._validate(missing_run)

        count_run = self._write_export("run-count")
        count_manifest_path = count_run / "manifest.json"
        count_manifest = json.loads(count_manifest_path.read_text(encoding="utf-8"))
        count_manifest["stores"][0]["count_matches"] = False
        count_manifest_path.write_text(json.dumps(count_manifest), encoding="utf-8")
        with self.assertRaisesRegex(ExtensionRecoveryError, "count did not close"):
            self._validate(count_run)

    def test_detects_replay_row_store_and_binary_nondeterminism(self) -> None:
        replay_1, replay_2 = self._default_validation_replays()
        first = self._validate(
            self._write_export("run-first", marker="first", replay=replay_1),
            replay_1,
        )
        second = self._validate(
            self._write_export("run-second", marker="second", replay=replay_2),
            replay_2,
        )
        comparison = compare_replay_exports(first, second)
        self.assertFalse(comparison.deterministic)
        self.assertFalse(comparison.extraction_complete)
        self.assertTrue(
            any("row fingerprint differs" in difference for difference in comparison.differences)
        )

        identical = self._validate(
            self._write_export("run-identical", marker="first", replay=replay_2),
            replay_2,
        )
        self.assertTrue(compare_replay_exports(first, identical).extraction_complete)


if __name__ == "__main__":
    unittest.main()
