"""Build the geoi content folder tree from flat hub listings.

Pure Python (no QGIS, no Qt) so the tree shape — folder nesting, owner
filtering, category grouping and item placement — is unit-testable without a
QGIS install. The browser panel renders whatever this returns.

Node shapes:

  folder    {"kind": "folder", "id", "title", "parentId", "children": [...]}
  category  {"kind": "category", "category": "feature"|"tile"|"tiles3d",
             "title", "children": [...]}
  service   {"kind": "service", "payload": <service entry>}
  project   {"kind": "project", "payload": <project summary>}
  tile      {"kind": "tile", "payload": <tile-service summary>}
  tiles3d   {"kind": "tiles3d", "payload": <3D-Tiles-service summary>}
  shared    {"kind": "shared", "title": "Shared with me", "children": [...]}

A leaf node carries ``"shared": True`` when it belongs to the "Shared with me"
section — content the signed-in user may CONSUME (added to the map) but does
NOT own, so the browser panel shows it read-only (no rename / move / delete).

The tree is STRUCTURED into clear, symmetrical categories: at every level
(the root AND inside each folder) the content is grouped under labelled
category buckets — **Feature Services** (feature services), **Web Maps**
(saved geoi projects), **Tile Services** (raster tile services) and
**3D Tiles Services** (OGC 3D Tiles) — so the kinds of content are always
cleanly separated and the structure looks the same everywhere.

A level is ordered folders-first (by title), then **Feature Services**, then
**Web Maps**, then **Tile Services**, then **3D Tiles Services**. A folder
can hold all of them. Items whose ``folderId`` is unknown sit at the root, so
an item in a since-deleted folder is never lost from the view. A category
bucket is only emitted when it has content, so empty buckets never clutter a
folder.
"""

# Category metadata: order + display title for each item kind's bucket. A
# saved geoi project is a "Web Map", so projects get their OWN bucket rather
# than riding with feature services.
_CATEGORIES = (
    ("feature", "Feature Services"),
    ("project", "Web Maps"),
    ("tile", "Tile Services"),
    ("tiles3d", "3D Tiles Services"),
)
# Which category each item kind belongs to.
_KIND_CATEGORY = {"service": "feature", "project": "project", "tile": "tile",
                  "tiles3d": "tiles3d"}


def _title(entry):
    return (entry.get("title") or entry.get("name")
            or str(entry.get("id", ""))).strip()


def _owned(items, owner_id):
    """Keep only items owned by ``owner_id`` (no filter when it is None)."""
    if owner_id is None:
        return list(items)
    out = []
    for item in items:
        owner = item.get("owner") or {}
        if owner.get("id") == owner_id:
            out.append(item)
    return out


def build_content_tree(folders, services, projects, owner_id=None, tiles=None,
                       tiles3d=None, shared_tiles=None, shared_tiles3d=None):
    """Return the root-level list of nodes for the content tree.

    ``owner_id`` (the signed-in user's id) restricts services and projects
    to the ones that user owns — so they never see other people's public or
    group-shared content. Folders already come back owner-scoped from the
    hub, so they are not filtered again.

    ``tiles`` (the user's published raster TILE SERVICES) carry a ``folderId``
    now, so they live in folders alongside feature services and are grouped
    under each level's **Tile Services** category. They already come back
    owner-scoped from ``/raster/services``.

    ``tiles3d`` (the user's published 3D TILES SERVICES, from
    ``/tiles3d/services``) follow the exact same rules as ``tiles``:
    owner-scoped by the server, folder-aware when the summary carries a
    ``folderId``, grouped under each level's **3D Tiles Services** category.

    ``shared_tiles`` / ``shared_tiles3d`` (from the ``?scope=shared`` endpoints,
    #981) are tile / 3D-Tiles services shared WITH the signed-in user via a
    group — content they can consume but do NOT own. They are grouped under a
    single **Shared with me** section appended at the root end, kept DISTINCT
    from the user's own folders/categories (a shared item never lands in a
    folder, and is flagged ``"shared": True`` so the panel shows it read-only).
    The section is omitted entirely when there is nothing shared.
    """
    folders = list(folders or [])
    services = _owned(services or [], owner_id)
    projects = _owned(projects or [], owner_id)
    tiles = list(tiles or [])
    tiles3d = list(tiles3d or [])

    known = {f.get("id") for f in folders}

    # Folder nodes keyed by id, then linked into a tree by parentId.
    nodes = {}
    for f in folders:
        nodes[f.get("id")] = {
            "kind": "folder",
            "id": f.get("id"),
            "title": (f.get("title") or "Folder").strip(),
            "parentId": f.get("parentId"),
            "children": [],
            # Items land in `_items` first, then get grouped into categories.
            "_items": [],
        }

    roots = []  # folder nodes only; category buckets are appended after
    root_items = []
    for fid, node in nodes.items():
        parent = node["parentId"]
        if parent in nodes and parent != fid:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    def items_bucket(fid):
        return nodes[fid]["_items"] if fid in nodes else root_items

    def place(items, kind):
        for item in items:
            fid = item.get("folderId")
            target = items_bucket(fid) if fid in known else root_items
            target.append({"kind": kind, "payload": item})

    place(services, "service")
    place(projects, "project")
    place(tiles, "tile")
    place(tiles3d, "tiles3d")

    # Sort folders (recursively) and group their items into categories.
    roots.sort(key=lambda n: n["title"].lower())
    for node in roots:
        _finalize_folder(node)

    # Root level: folders first, then the category buckets (in category order),
    # then — LAST — the "Shared with me" section (when any content is shared).
    root = roots + _categorize(root_items)
    shared = _shared_section(shared_tiles, shared_tiles3d)
    if shared is not None:
        root.append(shared)
    return root


def _shared_section(shared_tiles, shared_tiles3d):
    """The "Shared with me" root section grouping tile + 3D-Tiles services
    shared WITH the viewer via a group (#981), or None when nothing is shared.

    Reuses the same category buckets (**Tile Services**, **3D Tiles Services**)
    as the owned tree, so shared content reads identically — only its top-level
    home differs. Each leaf carries ``"shared": True`` so the browser panel
    renders it read-only (the viewer may open it, never rename/move/delete it).
    """
    items = []
    for tile in (shared_tiles or []):
        items.append({"kind": "tile", "payload": tile, "shared": True})
    for t3d in (shared_tiles3d or []):
        items.append({"kind": "tiles3d", "payload": t3d, "shared": True})
    if not items:
        return None

    return {
        "kind": "shared",
        "title": "Shared with me",
        "children": _categorize(items),
    }


def _finalize_folder(node):
    """Sort sub-folders, group this folder's items into category buckets, and
    splice them after the (sorted) sub-folders so the children read
    folders → Feature Services → Web Maps → Tile Services → 3D Tiles
    Services."""
    subfolders = sorted(
        (c for c in node["children"] if c["kind"] == "folder"),
        key=lambda n: n["title"].lower(),
    )
    for child in subfolders:
        _finalize_folder(child)
    node["children"] = subfolders + _categorize(node.pop("_items"))


def _categorize(items):
    """Group a flat list of item nodes into ordered category buckets.

    Returns a list of ``category`` nodes (only the non-empty ones), each with
    its items sorted by title. Deterministic: categories in ``_CATEGORIES``
    order, items alphabetical within each.
    """
    by_category = {}
    for item in items:
        cat = _KIND_CATEGORY.get(item["kind"])
        if cat is not None:
            by_category.setdefault(cat, []).append(item)
    buckets = []
    for cat, title in _CATEGORIES:
        members = by_category.get(cat)
        if not members:
            continue
        members.sort(key=lambda n: (_ITEM_ORDER.get(n["kind"], 9),
                                    _title(n["payload"]).lower()))
        buckets.append({
            "kind": "category",
            "category": cat,
            "title": title,
            "children": members,
        })
    return buckets


# Each category holds a single kind now, so the per-kind order is flat.
_ITEM_ORDER = {"service": 0, "project": 0, "tile": 0, "tiles3d": 0}
