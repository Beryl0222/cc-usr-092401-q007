"""支付链修复专项测试。

覆盖：
1. 屏障式并发：并发拆付合计绝不超过已生效决定上限；
2. 部分支付：分笔支付、超付/零额拒绝、默认结清；
3. 请求重放：稳定业务标识原样重试返回首次凭证，同标识异内容拒绝，
   失败不占用标识；
4. 调整竞态：在途调整与支付并发时按“先到先得、可解释”的顺序裁决，
   降额只形成应追回差额，升额只开放新增余额，历史支付不可撤销改写；
5. 领取人绑定、领取码不进日志/响应、逐人说明可核对。
"""

import json
import threading
import unittest

from reward_center import (
    RewardCenter, DomainError, PermissionDenied, InvalidStateError,
    IdempotencyConflict, PaymentRejected,
    ROLE_INTAKE as INTAKE, ROLE_HANDLER as HANDLER,
    ROLE_REVIEWER as REVIEWER, ROLE_FINANCE as FINANCE,
    ROLE_PAYER as PAYER,
)

TODAY = "2026-09-22"


def make_center():
    return RewardCenter(today=lambda: TODAY)


def setup_effective(center, penalty=5_000_000, grade=1,
                    category="食品药品安全", anonymous=False, insider=False):
    """登记一起举报并把决定推进到“已生效”，金额 500 万×6%=30 万（已会签）。"""
    identity = None if anonymous else {"name": "举报人"}
    alias, case_id, code = center.intake_report(
        "intake-1", INTAKE, category, ["事实A：违法线索"],
        received_at="2026-02-01", identity=identity, is_insider=insider)
    center.close_case(case_id, penalty, closed_at="2026-03-01")
    center.enter_reward_stage(case_id, entered_at="2026-03-05")
    center.assess_contributions(case_id, [
        {"alias": alias, "grade": grade, "new_facts": ["事实A：违法线索"]}
    ], "intake-1", INTAKE)
    did = center.propose_rewards(case_id, "handler-1", HANDLER)[0]
    center.approve_decision(did, "reviewer-1", REVIEWER)
    if center.decisions[did]["needs_cosign"]:
        center.cosign_decision(did, "finance-1", FINANCE)
    return alias, case_id, did, code


def run_concurrently(targets):
    """以屏障同时放行多个目标，返回 [(result, error), ...]（按传入顺序）。"""
    barrier = threading.Barrier(len(targets))
    results = [None] * len(targets)

    def worker(idx, fn):
        barrier.wait(timeout=5)
        try:
            results[idx] = (fn(), None)
        except DomainError as exc:
            results[idx] = (None, exc)

    threads = [threading.Thread(target=worker, args=(i, fn))
               for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    return results


class BarrierConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.alias, self.case_id, self.did, _ = setup_effective(self.c)
        self.assertEqual(self.c.decisions[self.did]["amount"], 300_000)

    def test_two_concurrent_full_payments_only_one_succeeds(self):
        results = run_concurrently([
            lambda: self.c.pay_decision(self.did, "payer-1", PAYER,
                                        "req-1", amount=300_000),
            lambda: self.c.pay_decision(self.did, "payer-1", PAYER,
                                        "req-2", amount=300_000),
        ])
        oks = [r for r, e in results if r is not None]
        errs = [e for r, e in results if e is not None]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertIsInstance(errs[0], PaymentRejected)
        self.assertEqual(errs[0].error_code, "PAYMENT_REJECTED")
        self.assertEqual(
            sum(p["amount"] for p in self.c.payments), 300_000)

    def test_split_payments_under_contention_never_exceed_cap(self):
        # 6 笔 6 万争抢 30 万余额：恰好 5 笔成功、1 笔被拒
        results = run_concurrently([
            (lambda i=i: self.c.pay_decision(
                self.did, "payer-1", PAYER, f"req-split-{i}", amount=60_000))
            for i in range(6)
        ])
        oks = [r for r, e in results if r is not None]
        errs = [e for r, e in results if e is not None]
        self.assertEqual(len(oks), 5)
        self.assertEqual(len(errs), 1)
        self.assertIsInstance(errs[0], PaymentRejected)
        # 落账总额恰好等于上限，凭证号互不相同，标识互不相同
        paid = sum(p["amount"] for p in self.c.payments)
        self.assertEqual(paid, 300_000)
        self.assertEqual(len({p["payment_id"] for p in self.c.payments}), 5)
        self.assertEqual(len({p["request_id"] for p in self.c.payments}), 5)

        explained = self.c.explain_case(self.case_id)["reporters"][0]
        self.assertEqual(explained["paid_total"], 300_000)
        self.assertEqual(explained["remaining_amount"], 0)

    def test_loser_can_take_remaining_balance_afterwards(self):
        results = run_concurrently([
            lambda: self.c.pay_decision(self.did, "payer-1", PAYER,
                                        "req-a", amount=200_000),
            lambda: self.c.pay_decision(self.did, "payer-1", PAYER,
                                        "req-b", amount=200_000),
        ])
        errs = [e for r, e in results if e is not None]
        self.assertEqual(len(errs), 1)
        # 失败的一方改用新标识领取剩余 10 万
        record = self.c.pay_decision(self.did, "payer-1", PAYER,
                                     "req-c", amount=100_000)
        self.assertEqual(record["amount"], 100_000)
        self.assertEqual(
            sum(p["amount"] for p in self.c.payments), 300_000)


class PartialPaymentTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.alias, self.case_id, self.did, _ = setup_effective(self.c)

    def test_partial_then_settle(self):
        r1 = self.c.pay_decision(self.did, "payer-1", PAYER,
                                 "p1", amount=100_000)
        r2 = self.c.pay_decision(self.did, "payer-1", PAYER,
                                 "p2", amount=100_000)
        explained = self.c.explain_case(self.case_id)["reporters"][0]
        self.assertEqual(explained["paid_total"], 200_000)
        self.assertEqual(explained["remaining_amount"], 100_000)
        # 不带金额即结清剩余
        r3 = self.c.pay_decision(self.did, "payer-1", PAYER, "p3")
        self.assertEqual(r3["amount"], 100_000)
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER, "p4", amount=1)

    def test_overpay_zero_and_negative_rejected(self):
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER,
                                "big", amount=300_001)
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER, "zero", amount=0)
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER,
                                "neg", amount=-100)
        # 失败不产生凭证、不占用余额
        self.assertEqual(self.c.payments, [])

    def test_request_id_is_mandatory(self):
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER, None)
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER, "   ")


class RequestReplayTest(unittest.TestCase):
    def setUp(self):
        self.c = make_center()
        self.alias, self.case_id, self.did, _ = setup_effective(self.c)
        self.first = self.c.pay_decision(self.did, "payer-1", PAYER,
                                         "stable-req-1", amount=100_000)

    def test_identical_retry_returns_first_voucher(self):
        replay = self.c.pay_decision(self.did, "payer-1", PAYER,
                                     "stable-req-1", amount=100_000)
        self.assertEqual(replay["payment_id"], self.first["payment_id"])
        self.assertEqual(replay["request_id"], "stable-req-1")
        self.assertEqual(replay["amount"], 100_000)
        # 只有一笔落账
        self.assertEqual(len(self.c.payments), 1)
        # 省略金额重放同样视为原样重试
        replay_default = self.c.pay_decision(self.did, "payer-1", PAYER,
                                             "stable-req-1")
        self.assertEqual(replay_default["payment_id"],
                         self.first["payment_id"])
        self.assertEqual(len(self.c.payments), 1)

    def test_same_id_different_amount_rejected(self):
        with self.assertRaises(IdempotencyConflict) as cm:
            self.c.pay_decision(self.did, "payer-1", PAYER,
                                "stable-req-1", amount=50_000)
        self.assertEqual(cm.exception.error_code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.c.payments), 1)

    def test_same_id_different_decision_or_payee_rejected(self):
        # 第二个领取人/决定：同标识跨决定复用必须拒绝
        alias2, _, did2, _ = setup_effective(self.c, category="产品质量")
        with self.assertRaises(IdempotencyConflict):
            self.c.pay_decision(did2, "payer-1", PAYER,
                                "stable-req-1", amount=100_000)
        with self.assertRaises(IdempotencyConflict):
            self.c.pay_decision(self.did, "payer-1", PAYER,
                                "stable-req-1", amount=100_000, alias=alias2)
        self.assertEqual(len(self.c.payments), 1)

    def test_failed_attempt_does_not_consume_request_id(self):
        # 超付失败后，同一标识以合法内容重试应当成功（标识只在成功时绑定）
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER,
                                "retry-me", amount=999_999)
        record = self.c.pay_decision(self.did, "payer-1", PAYER,
                                     "retry-me", amount=50_000)
        self.assertEqual(record["amount"], 50_000)

    def test_payee_binding_required(self):
        # 请求里显式带错领取人：拒绝，且不消耗新标识
        with self.assertRaises(PaymentRejected):
            self.c.pay_decision(self.did, "payer-1", PAYER,
                                "bound", amount=50_000, alias="RPT-9999")


class AdjustmentRaceTest(unittest.TestCase):
    def test_payment_blocked_while_adjustment_pending_and_down_adjustment_keeps_history(self):
        c = make_center()
        alias, case_id, did, _ = setup_effective(c)  # 30 万
        c.pay_decision(did, "payer-1", PAYER, "orig", amount=300_000)
        adj_id = c.adjust_decision(did, "duplicate", "handler-1", HANDLER,
                                   reason="核查确认重复")

        # 在途调整期间支付一律拒绝（409 类状态）
        with self.assertRaises(InvalidStateError):
            c.pay_decision(did, "payer-1", PAYER, "during-review")

        # 并发：4 个支付线程与调整审核线程同时放行
        def attempt_pay(i):
            return c.pay_decision(did, "payer-1", PAYER,
                                  f"race-down-{i}", amount=300_000)
        results = run_concurrently(
            [lambda: c.review_adjustment(adj_id, "reviewer-2", REVIEWER)]
            + [lambda i=i: attempt_pay(i) for i in range(4)])
        pay_results = results[1:]
        # 无论谁先谁后：在途则 InvalidState，生效后则 PaymentRejected，
        # 绝不能有一笔支付成功
        for _, err in pay_results:
            self.assertIsInstance(err, (InvalidStateError, PaymentRejected))
        self.assertEqual(len(c.payments), 1)

        explained = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(explained["effective_amount"], 0)
        self.assertEqual(explained["paid_total"], 300_000)      # 历史不撤销
        self.assertEqual(explained["clawback_due"], 300_000)    # 应追回差额
        self.assertEqual(explained["remaining_amount"], 0)
        self.assertEqual(
            [v["payment_id"] for v in explained["payment_vouchers"]],
            ["P-0001"])
        # 不存在任何负向“系统追回”凭证
        self.assertEqual(
            [p for p in c.payments if p["amount"] < 0], [])
        # 调整链完整可核对
        chain = explained["adjustment_chain"]
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["new_amount"], 0)
        self.assertEqual(chain[0]["paid_total_at_effect"], 300_000)
        self.assertEqual(chain[0]["clawback_due"], 300_000)
        self.assertEqual(chain[0]["open_balance"], 0)

    def test_up_adjustment_only_opens_new_balance(self):
        c = make_center()
        # 250 万罚没 ×6% = 15 万（无需会签）
        alias, case_id, did, _ = setup_effective(c, penalty=2_500_000)
        self.assertEqual(c.decisions[did]["amount"], 150_000)
        c.pay_decision(did, "payer-1", PAYER, "orig", amount=150_000)

        # 复议改判罚没 300 万：18 万，升额 3 万（仍低于会签线）
        adj_id = c.adjust_decision(
            did, "judgment", "handler-1", HANDLER,
            new_penalty_amount=3_000_000, changed_at="2026-04-01")
        adj = c.adjustments[adj_id]
        self.assertEqual(adj["new_amount"], 180_000)
        self.assertFalse(adj["needs_cosign"])
        with self.assertRaises(InvalidStateError):
            c.pay_decision(did, "payer-1", PAYER, "while-pending",
                           amount=30_000)

        # 审核线程与多个补付线程并发：开放余额只有 3 万，至多 1 笔成功
        def attempt_pay(i):
            return c.pay_decision(did, "payer-1", PAYER,
                                  f"race-up-{i}", amount=30_000)
        results = run_concurrently(
            [lambda: c.review_adjustment(adj_id, "reviewer-2", REVIEWER)]
            + [lambda i=i: attempt_pay(i) for i in range(4)])
        pay_results = results[1:]
        oks = [r for r, e in pay_results if r is not None]
        self.assertLessEqual(len(oks), 1)
        self.assertEqual(
            sum(p["amount"] for p in c.payments
                if p["request_id"] != "orig"),
            30_000 if oks else 0)

        explained = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(explained["effective_amount"], 180_000)
        self.assertEqual(explained["paid_total"], 180_000 if oks else 150_000)
        self.assertEqual(explained["clawback_due"], 0)
        self.assertEqual(explained["remaining_amount"], 0 if oks else 30_000)
        self.assertEqual(explained["adjustment_chain"][0]["open_balance"],
                         30_000)
        # 无论补付是否抢到，新增余额之外一分钱都不能多付
        if not oks:
            c.pay_decision(did, "payer-1", PAYER, "topup", amount=30_000)
        with self.assertRaises(PaymentRejected):
            c.pay_decision(did, "payer-1", PAYER, "over", amount=1)

    def test_withdraw_racing_payment_is_serialized_and_explainable(self):
        c = make_center()
        alias, case_id, did, _ = setup_effective(c)  # 30 万未付
        results = run_concurrently([
            lambda: c.pay_decision(did, "payer-1", PAYER,
                                   "withdraw-race", amount=300_000),
            lambda: c.withdraw_report(alias, "intake-1", INTAKE),
        ])
        pay_res, withdraw_res = results
        if pay_res[0] is not None:
            # 支付先落账：凭证不可撤销；撤回只生成待审的调减决定
            self.assertEqual(len(c.payments), 1)
            adj_ids = withdraw_res[0]
            self.assertEqual(len(adj_ids), 1)
            with self.assertRaises(InvalidStateError):
                c.pay_decision(did, "payer-1", PAYER, "second", amount=1)
            c.review_adjustment(adj_ids[0], "reviewer-2", REVIEWER)
            explained = c.explain_case(case_id)["reporters"][0]
            self.assertEqual(explained["paid_total"], 300_000)
            self.assertEqual(explained["clawback_due"], 300_000)
        else:
            # 撤回先到：支付被拒（在途调整/已撤回），无任何落账
            self.assertIsInstance(pay_res[1], InvalidStateError)
            self.assertEqual(c.payments, [])
            adj_ids = withdraw_res[0]
            c.review_adjustment(adj_ids[0], "reviewer-2", REVIEWER)
            explained = c.explain_case(case_id)["reporters"][0]
            self.assertEqual(explained["paid_total"], 0)
            self.assertEqual(explained["clawback_due"], 0)

    def test_rejected_adjustment_leaves_payment_cap_untouched(self):
        c = make_center()
        alias, case_id, did, _ = setup_effective(c)  # 30 万，未付
        adj_id = c.adjust_decision(did, "duplicate", "handler-1", HANDLER)
        c.review_adjustment(adj_id, "reviewer-2", REVIEWER, approve=False)
        # 驳回后余额仍是 30 万，可正常支付
        record = c.pay_decision(did, "payer-1", PAYER, "after-reject",
                                amount=300_000)
        self.assertEqual(record["amount"], 300_000)
        explained = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(explained["effective_amount"], 300_000)
        self.assertEqual(explained["remaining_amount"], 0)
        self.assertEqual(explained["adjustment_chain"][0]["status"], "已驳回")


class ClaimCodeAndExplainTest(unittest.TestCase):
    def test_claim_code_never_enters_logs_or_responses(self):
        c = make_center()
        alias, case_id, did, code = setup_effective(c, anonymous=True)
        record = c.pay_decision(did, "payer-1", PAYER, "anon-pay",
                                claim_code=code)
        # 凭证、逐人说明、办案视图、普通日志都不得出现领取码明文/密文
        views = [
            record,
            c.explain_case(case_id),
            c.case_file_for_handler(case_id, "h", HANDLER),
            {"events": c.ordinary_case_log(case_id)},
            c.public_case_material(case_id),
            list(c.payments),
            list(c.event_log),
        ]
        for view in views:
            text = json.dumps(view, ensure_ascii=False)
            self.assertNotIn(code, text)
            self.assertNotIn(c.reports[alias]["claim_code_hash"], text)
        # 但支付日志保留业务标识与凭证号，可核对
        pay_events = [e for e in c.event_log if e["event"] == "奖励支付"]
        self.assertEqual(pay_events[0]["request_id"], "anon-pay")
        self.assertTrue(pay_events[0]["payment_id"])

    def test_explained_ledger_reconciles(self):
        c = make_center()
        alias, case_id, did, _ = setup_effective(c)  # 30 万
        c.pay_decision(did, "payer-1", PAYER, "p1", amount=120_000)
        c.pay_decision(did, "payer-1", PAYER, "p2", amount=80_000)
        person = c.explain_case(case_id)["reporters"][0]
        # 累计支付 = 凭证明细之和
        self.assertEqual(
            person["paid_total"],
            sum(v["amount"] for v in person["payment_vouchers"]))
        self.assertEqual(person["paid_total"], 200_000)
        # 累计支付 + 剩余 = 已生效金额
        self.assertEqual(person["paid_total"] + person["remaining_amount"],
                         person["effective_amount"])
        self.assertEqual(person["remaining_amount"], 100_000)
        self.assertEqual(person["clawback_due"], 0)
        self.assertEqual(
            [v["request_id"] for v in person["payment_vouchers"]],
            ["p1", "p2"])

        # 降额后：累计支付 = 新生效金额 + 应追回
        adj_id = c.adjust_decision(
            did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=2_500_000, changed_at="2026-04-01")
        # 250 万 ×6% = 15 万
        c.review_adjustment(adj_id, "reviewer-2", REVIEWER)
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["effective_amount"], 150_000)
        self.assertEqual(person["paid_total"], 200_000)
        self.assertEqual(person["clawback_due"], 50_000)
        self.assertEqual(
            person["paid_total"],
            person["effective_amount"] + person["clawback_due"])

    def test_wrong_claim_code_does_not_bind_request_id(self):
        c = make_center()
        alias, case_id, did, code = setup_effective(c, anonymous=True)
        with self.assertRaises(PermissionDenied):
            c.pay_decision(did, "payer-1", PAYER, "anon-x",
                           claim_code="0" * 8)
        # 领取码纠正后同一标识仍可成功
        record = c.pay_decision(did, "payer-1", PAYER, "anon-x",
                                claim_code=code)
        self.assertEqual(record["request_id"], "anon-x")


if __name__ == "__main__":
    unittest.main()
