"""Build the geoi content folder tree from flat hub listings.

Pure Python (no QGIS, no Qt) so the tree shape — folder nesting, owner
filtering, category grouping and item placement — is unit-testable without a
QGIS install. The browser panel renders whatever this returns.

Node shapes:

  folder    {"kind": "folder", "id", "title", "parentId", "children": [...]}
  category  {"kind": "category", "category": "feature"|"tile",
             "title", "children": [...]}
  service   {"kind": "service", "payload": <service entry>}
  project   {"kind": "project", "payload": <project summary>}
  tile      {"kind": "tile", "payload": <tile-service summary>}

The tree is STRUCTURED into clear, symmetrical categories: at every level
(the root AND inside each folder) the content is grouped under labelled
category buckets — **Feature Services** (feature services), **Web Maps**
(saved geoi projects) and **Tile Services** (raster tile services) — so the
three kinds of content are always cleanly separated and the structure looks
the same everywhere.

A level is ordered folders-first (by title), then **Feature Services**, then
**Web Maps**, then **Tile Services**. A folder can hold all three. Items whose
``folderId`` is unknown sit at the root, so an item in a since-deleted folder
is never lost from the view. A category bucket is only emitted when it has
content, so empty buckets never clutter a folder.
"""

# Category metadata: order + display title for each item kind's bucket. A
# saved geoi project is a "Web Map", so projects get their OWN bucket rather
# than riding with feature services.
_CATEGORIES = (
    ("feature", "Feature Services"),
    ("project", "Web Maps"),
    ("tile", "Tile Services"),
)
# Which category each item kind belongs to.
_KIND_CATEGORY = {"service": "feature", "project": "project", "tile": "tile"}


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


def build_content_tree(folders, services, projects, owner_id=None, tiles=None):
    """Return the root-level list of nodes for the content tree.

    ``owner_id`` (the signed-in user's id) restricts services and projects
    to the ones that user owns — so they never see other people's public or
    group-shared content. Folders already come back owner-scoped from the
    hub, so they are not filtered again.

    ``tiles`` (the user's published raster TILE SERVICES) carry a ``folderId``
    now, so they live in folders alongside feature services and are grouped
    under each level's **Tile Services** category. They already come back
    owner-scoped from ``/raster/services``.
    """
    folders = list(folders or [])
    services = _owned(services or [], owner_id)
    projects = _owned(projects or [], owner_id)
    tiles = list(tiles or [])

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

    # Sort folders (recursively) and group their items into categories.
    roots.sort(key=lambda n: n["title"].lower())
    for node in roots:
        _finalize_folder(node)

    # Root level: folders first, then the category buckets (in category order).
    return roots + _categorize(root_items)


def _finalize_folder(node):
    """Sort sub-folders, group this folder's items into category buckets, and
    splice them after the (sorted) sub-folders so the children read
    folders → Feature Services → Web Maps → Tile Services."""
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
_ITEM_ORDER = {"service": 0, "project": 0, "tile": 0}
