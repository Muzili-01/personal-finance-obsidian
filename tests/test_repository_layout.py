from __future__ import annotations

import unittest
from pathlib import Path


def repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError("cannot locate repository root")


class RepositoryLayoutTests(unittest.TestCase):
    def test_skill_payload_lives_at_repository_root(self) -> None:
        root = repository_root()
        self.assertTrue((root / "SKILL.md").is_file())
        for directory in ("scripts", "references", "assets", "agents", "tests"):
            self.assertTrue((root / directory).is_dir(), directory)
        self.assertTrue((root / "scripts" / "finance.py").is_file())

    def test_codex_and_claude_discovery_entries_resolve_to_repository_root(self) -> None:
        root = repository_root().resolve()
        entries = (
            root / ".agents" / "skills" / "personal-finance-obsidian",
            root / ".claude" / "skills" / "personal-finance-obsidian",
        )
        for entry in entries:
            self.assertTrue(entry.is_symlink(), str(entry))
            self.assertEqual(entry.resolve(), root)

    def test_readme_first_screen_has_installation_and_copyable_prompt(self) -> None:
        readme = (repository_root() / "README.md").read_text(encoding="utf-8")
        first_screen = "\n".join(readme.splitlines()[:60])
        self.assertIn("Codex", first_screen)
        self.assertIn("Claude Code", first_screen)
        self.assertIn("~/.agents/skills/personal-finance-obsidian", first_screen)
        self.assertIn("~/.claude/skills/personal-finance-obsidian", first_screen)
        self.assertIn("$personal-finance-obsidian", first_screen)
        self.assertIn("```text", first_screen)

    def test_cli_is_presented_only_as_advanced_usage(self) -> None:
        root = repository_root()
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(readme.index("## 高级用法"), readme.index("finance.py"))
        self.assertLess(skill.index("## 高级维护"), skill.index("finance.py"))
        self.assertNotIn("## Commands", skill)

    def test_privacy_copy_states_actual_processing_boundary(self) -> None:
        root = repository_root()
        combined = "\n".join(
            [
                (root / "README.md").read_text(encoding="utf-8"),
                (root / "SKILL.md").read_text(encoding="utf-8"),
            ]
        )
        self.assertIn("Skill 自带脚本不会主动联网", combined)
        self.assertIn("云端 Codex 或 Claude", combined)
        self.assertIn("服务提供商", combined)
        self.assertIn("Vault 中以未加密形式保存", combined)
        self.assertNotIn("Never upload financial files", combined)
        self.assertNotIn("保证完全本地", combined)


if __name__ == "__main__":
    unittest.main()
