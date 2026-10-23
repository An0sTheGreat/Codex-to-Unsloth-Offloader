"""Tests for the routing layer (bridge.routing).

Phase 1 defines the Router ABC and RoutingDecision. This module tests the
interface contract: that Router is abstract, that a concrete router can
combine assess() + route() via plan(), and that RoutingDecision carries the
right subrequests per kind.

A small in-test router is used to exercise the interface without depending on
the Phase 3/4 classifiers/policy.
"""

import unittest

from bridge.schemas import (
    Execution,
    HandoffRequest,
    LengthLevel,
    OffloadDecision,
    RepositoryContext,
    RoutingDecision,
    RoutingDecisionKind,
    TaskAssessment,
)
from bridge.routing import Router


def _assessment(offload, execution, difficulty=2, label="Easy", length="Short"):
    return TaskAssessment(
        difficulty=difficulty,
        difficulty_label=label,
        length=length,
        length_note="one file",
        offload=offload,
        offload_note="reason",
        execution=execution,
        reason="test",
    )


class TestRoutingDecision(unittest.TestCase):
    def test_full_local_has_one_subrequest(self):
        req = HandoffRequest(
            task="t", objective="o", difficulty=2, length="Short",
            repository_context=RepositoryContext(summary="x"),
        )
        rd = RoutingDecision(
            kind=RoutingDecisionKind.FULL_LOCAL,
            model_alias="tb27b",
            reason="small safe",
            subrequests=[req],
        )
        self.assertEqual(rd.kind, RoutingDecisionKind.FULL_LOCAL)
        self.assertEqual(len(rd.subrequests), 1)
        self.assertEqual(rd.model_alias, "tb27b")

    def test_keep_in_codex_has_no_subrequests(self):
        rd = RoutingDecision(kind=RoutingDecisionKind.KEEP_IN_CODEX, reason="big")
        self.assertEqual(rd.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertEqual(rd.subrequests, [])

    def test_partial_local_has_multiple(self):
        r1 = HandoffRequest(task="a", objective="o", difficulty=2,
                             length="Short", repository_context=RepositoryContext(summary="x"))
        r2 = HandoffRequest(task="b", objective="o", difficulty=2,
                             length="Short", repository_context=RepositoryContext(summary="x"))
        rd = RoutingDecision(kind=RoutingDecisionKind.PARTIAL_LOCAL,
                             model_alias="tb27b", reason="medium", subrequests=[r1, r2])
        self.assertEqual(rd.kind, RoutingDecisionKind.PARTIAL_LOCAL)
        self.assertEqual(len(rd.subrequests), 2)


class TestRouterABC(unittest.TestCase):
    def test_router_is_abstract(self):
        with self.assertRaises(TypeError):
            Router()

    def test_router_has_abstract_methods(self):
        self.assertTrue(hasattr(Router, "assess"))
        self.assertTrue(hasattr(Router, "route"))
        self.assertTrue(hasattr(Router, "plan"))


class TestConcreteRouter(unittest.TestCase):
    """A minimal concrete router to verify the interface contract in action."""

    def test_plan_combines_assess_and_route(self):
        class DummyRouter(Router):
            def __init__(self):
                self.assess_calls = 0
                self.route_calls = 0

            def assess(self, task_description, *, context=None):
                self.assess_calls += 1
                return _assessment(OffloadDecision.YES, Execution.LOCAL_HANDOFF)

            def route(self, assessment, *, config=None):
                self.route_calls += 1
                req = HandoffRequest(
                    task="from route", objective="o", difficulty=2,
                    length="Short", repository_context=RepositoryContext(summary="x"),
                )
                return RoutingDecision(
                    kind=RoutingDecisionKind.FULL_LOCAL,
                    model_alias="tb27b",
                    reason="dummy",
                    subrequests=[req],
                    assessment=assessment,
                )

        r = DummyRouter()
        decision = r.plan("some task")
        self.assertEqual(r.assess_calls, 1)
        self.assertEqual(r.route_calls, 1)
        self.assertEqual(decision.kind, RoutingDecisionKind.FULL_LOCAL)
        # plan() must attach the assessment to the decision
        self.assertIsNotNone(decision.assessment)
        self.assertEqual(decision.assessment.difficulty, 2)

    def test_route_can_take_user_assessment(self):
        class DummyRouter(Router):
            def assess(self, task_description, *, context=None):
                raise NotImplementedError("not used in this test")

            def route(self, assessment, *, config=None):
                if assessment.offload is OffloadDecision.NO:
                    return RoutingDecision(kind=RoutingDecisionKind.KEEP_IN_CODEX, reason="keep")
                return RoutingDecision(kind=RoutingDecisionKind.FULL_LOCAL,
                                       model_alias="tb27b",
                                       reason="local",
                                       subrequests=[HandoffRequest(
                                           task="t", objective="o", difficulty=2,
                                           length="Short",
                                           repository_context=RepositoryContext(summary="x"),
                                       )])

        r = DummyRouter()
        decision = r.route(_assessment(OffloadDecision.NO, Execution.KEEP_IN_CODEX))
        self.assertEqual(decision.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertEqual(decision.subrequests, [])


if __name__ == "__main__":
    unittest.main()
