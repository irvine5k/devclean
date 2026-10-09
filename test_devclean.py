"""Tests for devclean. Run: python3 -m unittest -v test_devclean"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import devclean  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


class WorktreesAndBuildsTest(unittest.TestCase):
    """A fixture repo with one worktree per case devclean must handle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        os.environ["DEVCLEAN_LOG"] = str(self.tmp / "devclean.log")
        devclean.LOG_PATH = self.tmp / "devclean.log"
        self.root = self.tmp / "projects"
        self.repo = self.root / "app"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "pubspec.yaml").write_text("name: app\n")
        (self.repo / "lib.dart").write_text("void main() {}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        # An untracked file in the main checkout that a worktree copies.
        (self.repo / "notes.md").write_text("shared notes\n")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "app.bin").write_bytes(b"x" * 4096)

        self.wt = {}
        for name in ("clean", "dirty", "locked", "unique", "dupe", "inuse", "missing"):
            path = self.tmp / "wts" / name
            git(self.repo, "worktree", "add", "-q", "-b", name, str(path))
            self.wt[name] = path
        for name in ("detached-safe", "detached-orphan"):
            path = self.tmp / "wts" / name
            git(self.repo, "worktree", "add", "-q", "--detach", str(path))
            self.wt[name] = path

        (self.wt["dirty"] / "lib.dart").write_text("changed\n")
        (self.wt["dirty"] / "build").mkdir()
        git(self.repo, "worktree", "lock", str(self.wt["locked"]))
        (self.wt["unique"] / "only-here.txt").write_text("work\n")
        shutil.copy(self.repo / "notes.md", self.wt["dupe"] / "notes.md")
        (self.wt["inuse"] / "build").mkdir()
        orphan = self.wt["detached-orphan"]
        (orphan / "lib.dart").write_text("new\n")
        git(orphan, "commit", "-qam", "commit on no branch")
        shutil.rmtree(self.wt["missing"])

        self.sleeper = subprocess.Popen(["sleep", "60"], cwd=self.wt["inuse"])
        # The fixture is brand new; tests opt out of the idle guard unless they test it.
        self.config = self.tmp / "config.toml"
        self.config.write_text("worktree_min_idle_hours = 0\nbuild_min_idle_hours = 0\n")
        (self.wt["locked"] / "build").mkdir()

    def tearDown(self):
        self.sleeper.kill()
        self.sleeper.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, *extra: str) -> list[str]:
        return [*extra, "--root", str(self.root), "--config", str(self.config)]

    def write_plan(self, *categories: str) -> Path:
        plan = self.tmp / "plan.tsv"
        self.assertEqual(devclean.main(self.args(*categories, "--plan-out", str(plan))), 0)
        return plan

    def apply_plan(self, plan: Path) -> int:
        return devclean.main(self.args("--apply", "--plan", str(plan), "--yes"))

    def worktree_paths(self) -> set[str]:
        out = git(self.repo, "worktree", "list", "--porcelain")
        return {line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")}

    def test_dry_run_deletes_nothing(self):
        before = self.worktree_paths()
        code = devclean.main(["worktrees", "builds", "--root", str(self.root),
                              "--config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertEqual(self.worktree_paths(), before)
        self.assertTrue((self.repo / "build").exists())

    def test_apply_removes_only_worktrees_without_work(self):
        self.assertEqual(self.apply_plan(self.write_plan("worktrees")), 0)
        left = self.worktree_paths()

        for gone in ("clean", "dupe", "detached-safe", "missing"):
            self.assertNotIn(str(self.wt[gone]), left, gone)
        for kept in ("dirty", "locked", "unique", "inuse", "detached-orphan"):
            self.assertIn(str(self.wt[kept]), left, kept)
            self.assertTrue(self.wt[kept].exists(), kept)
        # Branches survive the checkout removal.
        self.assertIn("clean", git(self.repo, "branch", "--list", "clean"))
        self.assertTrue((self.wt["unique"] / "only-here.txt").exists())

    def test_verdicts_name_the_reason(self):
        ctx = devclean.Context(devclean.load_config(self.config), [self.root], set())
        items = {i.label.split("  ")[0]: i for i in devclean.scan_worktrees(ctx)}
        self.assertIn("uncommitted", items[str(self.wt["dirty"])].reason)
        self.assertEqual(items[str(self.wt["locked"])].reason, "locked")
        self.assertIn("only-here.txt", items[str(self.wt["unique"])].reason)
        self.assertIn("running process", items[str(self.wt["inuse"])].reason)
        self.assertIn("no branch", items[str(self.wt["detached-orphan"])].reason)

    def test_recently_used_worktrees_stay_by_default(self):
        ctx = devclean.Context(devclean.load_config(Path("/nonexistent")), [self.root], set())
        items = {i.label.split("  ")[0]: i for i in devclean.scan_worktrees(ctx)}
        self.assertTrue(items[str(self.wt["clean"])].kept)
        self.assertIn("last 24 h", items[str(self.wt["clean"])].reason)

    def test_a_process_naming_the_path_marks_it_in_use(self):
        ctx = devclean.Context(devclean.load_config(self.config), [self.root], set())
        clean = self.wt["clean"]
        self.assertFalse(ctx.in_use(clean))
        named = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", f"{clean}/build/x"], cwd=self.tmp)
        try:
            fresh = devclean.Context(devclean.load_config(self.config), [self.root], set())
            self.assertTrue(fresh.in_use(clean))
            # A sibling whose name only starts the same is not in use.
            self.assertFalse(fresh.in_use(Path(str(clean)[:-1])))
        finally:
            named.kill()
            named.wait()

    def test_recent_build_folders_stay_by_default(self):
        ctx = devclean.Context(devclean.load_config(Path("/nonexistent")), [self.root], set())
        items = {i.label: i for i in devclean.scan_builds(ctx)}
        self.assertTrue(items[str(self.repo / "build")].kept)
        self.assertIn("last 2 h", items[str(self.repo / "build")].reason)

    def test_apply_removes_build_folders_except_in_use(self):
        self.assertEqual(self.apply_plan(self.write_plan("builds")), 0)
        self.assertFalse((self.repo / "build").exists())
        self.assertFalse((self.wt["dirty"] / "build").exists())
        self.assertTrue((self.wt["inuse"] / "build").exists())
        self.assertTrue((self.wt["locked"] / "build").exists())
        self.assertIn("OK   builds", (self.tmp / "devclean.log").read_text())

    # ------------------------------------------------------------ review step

    def test_yes_without_a_reviewed_plan_is_refused(self):
        before = self.worktree_paths()
        code = devclean.main(self.args("worktrees", "--apply", "--yes"))
        self.assertEqual(code, 2)
        self.assertEqual(self.worktree_paths(), before)

    def test_writing_a_plan_deletes_nothing(self):
        before = self.worktree_paths()
        plan = self.write_plan("worktrees", "builds")
        self.assertEqual(self.worktree_paths(), before)
        self.assertTrue((self.repo / "build").exists())
        text = plan.read_text()
        self.assertIn(str(self.wt["clean"]), text)
        self.assertNotIn(str(self.wt["dirty"]) + "  ", text)  # kept items are not in the plan

    def test_a_line_removed_in_review_is_kept(self):
        plan = self.write_plan("worktrees")
        lines = [ln for ln in plan.read_text().splitlines(keepends=True)
                 if str(self.wt["clean"]) not in ln]
        plan.write_text("".join(lines))

        self.assertEqual(self.apply_plan(plan), 0)
        left = self.worktree_paths()
        self.assertIn(str(self.wt["clean"]), left)
        self.assertNotIn(str(self.wt["dupe"]), left)

    def test_an_item_that_became_unsafe_after_review_is_kept(self):
        plan = self.write_plan("worktrees")
        (self.wt["clean"] / "lib.dart").write_text("work started after the review\n")

        self.assertEqual(self.apply_plan(plan), 0)
        self.assertTrue(self.wt["clean"].exists())
        self.assertEqual((self.wt["clean"] / "lib.dart").read_text(),
                         "work started after the review\n")
        self.assertFalse(self.wt["dupe"].exists())

    def test_interactive_review_item_by_item(self):
        # Review each; keep the first item, delete every other one.
        answers = iter(["r", "n"] + ["y"] * 20)
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", lambda _: next(answers)):
            self.assertEqual(devclean.main(self.args("builds", "--apply")), 0)
        builds = [self.repo / "build", self.wt["dirty"] / "build"]
        self.assertEqual(sum(b.exists() for b in builds), 1)

    def test_interactive_skip_deletes_nothing(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", lambda _: "s"):
            self.assertEqual(devclean.main(self.args("builds", "--apply")), 0)
        self.assertTrue((self.repo / "build").exists())
        self.assertTrue((self.wt["dirty"] / "build").exists())


class RulesTest(unittest.TestCase):
    def test_runtimes_keep_newest_major_per_platform(self):
        rts = {
            "a": {"version": "26.2", "platformIdentifier": "ios", "runtimeIdentifier": "r26"},
            "b": {"version": "18.6", "platformIdentifier": "ios", "runtimeIdentifier": "r186"},
            "c": {"version": "18.4", "platformIdentifier": "ios", "runtimeIdentifier": "r184"},
            "d": {"version": "11.0", "platformIdentifier": "watch", "runtimeIdentifier": "w11"},
            "e": {"version": "17.0", "platformIdentifier": "ios", "runtimeIdentifier": "r17",
                  "deletable": False},
        }
        v = devclean.runtimes_to_delete(rts, keep_majors=1, in_use={"r184"})
        self.assertNotEqual(v["a"], "")  # newest iOS major
        self.assertEqual(v["b"], "")  # deleted
        self.assertIn("booted", v["c"])
        self.assertNotEqual(v["d"], "")  # newest watchOS major
        self.assertIn("not deletable", v["e"])

    def test_device_support_keeps_newest_per_model(self):
        names = [
            "iPhone16,1 18.6 (22G86)", "iPhone16,1 18.6.2 (22G100)",
            "iPhone16,1 26.2.1 (23C71)", "iPhone16,1 26.5 (23F77)",
            "iPhone14,2 17.2 (21C62)", "16.4 (20E247)", "17.0 (21A329)",
        ]
        doomed = devclean.device_support_to_delete(names, keep=1)
        self.assertEqual(doomed, {
            "iPhone16,1 18.6 (22G86)", "iPhone16,1 18.6.2 (22G100)",
            "iPhone16,1 26.2.1 (23C71)", "16.4 (20E247)",
        })

    def test_parse_size(self):
        self.assertEqual(devclean.parse_size("11.32GB (42%)"), 11_320_000_000)
        self.assertEqual(devclean.parse_size("258.3kB"), 258_300)
        self.assertEqual(devclean.parse_size("0B"), 0)

    def test_unknown_category_fails(self):
        with self.assertRaises(SystemExit):
            devclean.resolve_categories(["nope"])

    def test_aliases_expand(self):
        names = [c.name for c in devclean.resolve_categories(["xcode", "caches"])]
        self.assertEqual(names[:2], ["derived-data", "device-support"])
        self.assertIn("cache-npm", names)


if __name__ == "__main__":
    unittest.main()
