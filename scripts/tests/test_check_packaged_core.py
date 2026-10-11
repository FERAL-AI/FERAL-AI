"""Real disposable Git blobs prove the packaged-source equality gate."""
from pathlib import Path
import os
import runpy
import subprocess
import tempfile
import unittest

compare_payload = runpy.run_path(str(Path(__file__).resolve().parents[1] / "check_packaged_core.py"))["compare_payload"]


class PackagedCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="feral-package-source-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        source = self.repo / "feral-core/api/server.py"
        source.parent.mkdir(parents=True)
        source.write_text("# synthetic unicode: café\nprint('fixture')\n")
        self.git("add", "feral-core")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
        self.revision = self.git("rev-parse", "HEAD").strip()
        self.bundle = self.root / "FERAL.app"
        self.core = self.bundle / "Contents/Resources/feral-core"
        (self.core / "api").mkdir(parents=True)
        self.packaged = self.core / "api/server.py"
        self.packaged.write_bytes(source.read_bytes())

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.check_output(["git", "-C", str(self.repo), *args], env=env, stderr=subprocess.PIPE, text=True)

    def audit(self, revision=None):
        return compare_payload(self.bundle, self.repo, revision or self.revision)

    def codes(self, result):
        return {entry["code"] for entry in result["issues"]}

    def test_exact_frozen_unicode_blob_passes(self):
        result = self.audit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked_files"], 1)
        self.assertEqual(result["source_revision"], self.revision)

    def test_working_tree_does_not_replace_committed_source(self):
        (self.repo / "feral-core/api/server.py").write_text("UNCOMMITTED_FIXTURE\n")
        self.assertTrue(self.audit()["ok"])
        self.packaged.write_text("UNCOMMITTED_FIXTURE\n")
        self.assertEqual(self.codes(self.audit()), {"source_content_mismatch"})

    def test_missing_source_fails(self):
        self.packaged.unlink()
        self.assertIn("source_missing", self.codes(self.audit()))

    def test_untracked_runtime_source_fails(self):
        (self.core / "stale.py").write_text("STALE_SYNTHETIC_CODE\n")
        self.assertIn("unexpected_production_source", self.codes(self.audit()))

    def test_external_symlink_fails_even_for_matching_content(self):
        external = self.root / "external.py"
        external.write_bytes(self.packaged.read_bytes())
        self.packaged.unlink()
        self.packaged.symlink_to(external)
        self.assertIn("source_external_or_symlink", self.codes(self.audit()))

    def test_full_commit_sha_required(self):
        for revision in ("HEAD", "--help", self.revision[:8]):
            self.assertEqual(self.codes(self.audit(revision)), {"full_commit_sha_required"})

    def test_tree_object_cannot_claim_commit_identity(self):
        tree = self.git("rev-parse", "HEAD^{tree}").strip()
        self.assertEqual(self.codes(self.audit(tree)), {"source_commit_unavailable"})

    def test_missing_commit_fails(self):
        self.assertEqual(self.codes(self.audit("0" * 40)), {"source_commit_unavailable"})

    def test_empty_production_inventory_fails(self):
        self.packaged.unlink()
        (self.repo / "feral-core/api/server.py").unlink()
        self.git("add", "feral-core")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "empty")
        self.assertEqual(self.codes(self.audit(self.git("rev-parse", "HEAD").strip())), {"source_inventory_empty_or_over_budget"})


if __name__ == "__main__":
    unittest.main()
