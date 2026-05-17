from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.analytics.network_mapper import NetworkMapper
from src.api.routes.reticulum import read_peers_enrichment
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
    enrich = read_peers_enrichment()
    if not enrich:
        return nodes
    for n in nodes:
        if (n.get("protocol") or "").lower() != "reticulum":
            continue
        meta = enrich.get(n.get("node_id") or "", {})
        # Don't clobber an existing display_name (e.g. one already
        # set from a future operator-edited address book).
        if not n.get("display_name") and meta.get("display_name"):
            n["display_name"] = meta["display_name"]
        n["peer_class"] = meta.get("class") or "unknown"
        n["is_lxmf"]    = bool(meta.get("is_lxmf"))
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
