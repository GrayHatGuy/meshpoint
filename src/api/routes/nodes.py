from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.analytics.network_mapper import NetworkMapper
from src.api.routes.reticulum import (
    get_recent_announces_map,
    read_contacts,
    read_peers_enrichment,
    resolve_display_name,
)
from src.storage.node_repository import NodeRepository

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


@router.get("")
async def list_nodes(limit: int = 500, enrich: bool = True):
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
