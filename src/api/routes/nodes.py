from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.analytics.network_mapper import NetworkMapper
from src.api.routes.reticulum import (
    _INBOX_JSON,
    _PEERS_JSON,
    get_recent_announces_map,
    read_contacts,
    read_peers_enrichment,
    resolve_display_name,
)
from src.models.node import Node
from src.storage.node_repository import NodeRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

_node_repo: NodeRepository | None = None
_network_mapper: NetworkMapper | None = None


def init_routes(
    node_repo: NodeRepository, network_mapper: NetworkMapper
) -> None:
    global _node_repo, _network_mapper
    _node_repo = node_repo
    _network_mapper = network_mapper


def _enrich_reticulum_nodes(nodes: list) -> list:
    """Layer display_name + peer_class onto any reticulum-protocol nodes.

    Phase 1 #1b: the Dashboard side-panel was rendering Reticulum
    nodes as raw 32-char hashes badged "MT". Cross-reference the
    sidecar's lxmf_peers.json so:
      * display_name comes through (Anonymous Peer / 4w4 / etc.)
      * peer_class is exposed for the frontend to render an "RNS"
        badge with class-specific colouring (lxmf / propagation /
        relay / transport)

    Non-Reticulum nodes are left untouched. Reticulum nodes whose
    hash isn't in the enrichment map yet (haven't been seen by the
    60s classifier tick) get display_name=null + peer_class=unknown
    rather than being dropped, so the panel still shows them.
    """
    enrich    = read_peers_enrichment()
    contacts  = read_contacts()
    announces = get_recent_announces_map()
    if not enrich and not contacts and not announces:
        return nodes
    for n in nodes:
        if (n.get("protocol") or "").lower() != "reticulum":
            continue
        node_id = n.get("node_id") or ""
        meta    = enrich.get(node_id, {})
        # The node repository pre-populates display_name with a
        # "!<hash>" placeholder when nothing better is available --
        # we treat that as no-name-yet and let the resolved name
        # (operator > classifier) win. For Reticulum the air protocol
        # is the fallback truth (display_name lives in announce
        # app_data); operator overrides from data/lxmf_contacts.json
        # win when set.
        existing = n.get("display_name") or ""
        is_placeholder = existing == f"!{node_id}" or not existing
        name, source = resolve_display_name(node_id, meta, contacts)
        if is_placeholder and name:
            n["display_name"] = name
        # Always expose the source + class so the frontend can render
        # the pencil indicator and class badge even when the
        # underlying display_name was already non-placeholder.
        n["display_name_source"] = source
        n["peer_class"]          = meta.get("class") or "unknown"
        n["is_lxmf"]             = bool(meta.get("is_lxmf"))
        # Phase 1 #3: route info (hops, via-relay, interface) from the
        # most recent announce. Lets the Dashboard side panel render
        # "2 hops via 58721f81..." next to RSSI without a second API
        # round-trip. None for nodes the journal hasn't seen in 24h.
        ann = announces.get(node_id) or {}
        n["hops"]           = ann.get("hops")
        n["via"]            = ann.get("via")
        n["rns_interface"]  = ann.get("interface")
    return nodes


async def _sync_lxmf_peers_to_nodes() -> None:
    """Upsert RNS peers from the sidecar's JSON files into the nodes table.

    On the rnodeusb branch the SX1302 no longer captures Reticulum
    packets, so RNS peers never get into the `nodes` table via the
    normal packet → decoder → upsert pipeline. Without this sync, the
    Dashboard Nodes panel shows only stale RNS entries (or none at
    all) even while LXMF messaging works fine via the RNode USB stick
    and the sidecar.

    Strategy: read lxmf_peers.json (display names, classes) and
    inbox.json (per-peer last-message timestamp), then upsert each
    peer into nodes. `last_heard` derives from the most recent inbox
    message for that peer; falls back to peers.json's `generated_at`
    if the peer has no inbox messages yet (e.g. seen via announce
    only). Idempotent — safe to call on every /api/nodes request.
    """
    if _node_repo is None:
        return
    # Pull peer classification (display name, peer_class) from sidecar.
    enrich = read_peers_enrichment()
    if not enrich:
        return
    # Pull per-peer last-message timestamp from inbox.json (best-effort).
    last_seen_by_peer: dict[str, datetime] = {}
    try:
        with _INBOX_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        for m in payload.get("messages", []):
            peer = m.get("peer_hash") or ""
            ts = m.get("timestamp")
            if not peer or ts is None:
                continue
            try:
                when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            except (TypeError, ValueError):
                continue
            cur = last_seen_by_peer.get(peer)
            if cur is None or when > cur:
                last_seen_by_peer[peer] = when
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("inbox.json read failed during peer sync: %s", exc)
    # Global fallback timestamp from peers.json header.
    fallback_ts = datetime.now(timezone.utc)
    try:
        with _PEERS_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        gen_at = payload.get("generated_at")
        if gen_at:
            fallback_ts = datetime.fromisoformat(gen_at)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    # Upsert each peer. Only fills display_name when the sidecar
    # classifier produced a real one (not just the raw hash placeholder).
    for peer_hash, meta in enrich.items():
        display_name = meta.get("display_name") or None
        if display_name and display_name.startswith("!"):
            display_name = None  # placeholder, leave long_name NULL
        last_heard = last_seen_by_peer.get(peer_hash, fallback_ts)
        try:
            await _node_repo.upsert(Node(
                node_id=peer_hash,
                long_name=display_name,
                protocol="reticulum",
                last_heard=last_heard,
                first_seen=last_heard,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("RNS peer upsert failed for %s: %s", peer_hash, exc)


@router.get("")
async def list_nodes(limit: int = 500, enrich: bool = True):
    # Sync LXMF peers before reading nodes so the Dashboard panel sees
    # current RNS activity. Cheap (handful of peers, single transaction)
    # and idempotent.
    await _sync_lxmf_peers_to_nodes()
    if enrich:
        nodes = await _node_repo.get_all_with_signal(limit)
    else:
        nodes = [n.to_dict() for n in await _node_repo.get_all(limit)]
    return _enrich_reticulum_nodes(nodes)


@router.get("/count")
async def node_count():
    count = await _node_repo.get_count()
    active = await _node_repo.get_active_count()
    return {"count": count, "active": active}


@router.get("/map")
async def map_data():
    return await _network_mapper.get_map_data()


@router.get("/summary")
async def network_summary():
    return await _network_mapper.get_network_summary()


@router.get("/{node_id}")
async def get_node(node_id: str):
    node = await _node_repo.get_by_id(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node.to_dict()
