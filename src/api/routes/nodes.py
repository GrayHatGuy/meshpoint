from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.analytics.network_mapper import NetworkMapper
from src.api.routes.reticulum import (
    _INBOX_JSON,
    _PEERS_JSON,
    _parse_recent_announces,
    get_recent_announces_map,
    read_contacts,
    read_peers_enrichment,
    resolve_display_name,
)
from src.models.node import Node
from src.models.packet import Packet, PacketType, Protocol
from src.models.signal import SignalMetrics
from src.storage.node_repository import NodeRepository
from src.storage.packet_repository import PacketRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

_node_repo: NodeRepository | None = None
_network_mapper: NetworkMapper | None = None
_packet_repo: PacketRepository | None = None

# Dedup state for synthetic RNS packet injection — populated on first
# /api/nodes call after restart; bounded by inbox/journal contents.
_seen_lxmf_msg_keys: set[str] = set()
_seen_rns_announce_keys: set[str] = set()


def init_routes(
    node_repo: NodeRepository,
    network_mapper: NetworkMapper,
    packet_repo: PacketRepository | None = None,
) -> None:
    global _node_repo, _network_mapper, _packet_repo
    _node_repo = node_repo
    _network_mapper = network_mapper
    _packet_repo = packet_repo


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


async def _sync_lxmf_to_packets() -> None:
    """Inject LXMF inbox messages and RNS announces into the packets table.

    On rnodeusb the SX1302 doesn't capture Reticulum traffic, so the
    Packets feed on the Dashboard is MT/MC-only by default. This
    function bridges two RNS-visible information sources --
        * inbox.json     -- LXMF deliveries seen by the sidecar
        * rnsd journal   -- "Valid announce for <hash>" lines with hops,
                            via-relay, RSSI, SNR
    -- into packet rows so they surface in the Packets feed alongside
    MT/MC. Dedup is per-process: each (msg_hash) and (ann_hash, ts) key
    is inserted at most once across the lifetime of the service.

    Caveats:
      * Only LXMF deliveries and RNS announces are captured. Link-layer
        traffic (DATA, LINK_REQUEST, PROOF) isn't logged at journal level
        unless rnsd is set to a verbose log level we don't otherwise
        rely on -- those packet types remain invisible.
      * On first call after a restart, every message/announce in the
        rolling window will be (re-)inserted. That's a one-time backfill
        per boot; subsequent calls only add genuinely new entries.
    """
    if _packet_repo is None:
        return

    # ── LXMF messages from inbox.json ────────────────────────────────
    try:
        with _INBOX_JSON.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        for m in payload.get("messages", []):
            msg_hash = m.get("hash") or m.get("packet_hash") or ""
            if not msg_hash or msg_hash in _seen_lxmf_msg_keys:
                continue
            _seen_lxmf_msg_keys.add(msg_hash)

            peer = m.get("peer_hash") or "unknown"
            ts_unix = m.get("timestamp")
            try:
                ts = datetime.fromtimestamp(float(ts_unix), tz=timezone.utc)
            except (TypeError, ValueError):
                ts = datetime.now(timezone.utc)

            direction = (m.get("direction") or "in").lower()
            # For inbound: peer is source, we are dest. For outbound:
            # reversed. We don't have our own LXMF address handy here,
            # so use "self" as a stable placeholder.
            if direction in ("in", "received"):
                source_id, dest_id = peer, "self"
            else:
                source_id, dest_id = "self", peer

            try:
                await _packet_repo.insert(Packet(
                    packet_id=msg_hash[:16],
                    source_id=source_id,
                    destination_id=dest_id,
                    protocol=Protocol.RETICULUM,
                    packet_type=PacketType.TEXT,
                    timestamp=ts,
                    capture_source="lxmf_sidecar",
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("LXMF -> packets insert failed: %s", exc)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("inbox.json sync skipped: %s", exc)

    # ── RNS announces from the rnsd journal ──────────────────────────
    for ann in _parse_recent_announces(limit=200):
        ann_hash = ann.get("hash") or ""
        ann_ts   = ann.get("last_heard") or ""
        if not ann_hash or not ann_ts:
            continue
        key = f"{ann_hash}:{ann_ts}"
        if key in _seen_rns_announce_keys:
            continue
        _seen_rns_announce_keys.add(key)

        try:
            ts = datetime.fromisoformat(ann_ts)
        except (ValueError, TypeError):
            ts = datetime.now(timezone.utc)

        signal = None
        if ann.get("rssi") is not None or ann.get("snr") is not None:
            signal = SignalMetrics(
                rssi=ann.get("rssi"),
                snr=ann.get("snr"),
            )

        try:
            await _packet_repo.insert(Packet(
                packet_id=hashlib.sha256(key.encode()).hexdigest()[:16],
                source_id=ann_hash,
                destination_id=ann_hash,  # announces are self-addressed
                protocol=Protocol.RETICULUM,
                packet_type=PacketType.NODEINFO,
                hop_limit=int(ann.get("hops") or 0),
                timestamp=ts,
                signal=signal,
                capture_source="rnsd_announce",
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("RNS announce -> packets insert failed: %s", exc)


@router.get("")
async def list_nodes(limit: int = 500, enrich: bool = True):
    # Sync LXMF peers + announces into the DB before reading nodes so
    # the Dashboard panels (Nodes + Packets) see current RNS activity.
    # Cheap; idempotent thanks to dedup sets.
    await _sync_lxmf_peers_to_nodes()
    await _sync_lxmf_to_packets()
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
