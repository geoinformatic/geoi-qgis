"""Build the geoi content folder tree from flat hub listings.

Pure Python (no QGIS, no Qt) so the tree shape — folder nesting, owner
filtering and item placement — is unit-testable without a QGIS install.
The browser panel renders whatever this returns.

Node shapes:

  folder   {"kind": "folder", "id", "title", "parentId", "children": [...]}
  service  {"kind": "service", "payload": <service entry>}
  project  {"kind": "project", "payload": <project summary>}

A level is ordered folders-first (by title), then services, then projects
(each by title). Items whose ``folderId`` is unknown sit at the root, so a
service in a since-deleted folder is never lost from the view.
"""


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


def build_content_tree(folders, services, projects, owner_id=None):
    """Return the root-level list of nodes for the content tree.

    ``owner_id`` (the signed-in user's id) restricts services and projects
    to the ones that user owns — so they never see other people's public or
    group-shared content. Folders already come back owner-scoped from the
    hub, so they are not filtered again.
    """
    folders = list(folders or [])
    services = _owned(services or [], owner_id)
    projects = _owned(projects or [], owner_id)

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
        }

    roots = []
    for fid, node in nodes.items():
        parent = node["parentId"]
        if parent in nodes and parent != fid:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    def bucket(fid):
        return nodes[fid]["children"] if fid in nodes else roots

    def place(items, kind):
        for item in items:
            fid = item.get("folderId")
            target = bucket(fid) if fid in known else roots
            target.append({"kind": kind, "payload": item})

    place(services, "service")
    place(projects, "project")

    _sort_level(roots)
    return roots


_ORDER = {"folder": 0, "service": 1, "project": 2}


def _node_title(node):
    if node["kind"] == "folder":
        return node["title"]
    return _title(node["payload"])


def _sort_level(nodes):
    for node in nodes:
        if node["kind"] == "folder":
            _sort_level(node["children"])
    nodes.sort(key=lambda n: (_ORDER.get(n["kind"], 9),
                              _node_title(n).lower()))
