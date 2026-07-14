import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi.content_tree import build_content_tree  # noqa: E402


def _kinds(nodes):
    return [n["kind"] for n in nodes]


def _category(tree, name):
    """Return the named category node ('feature'|'project'|'tile'), or None."""
    for n in tree:
        if n["kind"] == "category" and n["category"] == name:
            return n
    return None


def _payload_names(category_node):
    return [c["payload"].get("name") or c["payload"].get("title")
            for c in (category_node or {}).get("children", [])]


class OwnerFilterTest(unittest.TestCase):
    def setUp(self):
        self.services = [
            {"name": "mine", "title": "Mine", "owner": {"id": 1}},
            {"name": "theirs", "title": "Theirs", "owner": {"id": 2}},
            {"name": "public-other", "title": "Pub", "owner": {"id": 2},
             "visibility": "public"},
        ]
        self.projects = [
            {"id": 10, "name": "P-mine", "owner": {"id": 1}},
            {"id": 11, "name": "P-theirs", "owner": {"id": 2}},
        ]

    def test_only_owns_services_and_projects(self):
        tree = build_content_tree([], self.services, self.projects, owner_id=1)
        # Services live in Feature Services, projects in their own Web Maps.
        svc_names = _payload_names(_category(tree, "feature"))
        self.assertIn("mine", svc_names)
        self.assertNotIn("theirs", svc_names)
        self.assertNotIn("public-other", svc_names)
        proj_names = _payload_names(_category(tree, "project"))
        self.assertIn("P-mine", proj_names)
        self.assertNotIn("P-theirs", proj_names)

    def test_no_owner_keeps_everything(self):
        tree = build_content_tree([], self.services, self.projects, owner_id=None)
        # 3 services in Feature Services, 2 projects in Web Maps.
        self.assertEqual(len(_category(tree, "feature")["children"]), 3)
        self.assertEqual(len(_category(tree, "project")["children"]), 2)


class CategoryStructureTest(unittest.TestCase):
    """Feature services, web maps (projects) and tile services each live in
    their own labelled category, symmetric at the root and inside folders."""

    def test_root_has_three_ordered_categories(self):
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        projects = [{"id": 1, "name": "Proj", "owner": {"id": 1}}]
        tiles = [{"id": 5, "title": "Ortho", "visibility": "public"}]
        tree = build_content_tree([], services, projects, owner_id=1, tiles=tiles)
        self.assertEqual(_kinds(tree), ["category", "category", "category"])
        # Feature Services, then Web Maps, then Tile Services.
        self.assertEqual(tree[0]["category"], "feature")
        self.assertEqual(tree[0]["title"], "Feature Services")
        self.assertEqual(tree[1]["category"], "project")
        self.assertEqual(tree[1]["title"], "Web Maps")
        self.assertEqual(tree[2]["category"], "tile")
        self.assertEqual(tree[2]["title"], "Tile Services")
        self.assertEqual(_kinds(tree[0]["children"]), ["service"])
        self.assertEqual(_kinds(tree[1]["children"]), ["project"])
        self.assertEqual(_kinds(tree[2]["children"]), ["tile"])

    def test_empty_category_is_omitted(self):
        # Only one kind of content -> only that bucket is emitted.
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        tree = build_content_tree([], services, [], owner_id=1, tiles=[])
        self.assertEqual([n["category"] for n in tree], ["feature"])
        tree2 = build_content_tree([], [], [], owner_id=1,
                                   tiles=[{"id": 1, "title": "T"}])
        self.assertEqual([n["category"] for n in tree2], ["tile"])
        tree3 = build_content_tree(
            [], [], [{"id": 1, "name": "Proj", "owner": {"id": 1}}], owner_id=1)
        self.assertEqual([n["category"] for n in tree3], ["project"])

    def test_projects_get_their_own_web_maps_category(self):
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        projects = [{"id": 1, "name": "Proj", "owner": {"id": 1}}]
        tree = build_content_tree([], services, projects, owner_id=1)
        self.assertEqual(_kinds(_category(tree, "feature")["children"]), ["service"])
        self.assertEqual(_kinds(_category(tree, "project")["children"]), ["project"])

    def test_tiles_sorted_by_title(self):
        tiles = [
            {"id": 5, "title": "Beta", "visibility": "public"},
            {"id": 6, "title": "Alpha", "visibility": "private"},
        ]
        tree = build_content_tree([], [], [], owner_id=1, tiles=tiles)
        tile = _category(tree, "tile")
        self.assertEqual([c["payload"]["title"] for c in tile["children"]],
                         ["Alpha", "Beta"])

    def test_tiles_not_owner_filtered(self):
        # /raster/services already returns only the user's own tile services,
        # so they carry no `owner` block and must NOT be dropped.
        tiles = [{"id": 5, "title": "Mine", "visibility": "public"}]
        tree = build_content_tree([], [], [], owner_id=1, tiles=tiles)
        tile = _category(tree, "tile")
        self.assertEqual(tile["children"][0]["payload"]["title"], "Mine")


class Tiles3dCategoryTest(unittest.TestCase):
    """3D Tiles services get their own category, LAST in the category order,
    folder-aware exactly like raster tile services."""

    def test_tiles3d_category_is_last_and_titled(self):
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        projects = [{"id": 1, "name": "Proj", "owner": {"id": 1}}]
        tiles = [{"id": 5, "title": "Ortho", "visibility": "public"}]
        tiles3d = [{"id": 9, "title": "Scan", "visibility": "private"}]
        tree = build_content_tree([], services, projects, owner_id=1,
                                  tiles=tiles, tiles3d=tiles3d)
        self.assertEqual([n["category"] for n in tree],
                         ["feature", "project", "tile", "tiles3d"])
        t3d = _category(tree, "tiles3d")
        self.assertEqual(t3d["title"], "3D Tiles Services")
        self.assertEqual(_kinds(t3d["children"]), ["tiles3d"])
        self.assertEqual(t3d["children"][0]["payload"]["title"], "Scan")

    def test_tiles3d_only_emits_just_that_bucket(self):
        tree = build_content_tree([], [], [], owner_id=1,
                                  tiles3d=[{"id": 1, "title": "T"}])
        self.assertEqual([n["category"] for n in tree], ["tiles3d"])

    def test_tiles3d_sorted_by_title_and_not_owner_filtered(self):
        # /tiles3d/services returns only the user's own services (no `owner`
        # block) — they must NOT be dropped by the owner filter.
        tiles3d = [
            {"id": 5, "title": "Beta"},
            {"id": 6, "title": "Alpha"},
        ]
        tree = build_content_tree([], [], [], owner_id=1, tiles3d=tiles3d)
        t3d = _category(tree, "tiles3d")
        self.assertEqual([c["payload"]["title"] for c in t3d["children"]],
                         ["Alpha", "Beta"])

    def test_tiles3d_lives_in_its_folder(self):
        folders = [{"id": "a", "parentId": None, "title": "Alpha"}]
        tiles3d = [
            {"id": 7, "title": "InA", "folderId": "a"},
            {"id": 8, "title": "Loose"},
        ]
        tree = build_content_tree(folders, [], [], owner_id=1, tiles3d=tiles3d)
        # root: the folder first, then the root-level 3D Tiles bucket.
        self.assertEqual(_kinds(tree), ["folder", "category"])
        alpha = tree[0]
        self.assertEqual([n["category"] for n in alpha["children"]], ["tiles3d"])
        self.assertEqual(
            alpha["children"][0]["children"][0]["payload"]["title"], "InA")
        self.assertEqual(
            _category(tree, "tiles3d")["children"][0]["payload"]["title"],
            "Loose")

    def test_tiles3d_unknown_folder_falls_back_to_root(self):
        tiles3d = [{"id": 3, "title": "Orphan3d", "folderId": "gone"}]
        tree = build_content_tree([], [], [], owner_id=1, tiles3d=tiles3d)
        self.assertEqual(
            _category(tree, "tiles3d")["children"][0]["payload"]["title"],
            "Orphan3d")

    def test_folders_still_first_with_all_four_categories(self):
        folders = [{"id": "z", "parentId": None, "title": "Zeta"}]
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        projects = [{"id": 1, "name": "Proj", "owner": {"id": 1}}]
        tiles = [{"id": 5, "title": "Ortho"}]
        tiles3d = [{"id": 9, "title": "Scan"}]
        tree = build_content_tree(folders, services, projects, owner_id=1,
                                  tiles=tiles, tiles3d=tiles3d)
        self.assertEqual(_kinds(tree), ["folder", "category", "category",
                                        "category", "category"])
        self.assertEqual(tree[0]["title"], "Zeta")


class FolderNestingTest(unittest.TestCase):
    def test_folders_first_then_categories(self):
        folders = [{"id": "z", "parentId": None, "title": "Zeta"}]
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        tiles = [{"id": 9, "title": "Tiles", "owner": {"id": 1}}]
        tree = build_content_tree(folders, services, [], owner_id=1, tiles=tiles)
        # root: folder, then Feature Services, then Tile Services
        self.assertEqual(_kinds(tree), ["folder", "category", "category"])
        self.assertEqual(tree[0]["title"], "Zeta")

    def test_folder_holds_all_three_categories(self):
        folders = [{"id": "a", "parentId": None, "title": "Alpha"}]
        services = [
            {"name": "in-a", "title": "InA", "owner": {"id": 1}, "folderId": "a"},
        ]
        projects = [
            {"id": 1, "name": "ProjA", "owner": {"id": 1}, "folderId": "a"},
        ]
        tiles = [
            {"id": 7, "title": "TileInA", "visibility": "public", "folderId": "a"},
        ]
        tree = build_content_tree(folders, services, projects, owner_id=1,
                                  tiles=tiles)
        alpha = tree[0]
        self.assertEqual(alpha["kind"], "folder")
        # Inside the folder: the three category buckets, in order.
        self.assertEqual([n["category"] for n in alpha["children"]],
                         ["feature", "project", "tile"])
        feature, project, tile = alpha["children"]
        self.assertEqual(feature["children"][0]["payload"]["name"], "in-a")
        self.assertEqual(project["children"][0]["payload"]["name"], "ProjA")
        self.assertEqual(tile["children"][0]["payload"]["title"], "TileInA")

    def test_subfolders_and_placement(self):
        folders = [
            {"id": "a", "parentId": None, "title": "Alpha"},
            {"id": "b", "parentId": "a", "title": "Beta"},
        ]
        services = [
            {"name": "in-a", "title": "InA", "owner": {"id": 1}, "folderId": "a"},
            {"name": "in-b", "title": "InB", "owner": {"id": 1}, "folderId": "b"},
            {"name": "loose", "title": "Loose", "owner": {"id": 1}},
        ]
        tree = build_content_tree(folders, services, [], owner_id=1)
        # root: folder Alpha, then the Feature Services category for `loose`.
        self.assertEqual(_kinds(tree), ["folder", "category"])
        alpha = tree[0]
        # Alpha: subfolder Beta, then its own Feature Services category (InA).
        self.assertEqual(_kinds(alpha["children"]), ["folder", "category"])
        beta = alpha["children"][0]
        self.assertEqual(beta["title"], "Beta")
        self.assertEqual(beta["children"][0]["category"], "feature")
        self.assertEqual(
            beta["children"][0]["children"][0]["payload"]["name"], "in-b")
        # InA sits in Alpha's own Feature Services bucket.
        self.assertEqual(
            alpha["children"][1]["children"][0]["payload"]["name"], "in-a")

    def test_unknown_folder_falls_back_to_root(self):
        services = [{"name": "orphan", "title": "Orphan", "owner": {"id": 1},
                     "folderId": "gone"}]
        tiles = [{"id": 3, "title": "OrphanTile", "folderId": "gone-too"}]
        tree = build_content_tree([], services, [], owner_id=1, tiles=tiles)
        self.assertEqual(
            _category(tree, "feature")["children"][0]["payload"]["name"],
            "orphan")
        self.assertEqual(
            _category(tree, "tile")["children"][0]["payload"]["title"],
            "OrphanTile")


class SharedWithMeTest(unittest.TestCase):
    """#981: tile / 3D-Tiles services shared WITH the viewer via a group appear
    under a single top-level 'Shared with me' section, kept distinct from the
    viewer's own content and flagged read-only."""

    def test_no_shared_content_omits_the_section(self):
        tiles = [{"id": 5, "title": "Mine"}]
        tree = build_content_tree([], [], [], owner_id=1, tiles=tiles)
        self.assertNotIn("shared", _kinds(tree))

    def test_shared_section_is_last_and_grouped(self):
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        tiles = [{"id": 5, "title": "Own tiles"}]
        shared_tiles = [{"id": 20, "title": "Team ortho", "visibility": "groups"}]
        shared_tiles3d = [{"id": 30, "title": "Team scan", "visibility": "groups"}]
        tree = build_content_tree(
            [], services, [], owner_id=1, tiles=tiles,
            shared_tiles=shared_tiles, shared_tiles3d=shared_tiles3d)
        # Owned categories first, then the Shared-with-me section LAST.
        self.assertEqual(_kinds(tree), ["category", "category", "shared"])
        shared = tree[-1]
        self.assertEqual(shared["title"], "Shared with me")
        # Its children are the same per-kind category buckets, tile then tiles3d.
        self.assertEqual([n["category"] for n in shared["children"]],
                         ["tile", "tiles3d"])
        self.assertEqual(shared["children"][0]["children"][0]["payload"]["title"],
                         "Team ortho")
        self.assertEqual(shared["children"][1]["children"][0]["payload"]["title"],
                         "Team scan")

    def test_shared_leaves_are_flagged_shared(self):
        shared_tiles = [{"id": 20, "title": "Team ortho"}]
        tree = build_content_tree([], [], [], owner_id=1,
                                  shared_tiles=shared_tiles)
        shared = tree[-1]
        leaf = shared["children"][0]["children"][0]
        self.assertTrue(leaf.get("shared"))

    def test_only_shared_tiles3d_still_builds_the_section(self):
        shared_tiles3d = [{"id": 30, "title": "Team scan"}]
        tree = build_content_tree([], [], [], owner_id=1,
                                  shared_tiles3d=shared_tiles3d)
        self.assertEqual(_kinds(tree), ["shared"])
        shared = tree[0]
        self.assertEqual([n["category"] for n in shared["children"]], ["tiles3d"])

    def test_owned_content_never_gets_the_shared_flag(self):
        tiles = [{"id": 5, "title": "Own tiles"}]
        tree = build_content_tree([], [], [], owner_id=1, tiles=tiles)
        tile_cat = _category(tree, "tile")
        self.assertFalse(tile_cat["children"][0].get("shared"))


if __name__ == "__main__":
    unittest.main()
