#!/usr/bin/env python3
"""Minimal LXMF send helper.

Runs as the user that owns the lxmd identity (typically `mp`),
invoked by Meshpoint's POST /api/reticulum/send endpoint via a
narrow sudoers rule.

Why this script exists: the upstream `lxmf` pip package ships only
the `lxmd` daemon, NOT a standalone send CLI. MeshChat/Sideband
each implement their own sender using the LXMF Python API. Rather
than pin Meshpoint to MeshChat (which would defeat the whole
"replace MeshChat" goal of Phase 2 #3) or import LXMF inside the
Meshpoint venv (which would defeat the decoupling rule we set in
#1+#2), we ship this tiny shim and run it via sudo -u mp.

Usage:
    lxmf_send.py <dest_hash_hex> <content> [--title TITLE] [--timeout SECS]

Exit codes:
    0  -- message handed to LXMRouter, state reached SENT or DELIVERED
    1  -- argument / preflight error (bad hash, missing identity)
    2  -- no path to destination after path-request timeout
    3  -- LXMRouter handed off but state never reached SENT in timeout
    4  -- unexpected exception during send (see stderr)

On success, the message id (hex) is printed to stdout so the caller
can log it. All diagnostics go to stderr.

Design choices worth knowing:
  * We use a SEPARATE storage path (/tmp/meshpoint-lxmf-sender) so
    we never touch lxmd's storage and risk a two-router conflict on
    the same identity. The identity is loaded from lxmd's storage
    in read-only mode -- peers still see messages from our real
    lxmf address.
  * We auto-attach to the running rnsd via its shared-instance
    socket (RNS.Reticulum() with no configdir uses ~/.reticulum,
    which already has share_instance = Yes from our template). No
    second radio is opened.
  * We block on send-completion up to --timeout (default 25s) and
    exit nonzero if the message never moves past OUTBOUND. The
    FastAPI handler that invokes us has a 30s subprocess timeout
    of its own, so 25s here keeps a safety margin.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# All the heavy lifting depends on RNS + LXMF being importable in
# THIS user's environment (the mp user's pip --user install). If
# they're not, we want a clean error, not a stack trace.
try:
    import RNS  # type: ignore
    import LXMF  # type: ignore
except ImportError as exc:
    sys.stderr.write(
        f"ERROR: RNS/LXMF not importable in this environment: {exc}\n"
        "Run: pip install --user --upgrade --break-system-packages rns lxmf\n"
    )
    sys.exit(1)


def _eprint(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Send a single LXMF message")
    ap.add_argument("destination_hash", help="32-char lowercase hex")
    ap.add_argument("content", help="Message body (UTF-8)")
    ap.add_argument("--title", default="", help="Optional subject line")
    ap.add_argument(
        "--timeout", type=float, default=25.0,
        help="Seconds to wait for SENT state (default: 25)",
    )
    args = ap.parse_args()

    dest_hex = args.destination_hash.strip().lower()
    if len(dest_hex) != 32 or any(c not in "0123456789abcdef" for c in dest_hex):
        _eprint("ERROR: destination_hash must be 32 lowercase hex characters")
        return 1
    try:
        dest_hash = bytes.fromhex(dest_hex)
    except ValueError:
        _eprint("ERROR: destination_hash is not valid hex")
        return 1

    # ── Load lxmd's identity (read-only) ─────────────────────────
    # lxmd keeps its identity at ~/.lxmd/identity (NOT under storage/,
    # despite the rest of its state living there). We MUST use this
    # exact file so peers see messages coming from our real lxmf
    # address (the one shown in /api/reticulum/identity).
    identity_path = os.path.expanduser("~/.lxmd/identity")
    if not os.path.isfile(identity_path):
        _eprint(f"ERROR: lxmd identity not found at {identity_path}")
        return 1
    try:
        identity = RNS.Identity.from_file(identity_path)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: could not load lxmd identity: {exc}")
        return 1

    # ── Attach to the running rnsd via shared-instance socket ────
    # No configdir => default ~/.reticulum/, which our template
    # marks share_instance = Yes. RNS auto-detects the running
    # rnsd's local socket and piggybacks on its radio.
    try:
        RNS.Reticulum(loglevel=0)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: could not connect to RNS: {exc}")
        return 1

    # ── Resolve the destination identity ────────────────────────
    # We must have heard an announce for this hash (directly or
    # via a neighbor) for RNS.Identity.recall to return anything.
    # If we haven't, request a path and wait briefly.
    if not RNS.Transport.has_path(dest_hash):
        _eprint(f"INFO: no path to {dest_hex}, requesting...")
        RNS.Transport.request_path(dest_hash)
        deadline = time.time() + 10.0
        while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
            time.sleep(0.2)

    dest_identity = RNS.Identity.recall(dest_hash)
    if dest_identity is None:
        _eprint(
            f"ERROR: no identity known for {dest_hex}. The destination "
            "must have announced recently (within ~24h) or we cannot "
            "build a delivery destination."
        )
        return 2

    destination = RNS.Destination(
        dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery",
    )
    source = RNS.Destination(
        identity, RNS.Destination.IN, RNS.Destination.SINGLE,
        "lxmf", "delivery",
    )

    # ── Spin up an ephemeral LXMRouter for the send ─────────────
    # Separate storage path from lxmd so we don't collide. lxmd
    # remains the canonical "this is your inbox" router; this one
    # exists for ~5s, sends one message, exits. Cleanup happens at
    # process exit (tmpfs reclaims).
    sender_storage = os.environ.get(
        "MESHPOINT_LXMF_SENDER_STORAGE", "/tmp/meshpoint-lxmf-sender",
    )
    os.makedirs(sender_storage, exist_ok=True)
    try:
        router = LXMF.LXMRouter(identity=identity, storagepath=sender_storage)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: could not start LXMRouter: {exc}")
        return 4

    # Build the message. desired_method=DIRECT means "establish a
    # direct link to the destination and deliver in-band" -- the
    # most reliable mode for an interactive send. PROPAGATED would
    # drop the message at a propagation node for the recipient to
    # fetch later, which we don't expose in this MVP.
    lxm = LXMF.LXMessage(
        destination=destination,
        source=source,
        content=args.content.encode("utf-8"),
        title=args.title.encode("utf-8") if args.title else b"",
        desired_method=LXMF.LXMessage.DIRECT,
    )

    try:
        router.handle_outbound(lxm)
    except Exception as exc:  # noqa: BLE001
        _eprint(f"ERROR: handle_outbound raised: {exc}")
        return 4

    # ── Wait for state transition ───────────────────────────────
    # LXMessage progresses OUTBOUND -> SENDING -> SENT -> DELIVERED
    # (or FAILED). We consider SENT good enough -- delivery receipts
    # require the recipient online and would add another 10s of
    # waiting that's wasted on offline recipients.
    success_states = {
        getattr(LXMF.LXMessage, "SENT", -1),
        getattr(LXMF.LXMessage, "DELIVERED", -2),
    }
    fail_states = {
        getattr(LXMF.LXMessage, "FAILED", -3),
        getattr(LXMF.LXMessage, "REJECTED", -4),
    }

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if lxm.state in success_states:
            print(lxm.hash.hex() if isinstance(lxm.hash, (bytes, bytearray)) else "")
            return 0
        if lxm.state in fail_states:
            _eprint(f"ERROR: send failed (state={lxm.state})")
            return 3
        time.sleep(0.25)

    _eprint(
        f"ERROR: send timed out after {args.timeout}s "
        f"(final state={lxm.state}; OUTBOUND={getattr(LXMF.LXMessage, 'OUTBOUND', '?')})"
    )
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
