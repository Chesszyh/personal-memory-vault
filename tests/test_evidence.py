from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import personal_vault.evidence as evidence_module
from personal_vault.evidence import (
    MANIFEST_SCHEMA_VERSION,
    EvidenceEntry,
    EvidenceError,
    build_manifest,
    load_manifest,
    manifest_payload_sha256,
    scan_evidence,
    tree_sha256,
    verify_manifest,
    write_manifest,
)


class EvidenceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "evidence"
        self.root.mkdir()
        (self.root / "nested").mkdir()
        (self.root / "empty").mkdir()
        (self.root / "nested" / "message.json").write_text('{"ok": true}\n')
        (self.root / ".hidden").write_bytes(b"hidden")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> dict[str, object]:
        return build_manifest(
            self.root,
            source_id="source-1",
            source_kind="test",
            captured_at="2026-08-10T00:00:00+08:00",
        )

    def test_manifest_is_deterministic_except_generation_time(self) -> None:
        first = self.build()
        second = self.build()
        self.assertEqual(first["entries"], second["entries"])
        self.assertEqual(first["tree_sha256"], second["tree_sha256"])
        self.assertEqual(
            first["manifest_payload_sha256"], second["manifest_payload_sha256"]
        )
        self.assertEqual(first["totals"], second["totals"])

    def test_schema_v2_payload_digest_binds_provenance_but_not_generation_time(
        self,
    ) -> None:
        manifest = self.build()
        self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA_VERSION)

        source_tampered = copy.deepcopy(manifest)
        source_tampered["source"]["id"] = "forged"
        with self.assertRaisesRegex(EvidenceError, "payload digest"):
            verify_manifest(self.root, source_tampered)

        generated_at_changed = copy.deepcopy(manifest)
        generated_at_changed["generated_at"] = "2030-01-01T00:00:00+00:00"
        self.assertEqual(
            manifest_payload_sha256(generated_at_changed),
            manifest["manifest_payload_sha256"],
        )
        self.assertTrue(verify_manifest(self.root, generated_at_changed).ok)

    def test_date_only_capture_preserves_day_precision(self) -> None:
        manifest = build_manifest(
            self.root,
            source_id="source-day",
            source_kind="test",
            captured_at="2026-08-10",
        )

        self.assertEqual(manifest["source"]["captured_at"], "2026-08-10")
        self.assertEqual(manifest["source"]["captured_at_precision"], "day")
        output = Path(self.temporary.name) / "day-manifest.json"
        write_manifest(manifest, output, evidence_root=self.root)
        self.assertTrue(verify_manifest(self.root, load_manifest(output)).ok)

    def test_round_trip_verifies_and_detects_change(self) -> None:
        output = Path(self.temporary.name) / "manifest.json"
        write_manifest(self.build(), output, evidence_root=self.root)
        manifest = load_manifest(output)
        self.assertTrue(verify_manifest(self.root, manifest).ok)

        (self.root / "nested" / "message.json").write_text("changed\n")
        result = verify_manifest(self.root, manifest)
        self.assertFalse(result.ok)
        self.assertEqual(result.differences, ("changed: nested/message.json",))

    def test_verify_detects_unexpected_missing_and_type_change(self) -> None:
        manifest = self.build()

        (self.root / "unexpected").write_bytes(b"new")
        result = verify_manifest(self.root, manifest)
        self.assertIn("unexpected: unexpected", result.differences)

        (self.root / "unexpected").unlink()
        (self.root / "nested" / "message.json").unlink()
        result = verify_manifest(self.root, manifest)
        self.assertIn("missing: nested/message.json", result.differences)

        (self.root / "nested" / "message.json").write_text('{"ok": true}\n')
        (self.root / ".hidden").unlink()
        (self.root / ".hidden").mkdir()
        result = verify_manifest(self.root, manifest)
        self.assertIn("changed: .hidden", result.differences)

    def test_tree_and_payload_hashes_are_independent_of_entry_order(self) -> None:
        manifest = self.build()
        entries = tuple(EvidenceEntry(**entry) for entry in manifest["entries"])
        reversed_entries = tuple(reversed(entries))
        self.assertEqual(tree_sha256(entries), tree_sha256(reversed_entries))

        reordered = copy.deepcopy(manifest)
        reordered["entries"].reverse()
        self.assertEqual(
            manifest_payload_sha256(reordered), manifest["manifest_payload_sha256"]
        )
        result = verify_manifest(self.root, reordered)
        self.assertTrue(result.ok)
        self.assertEqual(result.differences, ())

    def test_manifest_cannot_be_written_inside_evidence(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "outside"):
            write_manifest(
                self.build(), self.root / "manifest.json", evidence_root=self.root
            )

        nested_parent = self.root / "must-not-be-created"
        with self.assertRaisesRegex(EvidenceError, "outside"):
            write_manifest(
                self.build(), nested_parent / "manifest.json", evidence_root=self.root
            )
        self.assertFalse(nested_parent.exists())

        manifest = self.build()
        outside_target = Path(self.temporary.name) / "outside-manifest.json"
        output_link = self.root / "manifest-link.json"
        os.symlink(outside_target, output_link)
        with self.assertRaisesRegex(EvidenceError, "outside"):
            write_manifest(manifest, output_link, evidence_root=self.root)
        self.assertTrue(output_link.is_symlink())
        self.assertFalse(outside_target.exists())

    def test_written_manifest_is_private_and_fsyncs_file_and_directory(self) -> None:
        output = Path(self.temporary.name) / "manifests" / "manifest.json"
        fsync_kinds: list[str] = []
        original_fsync = os.fsync

        def recording_fsync(descriptor: int) -> None:
            mode = os.fstat(descriptor).st_mode
            fsync_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
            original_fsync(descriptor)

        with mock.patch("personal_vault.evidence.os.fsync", side_effect=recording_fsync):
            write_manifest(self.build(), output, evidence_root=self.root)

        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(fsync_kinds, ["file", "directory"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_target_is_recorded_without_following_it(self) -> None:
        os.symlink("nested/message.json", self.root / "message-link")
        manifest = self.build()
        link = next(
            entry for entry in manifest["entries"] if entry["path"] == "message-link"
        )
        self.assertEqual(link["kind"], "symlink")
        self.assertEqual(link["link_target"], "nested/message.json")
        self.assertEqual(
            link["sha256"], hashlib.sha256(b"nested/message.json").hexdigest()
        )

    @unittest.skipUnless(os.name == "posix", "raw POSIX filenames required")
    def test_non_utf8_filename_round_trips(self) -> None:
        raw_name = b"invalid-\xff"
        root_bytes = os.fsencode(self.root)
        descriptor = os.open(
            root_bytes + b"/" + raw_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, b"raw-name")
        finally:
            os.close(descriptor)
        os.symlink(b"missing-target-\xfe", root_bytes + b"/raw-target-link")

        output = Path(self.temporary.name) / "raw-name-manifest.json"
        write_manifest(self.build(), output, evidence_root=self.root)
        serialized = output.read_text(encoding="utf-8")
        self.assertIn("\\udcff", serialized)
        self.assertIn("\\udcfe", serialized)
        self.assertTrue(verify_manifest(self.root, load_manifest(output)).ok)

    def test_non_posix_scan_fails_closed(self) -> None:
        with mock.patch("personal_vault.evidence.os.name", "nt"):
            with self.assertRaisesRegex(EvidenceError, "requires POSIX"):
                scan_evidence(self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_root_is_rejected(self) -> None:
        root_link = Path(self.temporary.name) / "root-link"
        os.symlink(self.root, root_link)
        with self.assertRaisesRegex(EvidenceError, "non-symlink"):
            scan_evidence(root_link)

    @unittest.skipUnless(os.name == "posix", "POSIX dir_fd behavior required")
    def test_file_swap_to_external_symlink_is_rejected(self) -> None:
        race = self.root / "race"
        race.write_bytes(b"inside")
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"outside")
        original_open = os.open
        swapped = False

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal swapped
            if path == "race" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                race.unlink()
                race.symlink_to(outside)
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch("personal_vault.evidence.os.open", side_effect=racing_open),
            mock.patch("personal_vault.evidence._require_safe_posix_io"),
        ):
            with self.assertRaisesRegex(EvidenceError, "cannot open"):
                scan_evidence(self.root)
        self.assertTrue(swapped)

    @unittest.skipUnless(os.name == "posix", "POSIX dir_fd behavior required")
    def test_same_size_rewrite_with_restored_mtime_is_rejected(self) -> None:
        race = self.root / "race"
        race.write_bytes(b"old!")
        original_metadata = race.stat()
        original_open = os.open
        changed = False

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal changed
            if path == "race" and kwargs.get("dir_fd") is not None and not changed:
                changed = True
                race.write_bytes(b"new!")
                os.utime(
                    race,
                    ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
                )
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch("personal_vault.evidence.os.open", side_effect=racing_open),
            mock.patch("personal_vault.evidence._require_safe_posix_io"),
        ):
            with self.assertRaisesRegex(EvidenceError, "changed before hashing"):
                scan_evidence(self.root)
        self.assertTrue(changed)

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO behavior required")
    def test_file_swap_to_fifo_is_rejected_without_blocking(self) -> None:
        race = self.root / "race"
        race.write_bytes(b"inside")
        original_open = os.open
        swapped = False

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal swapped
            if path == "race" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                race.unlink()
                os.mkfifo(race, 0o600)
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch("personal_vault.evidence.os.open", side_effect=racing_open),
            mock.patch("personal_vault.evidence._require_safe_posix_io"),
        ):
            with self.assertRaisesRegex(EvidenceError, "changed type"):
                scan_evidence(self.root)
        self.assertTrue(swapped)

    @unittest.skipUnless(os.name == "posix", "POSIX dir_fd behavior required")
    def test_file_added_after_directory_enumeration_is_rejected(self) -> None:
        original_hash = __import__(
            "personal_vault.evidence", fromlist=["_hash_regular_file_at"]
        )._hash_regular_file_at
        added = False

        def hash_then_add(*args: object, **kwargs: object) -> object:
            nonlocal added
            result = original_hash(*args, **kwargs)
            if not added:
                added = True
                (self.root / "late").write_bytes(b"late")
            return result

        with mock.patch(
            "personal_vault.evidence._hash_regular_file_at", side_effect=hash_then_add
        ):
            with self.assertRaisesRegex(EvidenceError, "members changed"):
                scan_evidence(self.root)
        self.assertTrue(added)

    def test_rejects_manifest_with_tampered_tree_hash(self) -> None:
        output = Path(self.temporary.name) / "manifest.json"
        manifest = self.build()
        manifest["tree_sha256"] = "0" * 64
        output.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "tree hash"):
            verify_manifest(self.root, load_manifest(output))

    def test_rejects_legacy_schema_with_migration_message(self) -> None:
        manifest = self.build()
        manifest["schema_version"] = 1
        with self.assertRaisesRegex(EvidenceError, "legacy.*regenerate"):
            verify_manifest(self.root, manifest)

    def test_load_rejects_duplicate_json_keys(self) -> None:
        output = Path(self.temporary.name) / "duplicate.json"
        output.write_text('{"schema_version": 2, "schema_version": 2}\n')
        with self.assertRaisesRegex(EvidenceError, "duplicate key"):
            load_manifest(output)

    def test_rejects_invalid_manifest_shapes_and_values(self) -> None:
        manifest = self.build()
        cases: list[tuple[str, object, str]] = []

        cases.append(("top-level list", [], "JSON object"))

        invalid_path = copy.deepcopy(manifest)
        invalid_path["entries"][0]["path"] = "../escape"
        cases.append(("unsafe path", invalid_path, "safe relative path"))

        absolute_path = copy.deepcopy(manifest)
        absolute_path["entries"][0]["path"] = "/escape"
        cases.append(("absolute path", absolute_path, "safe relative path"))

        nul_path = copy.deepcopy(manifest)
        nul_path["entries"][0]["path"] = "bad\0name"
        cases.append(("NUL path", nul_path, "without NUL"))

        invalid_kind = copy.deepcopy(manifest)
        invalid_kind["entries"][0]["kind"] = "device"
        cases.append(("invalid kind", invalid_kind, "kind is invalid"))

        unhashable_kind = copy.deepcopy(manifest)
        unhashable_kind["entries"][0]["kind"] = []
        cases.append(("unhashable kind", unhashable_kind, "kind is invalid"))

        unpaired_surrogate = copy.deepcopy(manifest)
        unpaired_surrogate["entries"][0]["path"] = "invalid-\ud800"
        cases.append(
            (
                "unpaired surrogate",
                unpaired_surrogate,
                "cannot be represented by the filesystem",
            )
        )

        negative_size = copy.deepcopy(manifest)
        file_entry = next(
            entry for entry in negative_size["entries"] if entry["kind"] == "file"
        )
        file_entry["size"] = -1
        cases.append(("negative size", negative_size, "file entry fields"))

        invalid_sha = copy.deepcopy(manifest)
        file_entry = next(
            entry for entry in invalid_sha["entries"] if entry["kind"] == "file"
        )
        file_entry["sha256"] = "ABC"
        cases.append(("invalid sha", invalid_sha, "file entry fields"))

        invalid_totals = copy.deepcopy(manifest)
        invalid_totals["totals"]["entries"] += 1
        cases.append(("invalid totals", invalid_totals, "totals"))

        missing_field = copy.deepcopy(manifest)
        del missing_field["source"]
        cases.append(("missing field", missing_field, "top-level fields"))

        extra_field = copy.deepcopy(manifest)
        extra_field["extra"] = True
        cases.append(("extra field", extra_field, "top-level fields"))

        naive_time = copy.deepcopy(manifest)
        naive_time["source"]["captured_at"] = "2026-08-10T00:00:00"
        cases.append(("timezone missing", naive_time, "timezone"))

        for label, candidate, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(EvidenceError, message):
                    verify_manifest(self.root, candidate)  # type: ignore[arg-type]

    def test_fstat_failures_close_newly_opened_descriptors(self) -> None:
        parent_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            nested_stat = os.stat("nested", dir_fd=parent_fd, follow_symlinks=False)
            closed: list[int] = []
            original_close = os.close

            def recording_close(descriptor: int) -> None:
                closed.append(descriptor)
                original_close(descriptor)

            with (
                mock.patch(
                    "personal_vault.evidence.os.fstat",
                    side_effect=OSError(5, "synthetic fstat failure"),
                ),
                mock.patch(
                    "personal_vault.evidence.os.close", side_effect=recording_close
                ),
            ):
                with self.assertRaisesRegex(EvidenceError, "inspect directory"):
                    evidence_module._open_directory_at(
                        parent_fd, "nested", "nested", nested_stat
                    )
            self.assertEqual(len(closed), 1)

            closed.clear()
            with (
                mock.patch(
                    "personal_vault.evidence.os.fstat",
                    side_effect=OSError(5, "synthetic fstat failure"),
                ),
                mock.patch(
                    "personal_vault.evidence.os.close", side_effect=recording_close
                ),
            ):
                with self.assertRaisesRegex(EvidenceError, "inspect manifest"):
                    evidence_module._open_output_directory(self.root)
            self.assertEqual(len(closed), 1)
        finally:
            os.close(parent_fd)


if __name__ == "__main__":
    unittest.main()
