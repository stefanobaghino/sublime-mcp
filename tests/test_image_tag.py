"""Unit tests for `derive_image_tag` (no Docker required).

Verifies that the git-derived tag matches the first 12 chars of
`git rev-parse HEAD`, gains a `-dirty` suffix when the work tree has
staged / unstaged / untracked changes, and refuses outright when the
source isn't a git repo at all. Skips if `git --version` does not work.

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
DIRTY_TAG_RE = re.compile(r"^%s:[0-9a-f]{12}-dirty$" % re.escape(IMAGE_REPO))
ANY_TAG_RE = re.compile(r"^%s:[0-9a-f]{12}(-dirty)?$" % re.escape(IMAGE_REPO))


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
        tag, dirty = derive_image_tag(self.tmp)
        self.assertEqual(tag, "%s:%s" % (IMAGE_REPO, head[:12]))
        self.assertFalse(dirty)

    def test_tag_shape(self) -> None:
        self._init_clean_repo()
        tag, _dirty = derive_image_tag(self.tmp)
        self.assertRegex(tag, TAG_RE)

    def test_dirty_work_tree_returns_dirty_tag(self) -> None:
        head = self._init_clean_repo()
        (self.tmp / "fixture.txt").write_text("hello\nworld\n")
        tag, dirty = derive_image_tag(self.tmp)
        self.assertEqual(tag, "%s:%s-dirty" % (IMAGE_REPO, head[:12]))
        self.assertTrue(dirty)
        self.assertRegex(tag, DIRTY_TAG_RE)

    def test_untracked_file_returns_dirty_tag(self) -> None:
        head = self._init_clean_repo()
        (self.tmp / "untracked.txt").write_text("x")
        tag, dirty = derive_image_tag(self.tmp)
        self.assertEqual(tag, "%s:%s-dirty" % (IMAGE_REPO, head[:12]))
        self.assertTrue(dirty)

    def test_non_git_dir_refuses(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            derive_image_tag(self.tmp)
        msg = str(ctx.exception)
        self.assertIn("not a git repository", msg)
        self.assertIn("--image-tag", msg)


@unittest.skipUnless(_git_available(), "`git` not on PATH")
class ProductionWiringTests(unittest.TestCase):
    """Sanity-check that the real harness checkout's tag is shaped correctly.

    Accepts either the clean `<sha12>` tag or the `<sha12>-dirty` tag,
    so this test stays green during contributor mid-edit runs.
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
        tag, dirty = derive_image_tag(ctx)
        self.assertRegex(tag, ANY_TAG_RE)
        self.assertEqual(dirty, bool(status.stdout.strip()))


if __name__ == "__main__":
    if not _git_available():
        sys.stderr.write("skipping: `git` not on PATH\n")
        sys.exit(0)
    unittest.main(verbosity=2)
