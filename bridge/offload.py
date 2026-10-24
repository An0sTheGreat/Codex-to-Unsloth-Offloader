"""Command-line entry point for the Unsloth offloader.

Sends a bounded coding task to the local Unsloth model, runs the patch scope
validation gate (Phase 7), and prints the normalized ``WorkerResult`` (with a
``validation`` key) as JSON to stdout. The result's ``state`` field and the
machine-readable validation fields follow the handoff.md task packet and
response-state contracts (sections 6 and 8). A ``needs_context`` result is
retried once with the requested context (handoff.md section 10) before the
gate runs.

Usage:
    python -m bridge.offload <task-packet-json-or-file>
    python -m bridge.offload --task "Add a helper" [--scope scope.json]
    python -m bridge.offload --task "Add a helper" --dry-run
    python -m bridge.offload --smoke

The task packet is a JSON document (inline string or file) matching the
``HandoffRequest`` shape from ``bridge/schemas.py``.

``--smoke`` is opt-in and requires a live Unsloth server plus an API key env
var (UNSLOTH_STUDIO_AUTH_TOKEN or UNSLOTH_API_KEY). It discovers the loaded
model via GET /v1/models, sends a trivial bounded prompt, and prints the
model metadata and the worker result as one JSON object.

Exit codes:
    0  worker returned state 'completed' and the patch passed scope validation
    1  worker returned a non-completed state
    2  invalid task packet (bad JSON / schema mismatch) or usage error
    3  Unsloth error (server down, auth rejected, timeout, malformed response)
    4  patch failed the scope validation gate (reject / escalate to Codex)
    5  task kept in Codex (ineligible, or Unsloth unavailable)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import load_config
from .diagnostics import configure_logging, log_handoff
from .errors import UnslothError
from .schemas import (
    FinalState,
    HandoffRequest,
    LengthLevel,
    ResultState,
    RepositoryContext,
    ValidationOutcomeKind,
)
from .transport import UnslothApiTransport
from .validate import PatchValidator
from .verification import verify
from .orchestrator import Orchestrator, send_with_context_retry

EXIT_COMPLETED = 0
EXIT_WORKER_STATE = 1
EXIT_INVALID_PACKET = 2
EXIT_UNSLOTH = 3
EXIT_VALIDATION_FAILED = 4
EXIT_KEEP_IN_CODEX = 5


def _smoke_request() -> HandoffRequest:
    """The trivial bounded prompt used by --smoke."""
    return HandoffRequest(
        task=("Smoke test: confirm the Unsloth offloader transport works "
              "end-to-end."),
        objective=("Return a single JSON object with state 'completed' and a "
                   "non-empty summary. No code changes required."),
        difficulty=1,
        length=LengthLevel.SHORT,
        repository_context=RepositoryContext(
            summary="Smoke test only; no repository context is supplied."),
        constraints=[
            "Return a single JSON object with a 'state' field.",
            "Do not modify any files.",
        ],
        acceptance_criteria=[
            "state is 'completed'",
            "summary is non-empty",
        ],
    )


def run_handoff(transport, raw_packet: str, *, max_context_retries: int = 1) -> int:
    """Validate the packet, send it, gate and verify the patch, print the result.

    On a ``needs_context`` result the requested context is appended to the
    request and the task is sent again, up to ``max_context_retries`` times
    (handoff.md section 10); it never loops.

    A ``completed`` result must pass two independent checks before it is
    reported as success (handoff.md section 9):
      1. the Phase 7 patch scope gate (:class:`PatchValidator`);
      2. the Phase 8 independent verification (:func:`verification.verify`),
         which applies the candidate to an isolated review worktree and runs
         the project's own validation. The local worker's self-reported
         ``tests_run`` is never trusted.

    Returns an exit code.
    """
    request = HandoffRequest.model_validate(json.loads(raw_packet))
    # Bounded needs-context retry (section 10): add only the requested context.
    result = send_with_context_retry(transport, request, max_context_retries)

    payload = result.model_dump()
    validation = None
    verification = None
    if result.state is ResultState.COMPLETED:
        # Phase 7: patch scope gate (must remain first; never bypassed).
        validation = PatchValidator().validate(result, request)
        payload["validation"] = validation.model_dump()
        # Phase 8: independent apply + verify (only for a scope-valid patch).
        if validation.valid:
            verification = verify(result, request)
            payload["verification"] = verification.model_dump()

    print(json.dumps(payload, indent=2))
    log_handoff(request, result, validation, verification)

    # A scope-valid candidate that fails independent verification is a failure,
    # never a success (handoff.md section 9: validation failure must never be
    # reported as success).
    if verification is not None and verification.outcome is not ValidationOutcomeKind.ACCEPTED_UNCHANGED:
        return EXIT_VALIDATION_FAILED
    if validation is not None and not validation.valid:
        return EXIT_VALIDATION_FAILED
    return EXIT_COMPLETED if result.state is ResultState.COMPLETED else EXIT_WORKER_STATE


def run_smoke(transport) -> int:
    """Live smoke test: discover the model, send the trivial prompt, print both."""
    model = transport.discover_loaded_model()
    result = transport.send(_smoke_request())
    print(json.dumps({
        "model": model.model_dump(),
        "worker_result": result.model_dump(),
    }, indent=2))
    return EXIT_COMPLETED if result.state is ResultState.COMPLETED else EXIT_WORKER_STATE


def _orchestration_exit(orchestrator, task, *, scope=None, project_root=None,
                       dry_run=None) -> int:
    """Run the Phase 9 orchestration for ``task`` and map the result to an
    exit code.

    COMPLETED / PARTIAL -> EXIT_COMPLETED
    KEEP_IN_CODEX       -> EXIT_KEEP_IN_CODEX
    FAILED              -> EXIT_VALIDATION_FAILED
    CANCELLED           -> EXIT_WORKER_STATE
    """
    result = orchestrator.offload(
        task, scope=scope, project_root=project_root, dry_run=dry_run)
    code = {
        FinalState.COMPLETED: EXIT_COMPLETED,
        FinalState.PARTIAL: EXIT_COMPLETED,
        FinalState.KEEP_IN_CODEX: EXIT_KEEP_IN_CODEX,
        FinalState.FAILED: EXIT_VALIDATION_FAILED,
        FinalState.CANCELLED: EXIT_WORKER_STATE,
    }[result.final_state]
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="offload",
        description=("Send a bounded coding task to the local Unsloth model "
                     "and print the normalized worker result as JSON."),
    )
    parser.add_argument(
        "packet",
        nargs="?",
        help=("Task packet: JSON string, or path to a JSON file matching the "
              "HandoffRequest shape. Required unless --smoke is given."),
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=("Opt-in live smoke test: requires a running Unsloth server and an "
              "API key env var. Prints the loaded model metadata and a trivial "
              "worker result."),
    )
    parser.add_argument(
        "--task",
        help=("Free-text coding task description. Runs the full Phase 9 "
              "orchestration: assess -> route -> send -> validate -> verify -> "
              "fallback."),
    )
    parser.add_argument(
        "--scope",
        help=("Path to a JSON task-scope file for context packaging "
              "(allowed_files, read_only_files, snippets, summary, "
              "acceptance_criteria)."),
    )
    parser.add_argument(
        "--project-root",
        help="Project root for scope packaging.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the routing decision and what would be offloaded, without "
             "calling the local model.",
    )
    args = parser.parse_args(argv)
    if args.smoke and args.packet is not None:
        parser.error("--smoke must not be combined with a task packet")
    if args.smoke and args.task is not None:
        parser.error("--smoke must not be combined with --task")
    if not args.smoke and not args.task and args.packet is None:
        parser.error("a task packet, --task, or --smoke is required")

    config = load_config()
    configure_logging(config.log_level)
    transport = UnslothApiTransport(config)
    try:
        if args.smoke:
            code = run_smoke(transport)
        elif args.task is not None:
            scope = None
            if args.scope:
                with open(args.scope, encoding="utf-8") as f:
                    scope = json.loads(f.read())
            code = _orchestration_exit(
                Orchestrator(config, transport), args.task,
                scope=scope, project_root=args.project_root,
                dry_run=args.dry_run or None)
        else:
            raw = args.packet
            if os.path.isfile(raw):
                with open(raw, encoding="utf-8") as f:
                    raw = f.read()
            code = run_handoff(transport, raw)
        return code
    except UnslothError as exc:
        print(json.dumps(exc.to_dict()), file=sys.stderr)
        return EXIT_UNSLOTH
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({
            "code": "invalid_request",
            "message": "task packet is not valid JSON or does not match HandoffRequest",
        }), file=sys.stderr)
        return EXIT_INVALID_PACKET
    finally:
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
