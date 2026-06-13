import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi.content_tree import build_content_tree  # noqa: E402


def _kinds(nodes):
    return [n["kind"] for n in nodes]


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
        names = [n["payload"].get("name") for n in tree]
        self.assertIn("mine", names)
        self.assertNotIn("theirs", names)
        self.assertNotIn("public-other", names)
        self.assertIn("P-mine", names)
        self.assertNotIn("P-theirs", names)

    def test_no_owner_keeps_everything(self):
        tree = build_content_tree([], self.services, self.projects, owner_id=None)
        self.assertEqual(len(tree), 5)


class FolderNestingTest(unittest.TestCase):
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
        # root has folder Alpha + loose service (folders first)
        self.assertEqual(_kinds(tree), ["folder", "service"])
        alpha = tree[0]
        self.assertEqual(alpha["title"], "Alpha")
        # Alpha contains subfolder Beta then service InA
        self.assertEqual(_kinds(alpha["children"]), ["folder", "service"])
        beta = alpha["children"][0]
        self.assertEqual(beta["title"], "Beta")
        self.assertEqual(beta["children"][0]["payload"]["name"], "in-b")

    def test_unknown_folder_falls_back_to_root(self):
        services = [{"name": "orphan", "title": "Orphan", "owner": {"id": 1},
                     "folderId": "gone"}]
        tree = build_content_tree([], services, [], owner_id=1)
        self.assertEqual(tree[0]["payload"]["name"], "orphan")

    def test_ordering_folders_then_services_then_projects(self):
        folders = [{"id": "z", "parentId": None, "title": "Zeta"}]
        services = [{"name": "s", "title": "Svc", "owner": {"id": 1}}]
        projects = [{"id": 1, "name": "Proj", "owner": {"id": 1}}]
        tree = build_content_tree(folders, services, projects, owner_id=1)
        self.assertEqual(_kinds(tree), ["folder", "service", "project"])


if __name__ == "__main__":
    unittest.main()
