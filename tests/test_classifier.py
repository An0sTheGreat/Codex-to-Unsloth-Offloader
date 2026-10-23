"""Tests for the deterministic task classifier (Phase 3, bridge.classifier).

All rules are deterministic: the same input always yields the same
:class:`~bridge.schemas.TaskAssessment`. Representative tasks cover every
risk flag, all five difficulty levels, all four length levels, and the
high-risk override of offload eligibility.
"""

import unittest

from bridge.classifier import MAX_CONTEXT_FILES, TaskClassifier
from bridge.schemas import (
    DifficultyLevel,
    Execution,
    LengthLevel,
    OffloadDecision,
    RiskFlag,
    TaskAssessment,
)


class TestTaskClassifier(unittest.TestCase):
    def setUp(self):
        self.clf = TaskClassifier()

    def assess(self, task, context=None):
        return self.clf.assess(task, context=context)

    # -- representative tasks --

    def test_trivial_single_file(self):
        ta = self.assess("Fix a typo in the README.")
        self.assertEqual(ta.difficulty, 1)
        self.assertIs(ta.difficulty_label, DifficultyLevel.TRIVIAL)
        self.assertIs(ta.length, LengthLevel.SHORT)
        self.assertEqual(ta.risk_flags, [])
        self.assertFalse(ta.is_high_risk)
        self.assertIs(ta.offload, OffloadDecision.YES)
        self.assertIs(ta.execution, Execution.LOCAL_HANDOFF)
        self.assertTrue(ta.reason)

    def test_easy_helper(self):
        ta = self.assess("Add a small helper function to format the log output.")
        self.assertEqual(ta.difficulty, 1)
        self.assertIs(ta.length, LengthLevel.SHORT)
        self.assertEqual(ta.risk_flags, [])
        self.assertIs(ta.offload, OffloadDecision.YES)
        self.assertIs(ta.execution, Execution.LOCAL_HANDOFF)

    def test_moderate_debug_integration(self):
        ta = self.assess("Debug a flaky integration failure across the codebase.")
        self.assertEqual(ta.difficulty, 3)  # base 1 + debug + integration
        self.assertIs(ta.length, LengthLevel.LONG)
        self.assertEqual(ta.risk_flags, [])
        self.assertIs(ta.offload, OffloadDecision.PARTIAL)
        self.assertIs(ta.execution, Execution.PARTIAL_HANDOFF)

    def test_architecture_flag_keeps_in_codex(self):
        ta = self.assess("Redesign the event-driven pipeline architecture.")
        self.assertEqual(ta.difficulty, 4)  # architecture-heavy base
        self.assertEqual(ta.risk_flags,
                         [RiskFlag.ARCHITECTURE_HEAVY])
        self.assertTrue(ta.is_high_risk)
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_difficulty_four_partial_handoff(self):
        ta = self.assess("Debug a complex integration failure in the handler code.")
        self.assertEqual(ta.difficulty, 4)  # base 1 + debug + complex + integration
        self.assertEqual(ta.risk_flags, [])
        self.assertIs(ta.offload, OffloadDecision.PARTIAL)
        self.assertIs(ta.execution, Execution.PARTIAL_HANDOFF)

    def test_expert_no_flags_keeps_in_codex(self):
        ta = self.assess(
            "Complex expert-level debugging of the integration failure and "
            "the endpoint layer.")
        self.assertEqual(ta.difficulty, 5)  # base 1 + complex + debug + integration + endpoint + expert, clamped
        self.assertEqual(ta.risk_flags, [])
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_expert_clamped_at_five(self):
        ta = self.assess(
            "Complex expert-level redesign of the monolith, including the"
            " database schema migration and a full rewrite of the core system.")
        self.assertEqual(ta.difficulty, 5)  # base 4 + complex + expert + rewrite, clamped
        self.assertIs(ta.difficulty_label, DifficultyLevel.EXPERT)
        self.assertEqual(ta.risk_flags, [RiskFlag.ARCHITECTURE_HEAVY])
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_security_flag(self):
        ta = self.assess("Fix the SQL injection vulnerability in the login authentication flow.")
        self.assertEqual(ta.risk_flags, [RiskFlag.SECURITY_SENSITIVE])
        self.assertEqual(ta.difficulty, 3)  # security base
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)
        self.assertIn("security_sensitive", ta.reason)

    def test_secrets_flag(self):
        ta = self.assess("Set the password for the local admin account.")
        self.assertEqual(ta.risk_flags, [RiskFlag.SECRETS_CREDENTIALS])
        self.assertEqual(ta.difficulty, 3)  # secrets base
        self.assertEqual(ta.length, LengthLevel.SHORT)
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_destructive_flag(self):
        ta = self.assess("Delete the old test database and all its tables.")
        self.assertEqual(ta.risk_flags, [RiskFlag.DESTRUCTIVE_ACTIONS])
        self.assertEqual(ta.difficulty, 4)
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_deployment_flag(self):
        ta = self.assess("Deploy the release to production.")
        self.assertEqual(ta.risk_flags, [RiskFlag.DEPLOYMENT_RELEASE])
        self.assertEqual(ta.difficulty, 4)
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_excessive_context_from_text(self):
        ta = self.assess("Refactor the entire repository so every module follows the new style.")
        self.assertEqual(ta.risk_flags,
                         [RiskFlag.EXCESSIVE_CONTEXT])
        self.assertEqual(ta.length, LengthLevel.EXTENDED)
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_excessive_context_from_context(self):
        many = [f"{i}.py" for i in range(MAX_CONTEXT_FILES + 1)]
        ta = self.assess("Refactor the parser.", context={"files": many})
        self.assertEqual(ta.risk_flags, [RiskFlag.EXCESSIVE_CONTEXT])
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_small_context_not_flagged(self):
        ta = self.assess("Refactor the parser.", context={"files": ["a.py", "b.py"]})
        self.assertEqual(ta.risk_flags, [])
        self.assertIs(ta.offload, OffloadDecision.YES)

    # -- length --

    def test_length_levels(self):
        self.assertIs(self.assess("Fix a typo in the README.").length, LengthLevel.SHORT)
        self.assertIs(self.assess("Update the transport module and the routing interface.").length,
                      LengthLevel.MEDIUM)
        self.assertIs(self.assess("Debug a flaky integration failure across the codebase.").length,
                      LengthLevel.LONG)
        self.assertIs(self.assess("Migrate the entire codebase to the new framework.").length,
                      LengthLevel.EXTENDED)

    def test_length_notes(self):
        notes = {
            LengthLevel.SHORT: "small, isolated change",
            LengthLevel.MEDIUM: "multi-file change",
            LengthLevel.LONG: "multi-module change",
            LengthLevel.EXTENDED: "repository-wide change",
        }
        for task, level in (
            ("Fix a typo in the README.", LengthLevel.SHORT),
            ("Update the transport module and the routing interface.", LengthLevel.MEDIUM),
            ("Debug a flaky integration failure across the codebase.", LengthLevel.LONG),
            ("Migrate the entire codebase to the new framework.", LengthLevel.EXTENDED),
        ):
            ta = self.assess(task)
            self.assertIs(ta.length, level)
            self.assertEqual(ta.length_note, notes[level])

    # -- determinism and contract --

    def test_deterministic(self):
        task = "Debug a flaky integration failure across the codebase."
        a, b = self.assess(task), self.assess(task)
        self.assertEqual(a, b)
        self.assertEqual(a.model_dump(), b.model_dump())

    def test_reason_always_nonempty(self):
        tasks = [
            "Fix a typo in the README.",
            "Add a small helper function to format the log output.",
            "Debug a flaky integration failure across the codebase.",
            "Redesign the event-driven pipeline architecture.",
            "Fix the SQL injection vulnerability in the login authentication flow.",
            "Set the password for the local admin account.",
            "Delete the old test database and all its tables.",
            "Deploy the release to production.",
            "Refactor the entire repository so every module follows the new style.",
        ]
        for task in tasks:
            with self.subTest(task=task):
                self.assertTrue(self.assess(task).reason)

    def test_difficulty_label_matches(self):
        for task in [
            "Fix a typo in the README.",
            "Add a small helper function to format the log output.",
            "Debug a flaky integration failure across the codebase.",
            "Redesign the event-driven pipeline architecture.",
            "Complex expert-level redesign of the monolith, including the"
            " database schema migration and a full rewrite of the core system.",
        ]:
            ta = self.assess(task)
            expected = {
                1: DifficultyLevel.TRIVIAL, 2: DifficultyLevel.EASY,
                3: DifficultyLevel.MODERATE, 4: DifficultyLevel.HARD,
                5: DifficultyLevel.EXPERT,
            }
            self.assertIs(ta.difficulty_label, expected[ta.difficulty])

    def test_high_risk_overrides_eligibility(self):
        # Even a trivially small task stays in Codex when a risk flag is set.
        ta = self.assess("Change the password.")
        self.assertEqual(ta.difficulty, 3)
        self.assertEqual(ta.length, LengthLevel.SHORT)
        self.assertIs(ta.offload, OffloadDecision.NO)
        self.assertIs(ta.execution, Execution.KEEP_IN_CODEX)

    def test_no_flags_on_benign_task(self):
        ta = self.assess("Rename the variable count to counts.")
        self.assertEqual(ta.risk_flags, [])
        self.assertEqual(ta.difficulty, 1)
        self.assertIs(ta.offload, OffloadDecision.YES)

    def test_empty_task_rejected(self):
        with self.assertRaises(ValueError):
            self.clf.assess("   ")

    def test_assessment_is_valid_contract(self):
        for task in [
            "Fix a typo in the README.",
            "Debug a flaky integration failure across the codebase.",
            "Redesign the event-driven pipeline architecture.",
            "Set the password for the local admin account.",
        ]:
            with self.subTest(task=task):
                self.assertIsInstance(self.assess(task), TaskAssessment)


if __name__ == "__main__":
    unittest.main()
