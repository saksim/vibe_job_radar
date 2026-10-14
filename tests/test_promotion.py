"""Promotion route and release-note failures must stop before publication."""
import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_promotion import validate_promotion
from release_summary import validate_summary

SUMMARY = """## 解决了什么
- #318 发行说明缺少具体变更。
## 提高了什么
- 下载前可核对修复范围与剩余问题。
## 验证结果
- 定向离线回归通过。
## 剩余问题
- 真实干净机验收仍由 #319 跟踪。
"""


class PromotionTests(unittest.TestCase):
    def event(self, head="dev", base="release"):
        repo = {"full_name": "example/research"}
        return {"repository": repo, "pull_request": {
            "head": {"ref": head, "repo": dict(repo)},
            "base": {"ref": base, "repo": dict(repo)}, "body": SUMMARY}}

    def test_feature_and_ordered_promotions(self):
        for head, base in (("codex/fix", "dev"), ("dev", "release"), ("release", "main")):
            self.assertTrue(validate_promotion(self.event(head, base)))

    def test_shortcuts_and_same_named_fork_branches_are_refused(self):
        for head, base in (("codex/fix", "main"), ("dev", "main"), ("main", "release")):
            with self.assertRaises(ValueError):
                validate_promotion(self.event(head, base))
        event = copy.deepcopy(self.event())
        event["pull_request"]["head"]["repo"]["full_name"] = "other/fork"
        with self.assertRaises(ValueError):
            validate_promotion(event)

    def test_release_requires_concrete_nonempty_sections_and_trace(self):
        for text in (None, "", SUMMARY.replace("## 提高了什么", "## Other"),
                     SUMMARY.replace("- 下载前可核对修复范围与剩余问题。", "<!-- fill here -->"),
                     SUMMARY.replace("#318", "issue").replace("#319", "issue"),
                     SUMMARY + "\n## 剩余问题\n- duplicate",
                     SUMMARY + "x" * 65536):
            with self.subTest(text=str(text)[:60]), self.assertRaises(ValueError):
                validate_summary(text)
        self.assertEqual(validate_summary(SUMMARY), SUMMARY)

    def test_pr_body_is_required_only_for_promotions(self):
        event = self.event()
        event["pull_request"]["body"] = None
        with self.assertRaises(ValueError):
            validate_promotion(event)
        event["pull_request"]["base"]["ref"] = "dev"
        self.assertTrue(validate_promotion(event))


if __name__ == "__main__":
    unittest.main()
