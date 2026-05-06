"""Unit tests for `derive_image_tag` (no Docker required).

Verifies that the git-derived tag refuses dirty / non-repo sources and
matches the first 12 chars of `git rev-parse HEAD` when the work tree
is clean. Skips if `git --version` does not work.

Run standalone (`python3 tests/test_image_tag.py`) or via pytest; the
script self-skips on either entrypoint.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from harness import IMAGE_REPO, build_context_dir, derive_image_tag  # noqa: E402

TAG_RE = re.compile(r"^%s:[0-9a-f]{12}$" % re.escape(IMAGE_REPO))


def _git_available() -> bool:
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


class _Skip(Exception):
    pass


@unittest.skipUnless(_git_available(), "`git` not on PATH")
class DeriveImageTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sublime-mcp-tag-test-"))
        # Local-config-only env so user/system git config (signing, hooks,
        # commit templates) doesn't bleed into the synthetic repo.
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.tmp), *argv],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def _init_clean_repo(self) -> str:
        """Init a repo with one committed file, return the HEAD sha."""
        self._git("init", "-q", "-b", "main")
        (self.tmp / "fixture.txt").write_text("hello\n")
        self._git("add", "fixture.txt")
        self._git("commit", "-q", "-m", "initial")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        return head

    def test_clean_repo_returns_short_sha_tag(self) -> None:
        head = self._init_clean_repo()
        tag = derive_image_tag(self.tmp)
        self.assertEqual(tag, "%s:%s" % (IMAGE_REPO, head[:12]))

    def test_tag_shape(self) -> None:
        self._init_clean_repo()
        tag = derive_image_tag(self.tmp)
        self.assertRegex(tag, TAG_RE)

    def test_dirty_work_tree_refuses(self) -> None:
        self._init_clean_repo()
        (self.tmp / "fixture.txt").write_text("hello\nworld\n")
        with self.assertRaises(RuntimeError) as ctx:
            derive_image_tag(self.tmp)
        self.assertIn("uncommitted changes", str(ctx.exception))
        self.assertIn("fixture.txt", str(ctx.exception))

    def test_untracked_file_refuses(self) -> None:
        self._init_clean_repo()
        (self.tmp / "untracked.txt").write_text("x")
        with self.assertRaises(RuntimeError) as ctx:
            derive_image_tag(self.tmp)
        self.assertIn("uncommitted changes", str(ctx.exception))
        self.assertIn("untracked.txt", str(ctx.exception))

    def test_non_git_dir_refuses(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            derive_image_tag(self.tmp)
        msg = str(ctx.exception)
        self.assertIn("not a git repository", msg)
        self.assertIn("--image-tag", msg)


@unittest.skipUnless(_git_available(), "`git` not on PATH")
class ProductionWiringTests(unittest.TestCase):
    """Sanity-check that the real harness checkout's tag is shaped correctly.

    Skipped when the checkout is dirty (e.g. mid-edit during dev) — the
    derive function intentionally refuses, and we don't want to mask
    that with a soft skip in tests.
    """

    def test_real_checkout_tag_shape(self) -> None:
        ctx = build_context_dir()
        status = subprocess.run(
            ["git", "-C", str(ctx), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if status.returncode != 0:
            self.skipTest("real checkout is not a git repository")
        if status.stdout.strip():
            self.skipTest("real checkout is dirty; not exercising derive_image_tag")
        tag = derive_image_tag(ctx)
        self.assertRegex(tag, TAG_RE)


if __name__ == "__main__":
    if not _git_available():
        sys.stderr.write("skipping: `git` not on PATH\n")
        sys.exit(0)
    unittest.main(verbosity=2)
