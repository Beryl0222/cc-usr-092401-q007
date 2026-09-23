"""领域核心规则测试。

覆盖三类必须能逐人说明的情形：
1. 同一违法行为的三人举报（最先有效 / 独立关键 / 重复）；
2. 无罚没款结案（定额奖励、匿名领取码、内部举报加成）；
3. 跨规则生效日的行政复议（按旧规则重算、追加决定、追回、旧结论保留）。

并验证身份隔离、职责分离、百万封顶与二十万会签线。
"""

import json
import unittest

import reward_center as rc
from reward_center import (
    RewardCenter, DomainError, PermissionDenied, InvalidStateError,
    CONTRIBUTION_FIRST, CONTRIBUTION_KEY, CONTRIBUTION_DUPLICATE,
    ROLE_INTAKE as INTAKE, ROLE_HANDLER as HANDLER,
    ROLE_REVIEWER as REVIEWER, ROLE_FINANCE as FINANCE,
    ROLE_PAYER as PAYER, ROLE_AUDITOR as AUDITOR,
)

TODAY = "2026-09-22"


def make_center():
    return RewardCenter(today=lambda: TODAY)


def intake(center, category, facts, day, identity=None, insider=False):
    alias, case_id, code = center.intake_report(
        "intake-1", INTAKE, category, facts, received_at=day,
        identity=identity, is_insider=insider)
    return alias, case_id, code


def settle(center, case_id, assessments, handler="handler-1",
           reviewer="reviewer-1", finance="finance-1"):
    """认定 + 建议 + 审核 + 必要时会签，返回 {alias: decision}。"""
    center.assess_contributions(case_id, assessments, "intake-1", INTAKE)
    ids = center.propose_rewards(case_id, handler, HANDLER)
    result = {}
    for did in ids:
        d = center.decisions[did]
        center.approve_decision(did, reviewer, REVIEWER)
        d = center.decisions[did]
        if d["needs_cosign"]:
            center.cosign_decision(did, finance, FINANCE)
        result[d["alias"]] = center.decisions[did]
    return result


class IdentityIsolationTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.alias, self.case, _ = intake(
            self.c, "食品药品安全", ["使用过期原料"], "2026-02-01",
            identity={"name": "张三", "id_card": "11010119900101001X"})

    def test_business_records_only_carry_alias(self):
        report = self.c.reports[self.alias]
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("张三", serialized)
        self.assertNotIn("110101", serialized)
        self.assertTrue(self.c.vault.has(self.alias))

    def test_handler_only_sees_alias_views(self):
        self.c.close_case(self.case, 100_000)
        self.c.enter_reward_stage(self.case)
        views = [
            json.dumps(self.c.case_file_for_handler(self.case, "h", HANDLER),
                       ensure_ascii=False),
            json.dumps(self.c.public_case_material(self.case), ensure_ascii=False),
            json.dumps(self.c.ordinary_case_log(self.case), ensure_ascii=False),
            json.dumps(self.c.explain_case(self.case), ensure_ascii=False),
        ]
        for view in views:
            self.assertNotIn("张三", view)
            self.assertIn(self.alias, view)

    def test_identity_reveal_is_role_gated_and_logged(self):
        with self.assertRaises(PermissionDenied):
            self.c.reveal_identity(self.alias, "h-1", HANDLER, "办案需要")
        identity = self.c.reveal_identity(self.alias, "intake-1", INTAKE,
                                          "核实联系方式")
        self.assertEqual(identity["name"], "张三")
        log = self.c.identity_access_log(AUDITOR)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["alias"], self.alias)
        self.assertNotIn("张三", json.dumps(log, ensure_ascii=False))
        with self.assertRaises(PermissionDenied):
            self.c.identity_access_log(HANDLER)


class ThreeReportersTest(unittest.TestCase):
    """同一违法行为三人举报：先后顺序与独立关键贡献。"""

    def setUp(self):
        self.c = make_center()
        self.a1, self.case, self.code1 = intake(
            self.c, "食品药品安全", ["事实A：使用过期原料"], "2026-02-01")
        self.a2, case2, _ = intake(
            self.c, "食品药品安全", ["事实A：使用过期原料"], "2026-02-03")
        self.a3, case3, self.code3 = intake(
            self.c, "食品药品安全", ["事实B：伪造检验报告"], "2026-02-10")
        self.assertEqual(case2, self.case)
        self.assertEqual(case3, self.case)
        self.c.close_case(self.case, 5_000_000, closed_at="2026-03-01")
        self.c.enter_reward_stage(self.case, entered_at="2026-03-05")

    def _assess(self):
        return self.c.assess_contributions(self.case, [
            {"alias": self.a1, "grade": 1, "new_facts": ["事实A：使用过期原料"]},
            {"alias": self.a2, "grade": 1, "duplicate": True},
            {"alias": self.a3, "grade": 2,
             "new_facts": ["事实B：伪造检验报告"], "key_contribution": True},
        ], "intake-1", INTAKE)

    def test_contribution_ordering(self):
        result = self._assess()
        self.assertEqual(result[self.a1], CONTRIBUTION_FIRST)
        self.assertEqual(result[self.a2], CONTRIBUTION_DUPLICATE)
        self.assertEqual(result[self.a3], CONTRIBUTION_KEY)
        self.assertEqual(self.c.reports[self.a2]["duplicate_of"], self.a1)

    def test_amounts_cosign_and_per_person_explanation(self):
        self._assess()
        decisions = settle(self.c, self.case, [
            {"alias": self.a1, "grade": 1},
            {"alias": self.a2, "grade": 1, "duplicate": True},
            {"alias": self.a3, "grade": 2, "key_contribution": True},
        ])
        # 500 万罚没 × 6% = 30 万；500 万 × 4% = 20 万，均达到会签线
        self.assertEqual(decisions[self.a1]["amount"], 300_000)
        self.assertTrue(decisions[self.a1]["needs_cosign"])
        self.assertEqual(decisions[self.a3]["amount"], 200_000)
        self.assertTrue(decisions[self.a3]["needs_cosign"])
        self.assertNotIn(self.a2, decisions)

        # 逐人说明
        explained = {r["alias"]: r for r in
                     self.c.explain_case(self.case)["reporters"]}
        order = [r["alias"] for r in
                 self.c.explain_case(self.case)["reporters"]]
        self.assertEqual(order, [self.a1, self.a2, self.a3])

        p1, p2, p3 = explained[self.a1], explained[self.a2], explained[self.a3]
        self.assertTrue(p1["eligible"])
        self.assertIn("最先有效贡献", p1["eligibility"])
        self.assertEqual(p1["effective_amount"], 300_000)
        self.assertEqual(p1["pending_approvals"], [])

        self.assertFalse(p2["eligible"])
        self.assertIn("重复举报", p2["eligibility"])
        self.assertIsNone(p2["current_decision"])
        self.assertEqual(p2["pending_approvals"], [])
        self.assertEqual(p2["paid_total"], 0)

        self.assertTrue(p3["eligible"])
        self.assertIn("独立关键贡献", p3["eligibility"])
        self.assertEqual(p3["grade"], 2)

    def test_pending_approvals_listed_per_person(self):
        self._assess()
        ids = self.c.propose_rewards(self.case, "handler-1", HANDLER)
        by_alias = {self.c.decisions[i]["alias"]: i for i in ids}
        explained = {r["alias"]: r for r in
                     self.c.explain_case(self.case)["reporters"]}
        self.assertEqual(
            [p["stage"] for p in explained[self.a1]["pending_approvals"]],
            ["奖励审核"])
        # 审核通过后、会签前：待办转为财政会签
        self.c.approve_decision(by_alias[self.a1], "reviewer-1", REVIEWER)
        explained = {r["alias"]: r for r in
                     self.c.explain_case(self.case)["reporters"]}
        self.assertEqual(
            [p["stage"] for p in explained[self.a1]["pending_approvals"]],
            ["财政会签"])
        self.assertEqual(explained[self.a1]["paid_total"], 0)

    def test_payments_tracked_per_person(self):
        self._assess()
        decisions = settle(self.c, self.case, [
            {"alias": self.a1, "grade": 1},
            {"alias": self.a2, "grade": 1, "duplicate": True},
            {"alias": self.a3, "grade": 2, "key_contribution": True},
        ])
        self.c.pay_decision(decisions[self.a1]["decision_id"], "payer-1",
                            PAYER, claim_code=self.code1)
        self.c.pay_decision(decisions[self.a3]["decision_id"], "payer-1",
                            PAYER, claim_code=self.code3)
        explained = {r["alias"]: r for r in
                     self.c.explain_case(self.case)["reporters"]}
        self.assertEqual(explained[self.a1]["paid_total"], 300_000)
        self.assertEqual(explained[self.a3]["paid_total"], 200_000)
        self.assertEqual(explained[self.a2]["paid_total"], 0)


class NoPenaltyCaseTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.anon, self.case, self.code = intake(
            self.c, "广告违法", ["发布违法医疗广告"], "2026-04-01")
        self.insider, _, _ = intake(
            self.c, "广告违法", ["提供广告投放后台记录"], "2026-04-03",
            identity={"name": "李四"}, insider=True)
        self.c.close_case(self.case, 0, closed_at="2026-05-01")
        self.c.enter_reward_stage(self.case, entered_at="2026-05-05")
        self.decisions = settle(self.c, self.case, [
            {"alias": self.anon, "grade": 3,
             "new_facts": ["发布违法医疗广告"]},
            {"alias": self.insider, "grade": 3,
             "new_facts": ["提供广告投放后台记录"], "key_contribution": True},
        ])

    def test_fixed_amounts_and_insider_bonus(self):
        # 2026 版三级举报无罚没款定额 3000；内部举报 ×1.5 = 4500
        self.assertEqual(self.decisions[self.anon]["amount"], 3000)
        self.assertEqual(self.decisions[self.insider]["amount"], 4500)
        self.assertFalse(self.decisions[self.anon]["needs_cosign"])

    def test_anonymous_requires_claim_code(self):
        did = self.decisions[self.anon]["decision_id"]
        with self.assertRaises(PermissionDenied):
            self.c.pay_decision(did, "payer-1", PAYER)
        with self.assertRaises(PermissionDenied):
            self.c.pay_decision(did, "payer-1", PAYER, claim_code="00000000")
        self.c.pay_decision(did, "payer-1", PAYER, claim_code=self.code)

    def test_explained_without_penalty(self):
        self.c.pay_decision(self.decisions[self.insider]["decision_id"],
                            "payer-1", PAYER)
        explained = {r["alias"]: r for r in
                     self.c.explain_case(self.case)["reporters"]}
        self.assertEqual(explained[self.insider]["paid_total"], 4500)
        case_view = self.c.explain_case(self.case)
        self.assertEqual(case_view["penalty_amount"], 0)
        self.assertEqual(case_view["rule_version"], "2026-01")


class CrossEffectiveDateReconsiderationTest(unittest.TestCase):
    """跨 2026-01-01 生效日的复议：仍按进入可奖励阶段时的旧规则重算。"""

    def setUp(self):
        self.c = make_center()
        self.alias, self.case, _ = intake(
            self.c, "食品药品安全", ["事实A"], "2025-11-01",
            identity={"name": "王五"})
        self.c.close_case(self.case, 5_000_000, closed_at="2025-12-10")
        self.c.enter_reward_stage(self.case, entered_at="2025-12-15")
        self.assertEqual(self.c.cases[self.case]["rule_version"], "2023-01")
        decisions = settle(self.c, self.case, [
            {"alias": self.alias, "grade": 1, "new_facts": ["事实A"]}])
        self.did = decisions[self.alias]["decision_id"]
        # 2023 版一级比例 5%：500 万 × 5% = 25 万（达到会签线，已会签）
        self.assertEqual(self.c.decisions[self.did]["amount"], 250_000)
        self.c.pay_decision(self.did, "payer-1", PAYER)

    def test_reconsideration_recomputes_under_old_rules_and_claws_back(self):
        adj_id = self.c.adjust_decision(
            self.did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=3_000_000, changed_at="2026-03-01")
        adj = self.c.adjustments[adj_id]
        # 即便复议发生在 2026 年，仍按 2023 版：300 万 × 5% = 15 万
        self.assertEqual(adj["new_amount"], 150_000)
        self.assertEqual(adj["delta"], -100_000)
        self.assertEqual(adj["rule_version"], "2023-01")
        self.assertFalse(adj["needs_cosign"])  # 15 万不再需要会签

        # 审核期间：逐人说明显示在途调整，且不得支付
        explained = self.c.explain_case(self.case)["reporters"][0]
        self.assertEqual(
            [p["stage"] for p in explained["pending_approvals"]],
            ["调整审核"])
        with self.assertRaises(InvalidStateError):
            self.c.pay_decision(self.did, "payer-1", PAYER)

        self.c.review_adjustment(adj_id, "reviewer-2", REVIEWER)
        self.assertEqual(self.c.adjustments[adj_id]["status"], "已生效")

        explained = self.c.explain_case(self.case)["reporters"][0]
        self.assertEqual(explained["effective_amount"], 150_000)
        self.assertEqual(explained["paid_total"], 150_000)  # 25 万 - 10 万追回
        self.assertEqual(len(explained["adjustments"]), 1)
        self.assertEqual(explained["adjustments"][0]["kind_label"],
                         "行政复议变化")
        self.assertEqual(explained["pending_approvals"], [])

        # 旧结论继续保留：原决定仍是 25 万，且记录追加决定沿革
        self.assertEqual(self.c.decisions[self.did]["amount"], 250_000)
        self.assertEqual(self.c.decisions[self.did]["superseded_by"], adj_id)

    def test_rejected_adjustment_keeps_original_conclusion(self):
        adj_id = self.c.adjust_decision(
            self.did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=3_000_000, changed_at="2026-03-01")
        self.c.review_adjustment(adj_id, "reviewer-2", REVIEWER, approve=False)
        explained = self.c.explain_case(self.case)["reporters"][0]
        self.assertEqual(explained["effective_amount"], 250_000)
        self.assertEqual(explained["paid_total"], 250_000)
        self.assertEqual(explained["adjustments"][0]["status"], "已驳回")
        # 驳回后可以再次发起调整
        again = self.c.adjust_decision(
            self.did, "judgment", "handler-1", HANDLER,
            new_penalty_amount=4_000_000, changed_at="2026-04-01")
        self.assertTrue(again)


class SeparationOfDutiesAndRulesTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.alias, self.case, _ = intake(
            self.c, "食品药品安全", ["事实A"], "2026-02-01")
        self.c.close_case(self.case, 5_000_000)
        self.c.enter_reward_stage(self.case)
        self.c.assess_contributions(self.case, [
            {"alias": self.alias, "grade": 1, "new_facts": ["事实A"]}
        ], "intake-1", INTAKE)
        self.did = self.c.propose_rewards(
            self.case, "handler-1", HANDLER)[0]

    def test_handler_cannot_approve_own_proposal(self):
        with self.assertRaises(PermissionDenied):
            self.c.approve_decision(self.did, "handler-1", REVIEWER)
        with self.assertRaises(PermissionDenied):
            self.c.approve_decision(self.did, "handler-1", HANDLER)

    def test_cosigner_cannot_be_reviewer_or_proposer(self):
        self.c.approve_decision(self.did, "reviewer-1", REVIEWER)
        with self.assertRaises(PermissionDenied):
            self.c.cosign_decision(self.did, "reviewer-1", FINANCE)

    def test_only_handler_proposes(self):
        with self.assertRaises(PermissionDenied):
            self.c.propose_rewards(self.case, "reviewer-1", REVIEWER)

    def test_intake_only_roles(self):
        with self.assertRaises(PermissionDenied):
            self.c.intake_report("h", HANDLER, "广告违法", ["x"])
        with self.assertRaises(PermissionDenied):
            self.c.assess_contributions(self.case, [], "h", HANDLER)


class RuleEngineTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.rule = self.c.rule_version_on(TODAY)

    def test_cap_at_one_million(self):
        self.assertEqual(
            self.c.compute_amount(self.rule, 1, 100_000_000, False, 1.0),
            1_000_000)

    def test_grade_floor(self):
        # 罚没极少时适用等级最低保障：2026 版三级 2000
        self.assertEqual(
            self.c.compute_amount(self.rule, 3, 10_000, False, 1.0), 2000)

    def test_insider_multiplier(self):
        self.assertEqual(
            self.c.compute_amount(self.rule, 1, 100_000, True, 1.0), 9_000)
        # 严重度系数作用于罚没比例（金额高于等级保底时不被保底覆盖）
        self.assertEqual(
            self.c.compute_amount(self.rule, 1, 200_000, False, 0.7), 8_400)

    def test_threshold_boundary(self):
        # 100 万 ×2%（三级）= 2 万；500 万 ×4% = 20 万，恰好达到会签线
        self.assertEqual(
            self.c.compute_amount(self.rule, 2, 5_000_000, False, 1.0),
            200_000)
        self.assertTrue(200_000 >= self.rule["cosign_threshold"])
        self.assertFalse(199_999 >= self.rule["cosign_threshold"])

    def test_versioned_rules(self):
        self.assertEqual(self.c.rule_version_on("2025-12-31")["version"],
                         "2023-01")
        self.assertEqual(self.c.rule_version_on("2026-01-01")["version"],
                         "2026-01")
        with self.assertRaises(DomainError):
            self.c.rule_version_on("2022-12-31")


class WithdrawalAndDuplicateAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.alias, self.case, _ = intake(
            self.c, "价格违法", ["事实A"], "2026-02-01",
            identity={"name": "赵六"})
        self.c.close_case(self.case, 1_000_000)
        self.c.enter_reward_stage(self.case)
        self.c.assess_contributions(self.case, [
            {"alias": self.alias, "grade": 2, "new_facts": ["事实A"]}
        ], "intake-1", INTAKE)
        # 100 万 ×4%×0.7 = 2.8 万，无需会签
        self.did = self.c.propose_rewards(self.case, "handler-1", HANDLER)[0]
        self.c.approve_decision(self.did, "reviewer-1", REVIEWER)
        self.c.pay_decision(self.did, "payer-1", PAYER)
        self.assertEqual(self.c.decisions[self.did]["amount"], 28_000)

    def test_withdrawal_after_payment_claws_back(self):
        adj_ids = self.c.withdraw_report(self.alias, "intake-1", INTAKE)
        self.assertEqual(len(adj_ids), 1)
        adj_id = adj_ids[0]
        self.c.review_adjustment(adj_id, "reviewer-1", REVIEWER)
        explained = self.c.explain_case(self.case)["reporters"][0]
        self.assertFalse(explained["eligible"])
        self.assertEqual(explained["effective_amount"], 0)
        self.assertEqual(explained["paid_total"], 0)  # 28000 - 28000 追回

    def test_withdrawal_before_approval_terminates_proposal(self):
        # 新举报 + 在途建议，随后撤回
        alias2, _, _ = intake(self.c, "价格违法", ["事实B（新）"], "2026-02-05")
        self.c.assess_contributions(self.case, [
            {"alias": self.alias, "grade": 2},
            {"alias": alias2, "grade": 3, "new_facts": ["事实B（新）"],
             "key_contribution": True},
        ], "intake-1", INTAKE)
        did2 = self.c.propose_rewards(self.case, "handler-1", HANDLER)[0]
        self.c.withdraw_report(alias2, "intake-1", INTAKE)
        self.assertEqual(self.c.decisions[did2]["status"], "已驳回")
        with self.assertRaises(InvalidStateError):
            self.c.approve_decision(did2, "reviewer-1", REVIEWER)

    def test_duplicate_confirmed_after_effective_resets_to_zero(self):
        adj_id = self.c.adjust_decision(
            self.did, "duplicate", "handler-1", HANDLER,
            reason="后续核查发现线索此前已由他人提供")
        self.assertEqual(self.c.adjustments[adj_id]["new_amount"], 0)
        self.c.review_adjustment(adj_id, "reviewer-1", REVIEWER)
        explained = self.c.explain_case(self.case)["reporters"][0]
        self.assertEqual(explained["effective_amount"], 0)
        self.assertEqual(explained["adjustments"][0]["kind_label"],
                         "重复举报确认")


class CommendationAndSupplementsTest(unittest.TestCase):
    def test_commendation_is_independent_of_money(self):
        c = make_center()
        alias, case, _ = intake(c, "广告违法", ["事实A"], "2026-02-01")
        c.grant_commendation(alias, "通报表扬", "积极提供线索",
                             "intake-1", INTAKE)
        c.close_case(case, 0)
        c.enter_reward_stage(case)
        c.assess_contributions(case, [
            {"alias": alias, "grade": 3, "new_facts": ["事实A"]}
        ], "intake-1", INTAKE)
        did = c.propose_rewards(case, "handler-1", HANDLER)[0]
        c.approve_decision(did, "reviewer-1", REVIEWER)
        explained = c.explain_case(case)["reporters"][0]
        self.assertEqual(explained["commendations"][0]["level"], "通报表扬")
        with self.assertRaises(PermissionDenied):
            c.grant_commendation(alias, "荣誉证书", "", "h", HANDLER)
        with self.assertRaises(DomainError):
            c.grant_commendation(alias, "奖状", "", "intake-1", INTAKE)

    def test_supplement_feeds_case_file(self):
        c = make_center()
        alias, case, _ = intake(c, "产品质量", ["事实A"], "2026-02-01")
        c.add_supplement(alias, ["补充证据1", "补充证据2"], added_at="2026-02-08")
        view = c.case_file_for_handler(case, "h", HANDLER)
        self.assertEqual(view["reports"][0]["supplements"][0]["facts"],
                         ["补充证据1", "补充证据2"])
        self.assertNotIn("identity", json.dumps(view, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
