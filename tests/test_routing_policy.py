"""Tests for the Phase 4 routing policy (bridge.routing.PolicyRouter).

No live Unsloth instance is required: model availability is driven by an
explicit ``ModelMetadata`` argument, and the policy is fully deterministic.
"""

import unittest

from bridge.classifier import TaskClassifier
from bridge.config import OffloaderConfig
from bridge.routing import PolicyRouter
from bridge.schemas import (
    LengthLevel,
    ModelMetadata,
    ModelProfile,
    RiskFlag,
    RoutingDecisionKind,
    TaskAssessment,
)


def _assessment(difficulty, length, risk_flags=None):
    """Build a mutually consistent TaskAssessment (classifier-style)."""
    risk_flags = risk_flags or []
    label = {1: "Trivial", 2: "Easy", 3: "Moderate", 4: "Hard", 5: "Expert"}[difficulty]
    if risk_flags or difficulty == 5:
        offload, execution = "No", "Keep in Codex"
    elif (difficulty == 4 or (difficulty == 3 and length in ("Long", "Extended"))
            or (difficulty in (1, 2) and length == "Extended")):
        offload, execution = "Partial", "Partial handoff"
    else:
        offload, execution = "Yes", "Local handoff"
    return TaskAssessment(
        difficulty=difficulty,
        difficulty_label=label,
        length=length,
        length_note="test",
        offload=offload,
        offload_note="test",
        execution=execution,
        risk_flags=sorted(risk_flags),
        reason=f"test task d{difficulty} {length}",
    )


def _loaded_model(name="0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0", loaded=True):
    return ModelMetadata(id=name, display_name=name, loaded=loaded)


class TestPolicyRouterFullLocal(unittest.TestCase):
    def setUp(self):
        self.router = PolicyRouter()

    def test_bounded_easy_routes_full_local_to_default_profile(self):
        d = self.router.route(_assessment(2, "Short"))
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)
        self.assertEqual(d.model_alias, "Ternary Bonsai 2 27B PTQ1_0")
        self.assertEqual(len(d.subrequests), 1)
        req = d.subrequests[0]
        self.assertEqual(req.difficulty, 2)
        self.assertEqual(req.length, LengthLevel.SHORT)
        self.assertTrue(req.task)
        self.assertTrue(req.objective)
        self.assertTrue(req.repository_context.summary)
        self.assertIsNone(d.assessment)  # route() does not attach the assessment

    def test_bounded_moderate_routes_full_local(self):
        d = self.router.route(_assessment(3, "Medium"))
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_bounded_trivial_routes_full_local(self):
        d = self.router.route(_assessment(1, "Short"))
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_full_local_reason_is_concise_and_model_named(self):
        d = self.router.route(_assessment(2, "Short"))
        self.assertIn("Ternary Bonsai 2 27B PTQ1_0", d.reason)
        self.assertIn("eligible", d.reason)


class TestPolicyRouterKeepInCodex(unittest.TestCase):
    def setUp(self):
        self.router = PolicyRouter()

    def test_hard_stays_in_codex_by_default(self):
        d = self.router.route(_assessment(4, "Short"))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertEqual(d.subrequests, [])
        self.assertIn("keep in Codex", d.reason)

    def test_expert_stays_in_codex_by_default(self):
        d = self.router.route(_assessment(5, "Short"))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)

    def test_high_risk_flags_force_codex_even_when_small(self):
        d = self.router.route(_assessment(1, "Short", [RiskFlag.SECURITY_SENSITIVE]))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertIn("security_sensitive", d.reason)

    def test_no_model_loaded_keeps_in_codex(self):
        d = self.router.route(_assessment(2, "Short"), loaded_model=_loaded_model(loaded=False))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertIn("no model loaded", d.reason)

    def test_unknown_loaded_model_capability_degrades_conservatively(self):
        d = self.router.route(
            _assessment(2, "Short"),
            loaded_model=ModelMetadata(id="Some Other Model", display_name="Some Other Model", loaded=True),
        )
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertIn("capability unknown", d.reason)

    def test_auto_handoff_disabled_keeps_in_codex(self):
        d = self.router.route(_assessment(2, "Short"),
                              config=OffloaderConfig(auto_handoff_enabled=False))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertIn("disabled", d.reason)

    def test_validation_failures_at_limit_keep_in_codex(self):
        d = self.router.route(_assessment(2, "Short"), validation_failures=2)
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertIn("validation failed 2", d.reason)

    def test_one_validation_failure_still_full_local(self):
        d = self.router.route(_assessment(2, "Short"), validation_failures=1)
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_context_over_budget_keeps_in_codex(self):
        d = self.router.route(_assessment(2, "Short"), context_tokens=20000)
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertIn("exceeds", d.reason)

    def test_context_within_budget_full_local(self):
        d = self.router.route(_assessment(2, "Short"), context_tokens=8192)
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_max_difficulty_full_handoff_caps_eligibility(self):
        d = self.router.route(_assessment(3, "Short"),
                              config=OffloaderConfig(max_difficulty_full_handoff=2))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)


class TestPolicyRouterModelProfile(unittest.TestCase):
    def setUp(self):
        self.router = PolicyRouter()

    def test_loaded_matching_model_used_as_alias(self):
        d = self.router.route(_assessment(2, "Short"), loaded_model=_loaded_model())
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)
        self.assertEqual(d.model_alias, "0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0")
        self.assertIn("matches profile", d.reason)

    def test_custom_profile_name_used_when_model_not_detected(self):
        cfg = OffloaderConfig(model_profile=ModelProfile(name="Other Local Model"))
        d = self.router.route(_assessment(2, "Short"), config=cfg)
        self.assertEqual(d.model_alias, "Other Local Model")

    def test_custom_profile_enables_difficulty_4_full_handoff(self):
        cfg = OffloaderConfig(model_profile=ModelProfile(use_for_difficulty=[1, 2, 3, 4]))
        d = self.router.route(_assessment(4, "Short"), config=cfg)
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_custom_profile_max_default_length(self):
        cfg = OffloaderConfig(model_profile=ModelProfile(max_default_length=LengthLevel.LONG))
        d = self.router.route(_assessment(3, "Long"), config=cfg)
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_loaded_model_matching_custom_profile(self):
        cfg = OffloaderConfig(model_profile=ModelProfile(name="Other Local Model"),
                              local_model_match="Other Local Model")
        d = self.router.route(_assessment(2, "Short"), config=cfg,
                              loaded_model=_loaded_model("Other Local Model"))
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)
        self.assertEqual(d.model_alias, "Other Local Model")


class TestPolicyRouterPartial(unittest.TestCase):
    def setUp(self):
        self.router = PolicyRouter()

    def test_long_scope_moderate_routes_partial(self):
        d = self.router.route(_assessment(3, "Long"))
        self.assertEqual(d.kind, RoutingDecisionKind.PARTIAL_LOCAL)
        self.assertGreaterEqual(len(d.subrequests), 2)
        for req in d.subrequests:
            self.assertEqual(req.length, LengthLevel.SHORT)
            self.assertGreaterEqual(req.difficulty, 1)
            self.assertLessEqual(req.difficulty, 3)
            self.assertTrue(req.task)
            self.assertTrue(req.objective)

    def test_extended_scope_easy_routes_partial(self):
        d = self.router.route(_assessment(1, "Extended"))
        self.assertEqual(d.kind, RoutingDecisionKind.PARTIAL_LOCAL)

    def test_partial_subrequests_carry_original_task_text(self):
        d = self.router.route(_assessment(3, "Long"), task_description="Refactor the parser module")
        for req in d.subrequests:
            self.assertIn("parser", req.task)
        self.assertIn("Bounded subtask", d.subrequests[0].repository_context.summary)

    def test_partial_subrequests_have_bounded_difficulty(self):
        d = self.router.route(
            _assessment(4, "Long"),
            config=OffloaderConfig(
                model_profile=ModelProfile(use_for_difficulty=[1, 2, 3, 4])
            ),
        )
        for req in d.subrequests:
            self.assertLessEqual(req.difficulty, 3)


class TestPolicyRouterDryRunAndExplain(unittest.TestCase):
    def setUp(self):
        self.router = PolicyRouter()

    def test_dry_run_flag_on_decision(self):
        d = self.router.route(_assessment(2, "Short"), config=OffloaderConfig(dry_run=True))
        self.assertTrue(d.dry_run)
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)

    def test_explain_contains_key_fields(self):
        d = self.router.route(_assessment(2, "Short"))
        line = d.explain()
        self.assertIn("kind=full_local", line)
        self.assertIn("model=Ternary Bonsai 2 27B PTQ1_0", line)
        self.assertIn("difficulty=2/Easy", line)
        self.assertIn("length=Short", line)
        self.assertIn("subrequests=1", line)
        self.assertNotIn("dry-run", line)

    def test_explain_marked_in_dry_run(self):
        d = self.router.route(_assessment(2, "Short"), config=OffloaderConfig(dry_run=True))
        self.assertIn("dry-run", d.explain())

    def test_explain_keep_in_codex(self):
        d = self.router.route(_assessment(5, "Short"))
        line = d.explain()
        self.assertIn("kind=keep_in_codex", line)
        self.assertNotIn("subrequests", line)


class TestPolicyRouterPlan(unittest.TestCase):
    def setUp(self):
        self.router = PolicyRouter()

    def test_plan_combines_assess_and_route_and_attaches_assessment(self):
        d = self.router.plan("Fix a typo in a single file")
        self.assertEqual(d.kind, RoutingDecisionKind.FULL_LOCAL)
        self.assertIsNotNone(d.assessment)
        self.assertEqual(d.assessment.difficulty, 1)
        self.assertEqual(d.assessment.length, LengthLevel.SHORT)

    def test_plan_with_matching_loaded_model(self):
        d = self.router.plan("Fix a typo in a single file", loaded_model=_loaded_model())
        self.assertEqual(d.model_alias, "0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0")

    def test_plan_with_auto_handoff_disabled(self):
        d = self.router.plan("Fix a typo in a single file",
                             config=OffloaderConfig(auto_handoff_enabled=False))
        self.assertEqual(d.kind, RoutingDecisionKind.KEEP_IN_CODEX)


if __name__ == "__main__":
    unittest.main()
