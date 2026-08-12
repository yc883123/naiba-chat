import unittest
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"


class SkillContractTests(unittest.TestCase):
    def test_external_generation_requires_current_turn_authorization(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("## Provider Boundary", skill)
        self.assertIn("authorization only for the configured ComfyUI instance", skill)
        self.assertIn("unless the user explicitly requests that provider in the current turn", skill)
        self.assertIn("Do not silently fall back to another provider", skill)

    def test_naibachat_setup_is_automatic(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("scripts/install_naiba.ps1", skill)
        self.assertIn("register_mcp", skill)
        self.assertIn("Never tell a NaibaChat user to merge JSON or restart the client", skill)


if __name__ == "__main__":
    unittest.main()
