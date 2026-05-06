"""Build-and-exec shim that fronts the dockerized sublime-mcp.

The shim resolves an image tag from the harness checkout's git HEAD,
builds the image if missing (or forced via `--rebuild`), and then
`exec`s into `docker run -i --rm <image>`. The in-container bridge
(see bridge.py) owns the JSON-RPC stdio after exec; dockerd reaps
the container when the parent docker CLI dies.

Registered with Claude Code as a stdio MCP server:

    claude mcp add --scope user --transport stdio sublime-text -- \\
        sublime-mcp --mount "$PWD:/work" \\
                    --agent-name <agent> --session-id <uuid>

`--agent-name` and `--session-id` only affect the container's
`--name` for `docker ps` visibility; they are not load-bearing.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE_REPO = "sublime-mcp-harness"
# Build-time label key applied to every image we build, so cleanup can
# target this project's images precisely:
#   docker images --filter "label=sublime-mcp-harness-image" -q
IMAGE_LABEL_KEY = "sublime-mcp-harness-image"
CONTAINER_LICENSE_DIR = "/root/.config/sublime-text/Local"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d  %(levelname)-7s  [%(component)s]  %(message)s"
)
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.component = record.name.rsplit(".", 1)[-1]
        return True


def _configure_logging(level: str) -> None:
    root = logging.getLogger("sublime_mcp")
    root.setLevel(level.upper())
    root.propagate = False
    if any(getattr(h, "_sublime_mcp_configured", False) for h in root.handlers):
        return
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler.addFilter(_ContextFilter())
    handler._sublime_mcp_configured = True  # type: ignore[attr-defined]
    root.addHandler(handler)


logger = logging.getLogger("sublime_mcp.harness")


# ---------------------------------------------------------------------
# Argument parsing


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_AGENT_NAME_SUB = re.compile(r"[^a-z0-9-]+")
CONTAINER_NAME_MAX = 253  # Docker container name limit.


def _sanitize_agent_name(name: str) -> str:
    cleaned = _AGENT_NAME_SUB.sub("-", name.lower())
    return cleaned.strip("-")


def _container_name(agent: str, session_id: str) -> str:
    name = "st-%s-%s" % (agent, session_id)
    if len(name) > CONTAINER_NAME_MAX:
        budget = CONTAINER_NAME_MAX - len("st--") - len(session_id)
        name = "st-%s-%s" % (agent[:max(budget, 1)], session_id)
    return name


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sublime-mcp",
        description="Build-and-exec shim for the dockerized sublime-mcp.",
    )
    parser.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="HOST:CONTAINER",
        help="Bind-mount HOST into the container at CONTAINER. Repeatable. "
             "Recommended: --mount $PWD:/work",
    )
    parser.add_argument(
        "--image-tag",
        default=None,
        help="Override the image tag. Default: derived from "
             "`git rev-parse HEAD` against the harness checkout as "
             "`sublime-mcp-harness:<sha12>`.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force `docker build` even if an image with the resolved "
             "tag already exists.",
    )
    parser.add_argument(
        "--license-file",
        metavar="PATH",
        help="Mount a Sublime Text license file into the container.",
    )
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=os.environ.get("SUBLIME_MCP_LOG_LEVEL", "INFO").upper(),
        help="Logging level for harness + bridge (also $SUBLIME_MCP_LOG_LEVEL). "
             "Default: %(default)s.",
    )
    parser.add_argument(
        "--agent-name",
        required=True,
        metavar="AGENT",
        help="Used to name the container as st-<agent>-<session-id>.",
    )
    parser.add_argument(
        "--session-id",
        required=True,
        metavar="UUID",
        help="Used to name the container.",
    )
    args = parser.parse_args(argv)
    if args.log_level not in LOG_LEVELS:
        parser.error("--log-level must be one of %s, got %r" % (LOG_LEVELS, args.log_level))
    for spec in args.mount:
        if ":" not in spec or spec.startswith(":") or spec.endswith(":"):
            parser.error("--mount expects HOST:CONTAINER, got %r" % spec)
    if args.license_file and not Path(args.license_file).is_file():
        parser.error("--license-file %r is not a regular file" % args.license_file)
    sanitized_agent = _sanitize_agent_name(args.agent_name)
    if not sanitized_agent:
        parser.error(
            "--agent-name %r contains no [a-z0-9-] characters after sanitization"
            % args.agent_name
        )
    args.agent_name = sanitized_agent
    if not _SESSION_ID_RE.match(args.session_id):
        parser.error(
            "--session-id %r must match [A-Za-z0-9-]{1,64}" % args.session_id
        )
    return args


# ---------------------------------------------------------------------
# Build context


def build_context_dir() -> Path:
    """Locate the Dockerfile + plugin.py + bridge.py + entrypoint.sh."""
    here = Path(__file__).resolve().parent
    required = ["Dockerfile", "plugin.py", "bridge.py", "docker/entrypoint.sh"]
    missing = [name for name in required if not (here / name).is_file()]
    if missing:
        raise RuntimeError(
            "build context incomplete next to %s: missing %s"
            % (here, ", ".join(missing))
        )
    return here


def stage_build_context(src: Path) -> Path:
    """Copy the build context into a temp dir for `docker build`."""
    dst = Path(tempfile.mkdtemp(prefix="sublime-mcp-harness-build-"))
    shutil.copy2(src / "Dockerfile", dst / "Dockerfile")
    shutil.copy2(src / "plugin.py", dst / "plugin.py")
    shutil.copy2(src / "bridge.py", dst / "bridge.py")
    (dst / "docker").mkdir()
    shutil.copy2(src / "docker" / "entrypoint.sh", dst / "docker" / "entrypoint.sh")
    return dst


# ---------------------------------------------------------------------
# Git-derived image tag


def derive_image_tag(src: Path) -> tuple[str, bool]:
    """Return `(tag, dirty)` for the harness checkout at `src`."""
    inside = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise RuntimeError(
            "harness source at %s is not a git repository — "
            "pass --image-tag to override or run from a clone of the "
            "sublime-mcp source repo" % src
        )
    status = subprocess.run(
        ["git", "-C", str(src), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    sha12 = head.stdout.strip()[:12]
    dirty = bool(status.stdout.strip())
    suffix = "-dirty" if dirty else ""
    return "%s:%s%s" % (IMAGE_REPO, sha12, suffix), dirty


# ---------------------------------------------------------------------
# Docker calls


def docker_available() -> tuple[bool, str]:
    try:
        subprocess.run(
            ["docker", "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, "`docker` not found on PATH"
    except subprocess.CalledProcessError:
        return False, "`docker --version` failed"
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        msg = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = msg[-1] if msg else "(no stderr)"
        return False, "docker daemon unreachable (%s)" % tail
    return True, ""


def image_exists(tag: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_image(tag: str, context: Path) -> None:
    logger.info("building image %s (this can take a few minutes on first run)…", tag)
    subprocess.run(
        [
            "docker", "build",
            "--label", "%s=%s" % (IMAGE_LABEL_KEY, tag),
            "-t", tag,
            str(context),
        ],
        check=True,
    )
    logger.info("image %s built", tag)


def ensure_image(tag: str, force: bool, context: Path) -> None:
    """Build the image with a host-side flock so concurrent first-runs serialise."""
    cache_dir = Path.home() / ".cache" / "sublime-mcp-harness"
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / "build.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if force or not image_exists(tag):
            staged = stage_build_context(context)
            try:
                build_image(tag, staged)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
        else:
            logger.info("reusing image %s", tag)


def build_run_argv(
    tag: str,
    mounts: list[str],
    license_file: str | None,
    log_level: str,
    *,
    name: str,
) -> list[str]:
    args = [
        "docker", "run",
        "-i",
        "--rm",
        "--name", name,
        "--label", "sublime-mcp-harness=%d" % os.getpid(),
        "-e", "SUBLIME_MCP_LOG_LEVEL=%s" % log_level,
    ]
    for spec in mounts:
        args += ["-v", spec]
    if license_file:
        args += [
            "-v",
            "%s:%s/License.sublime_license:ro"
            % (str(Path(license_file).resolve()), CONTAINER_LICENSE_DIR),
        ]
    args.append(tag)
    return args


# ---------------------------------------------------------------------
# Top-level


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    _configure_logging(args.log_level)

    ok, reason = docker_available()
    if not ok:
        logger.error(
            "docker unavailable: %s. Install Docker Desktop or the Docker Engine, "
            "ensure the daemon is running, and retry.",
            reason,
        )
        return 2

    try:
        ctx = build_context_dir()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    if args.image_tag is None:
        try:
            tag, dirty = derive_image_tag(ctx)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        logger.info("derived image tag %s from git HEAD", tag)
        if dirty:
            logger.warning(
                "harness source has uncommitted changes; building fresh "
                "from the work tree. The %s tag is not reproducible.",
                tag,
            )
    else:
        tag = args.image_tag
        dirty = False

    try:
        ensure_image(tag, args.rebuild or dirty, ctx)
    except subprocess.CalledProcessError as exc:
        logger.error("docker build failed (exit %d)", exc.returncode)
        return exc.returncode or 1

    container_name = _container_name(args.agent_name, args.session_id)
    run_argv = build_run_argv(
        tag, args.mount, args.license_file, args.log_level,
        name=container_name,
    )
    logger.info("exec docker run -i --rm name=%s tag=%s", container_name, tag)
    os.execvp(run_argv[0], run_argv)


if __name__ == "__main__":
    sys.exit(main())
