from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_dm.patches import (
    apply_patch_operations,
    create_patch_set,
    write_patch_module,
)


class DynamicDmPatchTests(unittest.TestCase):
    def assert_reproduces(self, upstream: str, local: str) -> None:
        patch_set = create_patch_set(upstream, local, "code/example.dm")

        self.assertTrue(patch_set.changed)
        self.assertEqual(apply_patch_operations(upstream, patch_set.operations), local)

    def test_infers_single_line_replacement(self) -> None:
        self.assert_reproduces(
            "/datum/foo\n\tvar/value = 1\n",
            "/datum/foo\n\tvar/value = 2\n",
        )

    def test_infers_insert_after_anchor(self) -> None:
        patch_set = create_patch_set(
            "/datum/foo\n\tvar/value = 1\n",
            "/datum/foo\n\tvar/value = 1\n\tvar/extra = TRUE\n",
            "code/example.dm",
        )

        self.assertEqual(patch_set.operations[0].mode, "insert_after")
        self.assertEqual(apply_patch_operations("/datum/foo\n\tvar/value = 1\n", patch_set.operations), "/datum/foo\n\tvar/value = 1\n\tvar/extra = TRUE\n")

    def test_infers_multiline_block_replace(self) -> None:
        patch_set = create_patch_set(
            "/datum/foo/proc/run()\n\tif(active)\n\t\told_call()\n\t\treturn TRUE\n\treturn FALSE\n",
            "/datum/foo/proc/run()\n\tif(active)\n\t\tnew_call()\n\t\tlog_world(\"changed\")\n\t\treturn FALSE\n\treturn FALSE\n",
            "code/example.dm",
        )

        self.assertTrue(patch_set.changed)
        self.assertEqual(patch_set.operations[0].mode, "replace_between")
        self.assertEqual(
            apply_patch_operations(
                "/datum/foo/proc/run()\n\tif(active)\n\t\told_call()\n\t\treturn TRUE\n\treturn FALSE\n",
                patch_set.operations,
            ),
            "/datum/foo/proc/run()\n\tif(active)\n\t\tnew_call()\n\t\tlog_world(\"changed\")\n\t\treturn FALSE\n\treturn FALSE\n",
        )

    def test_infers_multiple_hunks(self) -> None:
        self.assert_reproduces(
            "/datum/foo\n\tvar/a = 1\n\tvar/b = 2\n\tvar/c = 3\n",
            "/datum/foo\n\tvar/a = 10\n\tvar/b = 2\n\tvar/c = 30\n",
        )

    def test_repeated_multiline_block_uses_correct_occurrence(self) -> None:
        upstream = (
            "if(TRUE)\n"
            "\tthing = 1\n"
            "if(TRUE)\n"
            "\tthing = 1\n"
        )
        local = (
            "if(TRUE)\n"
            "\tthing = 1\n"
            "if(TRUE)\n"
            "\tthing = 2\n"
        )
        self.assert_reproduces(upstream, local)

    def test_contextual_patch_handles_repeated_changed_line(self) -> None:
        upstream = (
            "/datum/foo/proc/a()\n"
            "\treturn TRUE\n"
            "/datum/foo/proc/b()\n"
            "\treturn TRUE\n"
        )
        local = (
            "/datum/foo/proc/a()\n"
            "\treturn TRUE\n"
            "/datum/foo/proc/b()\n"
            "\treturn FALSE\n"
        )
        self.assert_reproduces(upstream, local)

    def test_full_file_fallback_can_be_disabled(self) -> None:
        upstream = "\n".join(f"\told_{index}()" for index in range(200)) + "\n"
        local = "\n".join(f"\tnew_{index}()" for index in range(200)) + "\n"

        failed = create_patch_set(
            upstream,
            local,
            "code/example.dm",
            allow_full_file_fallback=False,
        )
        fallback = create_patch_set(
            upstream,
            local,
            "code/example.dm",
            allow_full_file_fallback=True,
        )

        self.assertFalse(failed.changed)
        self.assertTrue(fallback.changed)
        self.assertEqual(fallback.strategy, "full-file-replace")
        self.assertEqual(apply_patch_operations(upstream, fallback.operations), local)

    def test_writes_module_manifest_and_patch_files(self) -> None:
        patch_set = create_patch_set(
            "/datum/foo\n\tvar/value = 1\n",
            "/datum/foo\n\tvar/value = 2\n",
            "code/example.dm",
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = write_patch_module(
                Path(tmp),
                "example-module",
                "Example Module",
                [patch_set],
                base_ref="upstream/master",
            )

            manifest = result.manifest_path.read_text(encoding="utf-8")
            self.assertIn('id = "example-module"', manifest)
            self.assertIn('requires = ["dynamic-dm"]', manifest)
            self.assertIn('target_file = "code/example.dm"', manifest)
            self.assertEqual(len(result.patch_files), 1)
            self.assertEqual(result.patch_files[0].read_text(encoding="utf-8"), "\tvar/value = 2\n")


if __name__ == "__main__":
    unittest.main()
