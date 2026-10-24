"""Independent apply + verification of local-model candidate patches (Phase 8).

Phase 7's :class:`PatchValidator` is a pure *scope/boundary* check on the diff
string the local worker returned. Phase 8 adds the *independent* verification
step required by handoff.md section 9: the candidate patch is applied to an
isolated temporary Git worktree (derived from the current repository state),
and the project's own validation commands are run against that isolated state.

The local worker's self-reported ``tests_run`` / "tests pass" is **never**
trusted; only the results of commands executed by this module count. A patch
that fails any stage is reported as ``REJECTED`` and can never surface as a
successful offload.

This module is deliberately separate from :mod:`bridge.validate` so the pure
patch parser stays pure. It owns:

* resolving the repository root (dynamically, via Git; no hard-coded paths)
* creating a temporary isolated review worktree (and cleaning it up)
* ``git apply --check`` preflight
* applying the candidate patch inside the worktree only
* running the relevant validation commands (unit tests, plus lint / type-check
  when genuinely configured by the project)
* detecting unexpected tracked modifications introduced by the validation run
* constructing the :class:`~bridge.schemas.ValidationOutcome`

No live Unsloth instance is required to import this module. Tests use
throwaway Git repositories and ``MockTransport``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

import tomllib

from .config import get_project_root
from .schemas import ValidationOutcome, ValidationOutcomeKind
from .validate import PatchValidator

logger = logging.getLogger(__name__)

# Default per-command timeout (seconds) for validation runs (Phase 8 §11).
_DEFAULT_TIMEOUT = 300.0

# Paths that validation may create but which are not meaningful source changes.
_IGNorable = ("__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache")


def _is_ignorable(path: str) -> bool:
    return any(part in path for part in _IGNorable)


def _configured_commands(root: Path) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """Return ``(lint_cmd, type_check_cmd)``.

    A command is returned only when the project *genuinely configures* it
    (a config file exists). A tool that is merely installed globally, with no
    project configuration, is never invoked (Phase 8 §4). Returns ``None`` for
    any command that is not configured.
    """
    lint_cmd: Optional[List[str]] = None
    type_check_cmd: Optional[List[str]] = None
    tool: dict = {}
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.load(pyproject.open("rb"))
            tool = data.get("tool", {})
        except (tomllib.TOMLDecodeError, OSError):
            tool = {}

    if (root / "ruff.toml").exists() or "ruff" in tool:
        lint_cmd = ["ruff", "check"]
    elif (root / ".flake8").exists() or "flake8" in tool:
        lint_cmd = ["flake8"]
    else:
        setup_cfg = root / "setup.cfg"
        if setup_cfg.exists():
            text = setup_cfg.read_text(errors="replace")
            if re.search(r"^\[flake8\]", text, re.M):
                lint_cmd = ["flake8"]
            if re.search(r"^\[mypy\]", text, re.M):
                type_check_cmd = ["mypy"]

    if (root / "mypy.ini").exists() or "mypy" in tool:
        type_check_cmd = ["mypy"]
    elif (root / "pyrightconfig.json").exists():
        type_check_cmd = ["pyright"]

    return lint_cmd, type_check_cmd


def _resolve_repo_root(repo_root: Optional[object]) -> Optional[Path]:
    """Resolve the Git repository toplevel, or ``None`` if unavailable.

    ``repo_root`` may be a Path/string (used as-is, for tests) or ``None`` to
    default to the project root (parent of the ``bridge`` package). The result
    is verified to be a real Git repository via ``git rev-parse``.
    """
    base = Path(repo_root).resolve() if repo_root is not None else get_project_root()
    if not base.is_dir():
        return None
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=base, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    top = Path(p.stdout.strip())
    return top if top.is_dir() else None


def _run(cmd: Sequence[str], cwd: Path,
         inp: Optional[str] = None,
         timeout: float = _DEFAULT_TIMEOUT) -> tuple[bool, str, int]:
    """Run ``cmd``; return ``(ok, stderr, returncode)``.

    ``ok`` is ``returncode == 0``. Startup errors (e.g. a missing executable)
    and timeouts return ``ok=False`` with a concise reason in ``stderr`` and
    ``returncode=-1``. Never raises for these conditions.
    """
    try:
        p = subprocess.run(
            list(cmd), cwd=cwd, input=inp, capture_output=True,
            text=True, timeout=timeout,
        )
        return p.returncode == 0, p.stderr.strip(), p.returncode
    except subprocess.TimeoutExpired:
        return False, "timed out", -1
    except OSError as exc:
        return False, str(exc), -1


def _changed_paths(status: str) -> List[str]:
    """Parse ``git status --porcelain`` output into a list of relative paths,
    ignoring ignored caches. Renames are reported as their new path."""
    paths: List[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        # Porcelain format: two status chars, a space, then the path.
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip()
        if _is_ignorable(path):
            continue
        paths.append(path)
    return paths


def verify(worker_result, request, repo_root=None,
           *, timeout: float = _DEFAULT_TIMEOUT) -> ValidationOutcome:
    """Independently apply ``worker_result.diff`` to an isolated review worktree
    and run the project's validation. Returns a :class:`ValidationOutcome`.

    ``outcome`` is ``ACCEPTED_UNCHANGED`` only when the patch applies cleanly
    and every validation command passes with no unexpected tracked changes.
    Any other condition yields ``REJECTED`` with a concise ``notes`` string.
    A scope-invalid patch must not be passed to this function (the Phase 7
    :class:`PatchValidator` gate runs first).

    Parameters
    ----------
    worker_result: The local model's result; ``diff`` is the candidate patch.
    request: The handoff packet (its ``repository_context`` supplies the
        allowed/read-only scope used to derive the expected changed paths).
    repo_root: The Git repository to base the review worktree on. Defaults to
        the project root (parent of ``bridge``) resolved via Git; pass a path
        to override (used by tests).
    timeout: Per-command timeout in seconds for validation runs.
    """
    root = _resolve_repo_root(repo_root)
    if root is None:
        return ValidationOutcome(
            outcome=ValidationOutcomeKind.REJECTED,
            notes="Verification environment error: could not resolve a Git "
                  "repository (git unavailable or not inside a repository).",
        )

    rc = request.repository_context
    scope = PatchValidator().validate_diff(
        worker_result.diff, rc.allowed_files, rc.read_only_files,
    )
    expected = set(scope.files_changed)

    applied = False
    tests_passed = False
    lint_run = lint_passed = False
    type_checks_run = type_checks_passed = False
    unexpected: List[str] = []
    notes: List[str] = []

    with tempfile.TemporaryDirectory(prefix="offload-verify-") as base:
        worktree = Path(base) / "worktree"
        worktree_created = False
        try:
            # -- create an isolated review worktree (never the primary tree) --
            ok, _, err = _run(["git", "worktree", "add", str(worktree), "HEAD"], root)
            if not ok:
                return ValidationOutcome(
                    outcome=ValidationOutcomeKind.REJECTED,
                    notes="Verification environment error: failed to create an "
                          "isolated review worktree."
                          + (f" ({err[:200]})" if err else ""),
                )
            worktree_created = True
            notes.append("created isolated review worktree")

            # -- preflight: can the patch apply? --
            pre_ok, pre_err, _ = _run(
                ["git", "apply", "--check", "--allow-empty"],
                worktree, inp=worker_result.diff or "", timeout=timeout,
            )
            if not pre_ok:
                return ValidationOutcome(
                    outcome=ValidationOutcomeKind.REJECTED,
                    files_changed=sorted(expected),
                    applied=False,
                    notes="PATCH REJECTED (preflight): "
                          + (f"{pre_err[:200]}." if pre_err else "does not apply"),
                )

            # -- apply the candidate inside the worktree only --
            ok, apply_err, _ = _run(
                ["git", "apply", "--allow-empty"],
                worktree, inp=worker_result.diff or "", timeout=timeout,
            )
            if not ok:
                return ValidationOutcome(
                    outcome=ValidationOutcomeKind.REJECTED,
                    files_changed=sorted(expected),
                    applied=False,
                    notes="PATCH REJECTED (apply): "
                          + (f"{apply_err[:200]}." if apply_err else "failed to apply"),
                )
            applied = True
            notes.append("applied candidate to isolated worktree")

            # -- run the project's unit test suite against the candidate --
            ok, _, _ = _run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                worktree, timeout=timeout,
            )
            tests_passed = ok
            if not tests_passed:
                notes.append("unit tests FAILED on candidate")
                # Bounded baseline comparison (Phase 8 section 5): when the
                # candidate's tests fail, run the unmodified repo's tests in a
                # second throwaway worktree so the failure can be attributed
                # as pre-existing vs candidate-introduced. Never run when the
                # candidate already passes (no useful information).
                baseline_passed = None
                base_wt = Path(base) / "baseline"
                try:
                    ok, _, _ = _run(
                        ["git", "worktree", "add", str(base_wt), "HEAD"], root)
                    if ok:
                        ok, _, _ = _run(
                            [sys.executable, "-m", "unittest", "discover",
                             "-s", "tests"], base_wt, timeout=timeout)
                        baseline_passed = ok
                finally:
                    if base_wt.exists():
                        _run(["git", "worktree", "remove", "--force",
                               str(base_wt)], root)
                if baseline_passed is True:
                    notes.append("baseline passes; failure is candidate-introduced")
                elif baseline_passed is False:
                    notes.append("baseline also fails; failure may be pre-existing")

            # -- lint / type-check, only when genuinely configured --
            lint_cmd, type_check_cmd = _configured_commands(worktree)
            if lint_cmd is not None:
                ok, _, _ = _run(lint_cmd, worktree, timeout=timeout)
                lint_run = True
                lint_passed = ok
                if not ok:
                    notes.append("lint FAILED on candidate")
            if type_check_cmd is not None:
                ok, _, _ = _run(type_check_cmd, worktree, timeout=timeout)
                type_checks_run = True
                type_checks_passed = ok
                if not ok:
                    notes.append("type checks FAILED on candidate")

            # -- detect unexpected tracked modifications from the validation run --
            status_p = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=worktree, capture_output=True, text=True, timeout=30,
            )
            if status_p.returncode == 0:
                for path in _changed_paths(status_p.stdout):
                    if path not in expected:
                        unexpected.append(path)
            if unexpected:
                notes.append("unexpected tracked changes: " + ", ".join(sorted(unexpected)))

            # -- final disposition --
            if applied and tests_passed and not unexpected:
                outcome = ValidationOutcomeKind.ACCEPTED_UNCHANGED
                notes.append("candidate verified: patch applied, tests passed, "
                             "no unexpected changes")
            else:
                outcome = ValidationOutcomeKind.REJECTED
                if not tests_passed:
                    notes.append("candidate rejected: validation failed")

            return ValidationOutcome(
                outcome=outcome,
                files_changed=sorted(expected),
                unexpected_changes=sorted(unexpected),
                tests_passed=tests_passed,
                lint_passed=lint_passed,
                lint_run=lint_run,
                type_checks_passed=type_checks_passed,
                type_checks_run=type_checks_run,
                applied=applied,
                notes="; ".join(notes) if notes else "",
            )
        finally:
            # -- reliable cleanup; must not mask the result above --
            if worktree_created:
                ok, _, err = _run(
                    ["git", "worktree", "remove", "--force", str(worktree)], root,
                )
                if not ok:
                    logger.warning(
                        "could not remove review worktree %s (%s); falling back "
                        "to directory removal", worktree, (err or "unknown")[:200],
                    )
                    shutil.rmtree(str(worktree), ignore_errors=True)


__all__ = ["verify"]
