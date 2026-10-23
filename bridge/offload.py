"""Command-line entry point for the Unsloth offloader (Phase 2).

Sends a bounded coding task to the local Unsloth model and prints the
normalized ``WorkerResult`` as JSON to stdout. The result's ``state`` field
and machine-readable fields follow the handoff.md task packet and response-
state contracts (sections 6 and 8).

Usage:
    python -m bridge.offload <task-packet-json-or-file>
    python -m bridge.offload --smoke

The task packet is a JSON document (inline string or file) matching the
``HandoffRequest`` shape from ``bridge/schemas.py``.

``--smoke`` is opt-in and requires a live Unsloth server plus an API key env
var (UNSLOTH_STUDIO_AUTH_TOKEN or UNSLOTH_API_KEY). It discovers the loaded
model via GET /v1/models, sends a trivial bounded prompt, and prints the
model metadata and the worker result as one JSON object.

Exit codes:
    0  worker returned state 'completed' (or smoke check passed)
    1  worker returned a non-completed state
    2  invalid task packet (bad JSON / schema mismatch) or usage error
    3  Unsloth error (server down, auth rejected, timeout, malformed response)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import load_config
from .errors import UnslothError
from .schemas import (
    HandoffRequest,
    LengthLevel,
    ResultState,
    RepositoryContext,
)
from .transport import UnslothApiTransport


EXIT_COMPLETED = 0
EXIT_WORKER_STATE = 1
EXIT_INVALID_PACKET = 2
EXIT_UNSLOTH = 3


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


def run_handoff(transport, raw_packet: str) -> int:
    """Validate the packet, send it, print the result. Returns an exit code."""
    request = HandoffRequest.model_validate(json.loads(raw_packet))
    result = transport.send(request)
    print(json.dumps(result.model_dump(), indent=2))
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
    args = parser.parse_args(argv)
    if args.smoke and args.packet is not None:
        parser.error("--smoke must not be combined with a task packet")
    if not args.smoke and args.packet is None:
        parser.error("a task packet (JSON string or file path) is required")

    config = load_config()
    transport = UnslothApiTransport(config)
    try:
        if args.smoke:
            code = run_smoke(transport)
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
