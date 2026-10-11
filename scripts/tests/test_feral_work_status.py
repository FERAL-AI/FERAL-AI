"""Recovery status checks use disposable Git trees, never a personal checkout."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "feral_work_status.py"
SPEC = importlib.util.spec_from_file_location("feral_work_status", MODULE_PATH)
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)


class WorkStatusTests(unittest.TestCase):
    def test_no_local_git_marker_refuses_ancestor_discovery(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(status, "git") as git:
            with self.assertRaisesRegex(status.StatusError, "no local .git"):
                status.collect_status(Path(directory))
            git.assert_not_called()

    def test_another_git_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with patch.object(status, "git", return_value=str(root.parent)) as git:
                with self.assertRaisesRegex(status.StatusError, "another checkout"):
                    status.collect_status(root)
                self.assertEqual(git.call_count, 1)

    def test_layout_is_required_before_origin_or_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with patch.object(status, "git", return_value=str(root)) as git:
                with self.assertRaisesRegex(status.StatusError, "FERAL source layout"):
                    status.collect_status(root)
                self.assertEqual(git.call_count, 1)

    def test_redaction_and_origin_identity(self):
        for remote in (
            "https://secret-token:password@github.com/FERAL-AI/FERAL-AI.git?token=secret#private",
            "ssh://secret-token@github.com/FERAL-AI/FERAL-AI.git",
            "secret-token@github.com:FERAL-AI/FERAL-AI.git",
        ):
            self.assertEqual(status.remote_identity(remote), status.EXPECTED_ORIGIN)
            redacted = status.redact_remote(remote)
            for secret in ("secret-token", "password", "private", "?token"):
                self.assertNotIn(secret, redacted)
        self.assertEqual(status.redact_remote("git@github.com:FERAL-AI/FERAL-AI.git"),
                         "git@github.com:FERAL-AI/FERAL-AI.git")

    def test_nul_paths_preserve_spaces_newlines_and_rename(self):
        raw = "R  new\nname.py\0old name.py\0?? quote\" name.txt\0"
        self.assertEqual(status.dirty_paths(raw), [
            {"status": "R ", "path": "new\nname.py", "original_path": "old name.py"},
            {"status": "??", "path": 'quote" name.txt'},
        ])
        with self.assertRaisesRegex(status.StatusError, "incomplete rename"):
            status.dirty_paths("R  destination\0")

    def test_git_failure_does_not_echo_private_stderr(self):
        result = subprocess.CompletedProcess([], 1, b"", b"secret-token private-path")
        with patch.object(status.subprocess, "run", return_value=result):
            with self.assertRaises(status.StatusError) as caught:
                status.git(Path("/irrelevant"), "remote", "get-url", "origin")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_missing_git_and_timeout_are_bounded_and_redacted(self):
        for failure in (FileNotFoundError("private-path"), subprocess.TimeoutExpired("secret", 15)):
            with patch.object(status.subprocess, "run", side_effect=failure):
                with self.assertRaisesRegex(status.StatusError, "unavailable or did not answer") as caught:
                    status.git(Path("/irrelevant"), "rev-parse", "HEAD")
                self.assertNotIn("private-path", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_git_ignores_ambient_overrides_and_disables_optional_writes(self):
        result = subprocess.CompletedProcess([], 0, b"fixture", b"")
        with patch.dict(status.os.environ, {"GIT_DIR": "/private/home-repo", "GIT_CONFIG_COUNT": "1"}):
            with patch.object(status.subprocess, "run", return_value=result) as run:
                self.assertEqual(status.git(Path("/fixture"), "status", "--porcelain=v1"), "fixture")
        kwargs = run.call_args.kwargs
        self.assertNotIn("GIT_DIR", kwargs["env"])
        self.assertNotIn("GIT_CONFIG_COUNT", kwargs["env"])
        self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertIn("core.fsmonitor=false", run.call_args.args[0])
        self.assertEqual(kwargs["timeout"], 15)

    @unittest.skipUnless(shutil.which("git"), "Git is required for isolated integration")
    def test_real_checkout_json_disk_failure_and_wrong_origin(self):
        with tempfile.TemporaryDirectory(prefix="feral status ") as directory:
            root = Path(directory)
            def run(*args):
                subprocess.run([
                    "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                    "-C", str(root), *args,
                ], check=True, capture_output=True)
            run("init")
            for marker in ("feral-core/api/server.py", "feral-core/pyproject.toml", *status.DOCUMENTS):
                path = root / marker
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            run("add", ".")
            run("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-m", "fixture")
            run("remote", "add", "origin", "https://secret-token@github.com/FERAL-AI/FERAL-AI.git")
            (root / "untracked\nfile.txt").write_text("private contents never printed")
            output = io.StringIO()
            with patch.object(status.shutil, "disk_usage", side_effect=OSError("private-path")):
                with redirect_stdout(output):
                    self.assertEqual(status.main(["--repo", str(root), "--json"]), 0)
            result = json.loads(output.getvalue())
            self.assertIsNone(result["disk_free_bytes"])
            self.assertTrue(result["warnings"])
            self.assertTrue(all(result["documents"].values()))
            self.assertEqual(result["dirty_paths"][0]["path"], "untracked\nfile.txt")
            self.assertNotIn("secret-token", output.getvalue())
            self.assertNotIn("private contents", output.getvalue())
            run("remote", "set-url", "origin", "https://secret-token@github.com/other/repo.git")
            with self.assertRaisesRegex(status.StatusError, "Origin must identify"):
                status.collect_status(root)

    def test_error_json_exit_status(self):
        output = io.StringIO()
        with patch.object(status, "collect_status", side_effect=status.StatusError("wrong checkout")):
            with redirect_stdout(output):
                self.assertEqual(status.main(["--json"]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "error": "wrong checkout"})


if __name__ == "__main__":
    unittest.main()
