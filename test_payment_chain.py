"""支付链修复专项测试。

覆盖：
1. 屏障式并发：并发拆付/全额抢付不得突破已生效决定上限；同一稳定标识并发
   提交只落一笔账；
2. 部分支付：余额随逐笔支付递减，超额拒绝，案件说明可核对累计与剩余；
3. 请求重放：稳定业务标识绑定决定/金额/领取人，原样重试回放首次凭证，
   同标识异内容拒绝；
4. 调整竞态：支付与追加决定发起/撤回/规则调整并发时按临界区顺序裁决；
   升额只开放新增余额、降额只形成应追回差额，历史支付不撤销不改写；
5. 匿名领取码不得进入凭证、普通日志与错误信息；错误带稳定错误码。
"""

import json
import random
import threading
import time
import unittest

from reward_center import (
    RewardCenter, DomainError, InvalidStateError, IdempotencyConflict,
    PermissionDenied,
    ROLE_INTAKE as INTAKE, ROLE_HANDLER as HANDLER,
    ROLE_REVIEWER as REVIEWER, ROLE_FINANCE as FINANCE,
    ROLE_PAYER as PAYER,
)

TODAY = "2026-09-22"


def make_center():
    return RewardCenter(today=lambda: TODAY)


_UNSET = object()


def setup_decision(penalty=5_000_000, grade=1, category="食品药品安全",
                   identity=_UNSET, insider=False):
    """构造一个已生效（含必要会签）的奖励决定，返回中心与关键标识。

    默认构造实名决定（支付免领取码）；匿名场景显式传 identity=None。
    """
    if identity is _UNSET:
        identity = {"name": "测试举报人", "contact": "010-00000000"}
    c = make_center()
    alias, case_id, code = c.intake_report(
        "intake-1", INTAKE, category, ["事实A：核心违法线索"],
        received_at="2026-02-01", identity=identity, is_insider=insider)
    c.close_case(case_id, penalty, closed_at="2026-03-01")
    c.enter_reward_stage(case_id, entered_at="2026-03-05")
    c.assess_contributions(case_id, [
        {"alias": alias, "grade": grade, "new_facts": ["事实A：核心违法线索"]}
    ], "intake-1", INTAKE)
    did = c.propose_rewards(case_id, "handler-1", HANDLER)[0]
    c.approve_decision(did, "reviewer-1", REVIEWER)
    if c.decisions[did]["needs_cosign"]:
        c.cosign_decision(did, "finance-1", FINANCE)
    return c, alias, case_id, did, code


def setup_two_decisions(penalty=5_000_000):
    """同一案件下两名具备资格举报人的两个生效决定。"""
    c = make_center()
    a1, case_id, _ = c.intake_report(
        "intake-1", INTAKE, "食品药品安全", ["事实A"],
        received_at="2026-02-01", identity={"name": "举报人甲"})
    a2, _, _ = c.intake_report(
        "intake-1", INTAKE, "食品药品安全", ["事实B"],
        received_at="2026-02-03", identity={"name": "举报人乙"})
    c.close_case(case_id, penalty, closed_at="2026-03-01")
    c.enter_reward_stage(case_id, entered_at="2026-03-05")
    c.assess_contributions(case_id, [
        {"alias": a1, "grade": 1, "new_facts": ["事实A"],
         "key_contribution": True},
        {"alias": a2, "grade": 2, "new_facts": ["事实B"],
         "key_contribution": True},
    ], "intake-1", INTAKE)
    ids = c.propose_rewards(case_id, "handler-1", HANDLER)
    for did in ids:
        c.approve_decision(did, "reviewer-1", REVIEWER)
        if c.decisions[did]["needs_cosign"]:
            c.cosign_decision(did, "finance-1", FINANCE)
    by_alias = {c.decisions[i]["alias"]: i for i in ids}
    return c, case_id, by_alias[a1], by_alias[a2], a1, a2


def manual_payments(c):
    return [p for p in c.payments if p.get("kind", "payment") == "payment"]


def clawbacks(c):
    return [p for p in c.payments if p.get("kind") == "clawback"]


class PartialPaymentTest(unittest.TestCase):
    def test_partial_payments_drain_remaining_balance(self):
        # 价格违法二级：100 万 ×4%×0.7 = 28000 元，无需会签
        c, alias, case_id, did, _ = setup_decision(
            penalty=1_000_000, grade=2, category="价格违法")
        self.assertEqual(c.decisions[did]["amount"], 28_000)

        r1 = c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                            request_id="req-1")
        r2 = c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                            request_id="req-2")
        self.assertEqual((r1["amount"], r2["amount"]), (10_000, 10_000))

        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["paid_total"], 20_000)
        self.assertEqual(person["paid_out_total"], 20_000)
        self.assertEqual(person["clawed_back_total"], 0)
        self.assertEqual(person["remaining_amount"], 8_000)
        self.assertEqual([p["request_id"] for p in person["payments"]],
                         ["req-1", "req-2"])

        # 超额部分支付被稳定错误码拒绝，且不落账
        with self.assertRaises(DomainError) as ctx:
            c.pay_decision(did, "payer-1", PAYER, amount=9_000,
                           request_id="req-3")
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_BALANCE")
        self.assertEqual(c._paid_amount(did), 20_000)

        # 刚好付清余额
        r3 = c.pay_decision(did, "payer-1", PAYER, amount=8_000,
                            request_id="req-3")
        self.assertEqual(r3["amount"], 8_000)
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["paid_total"], 28_000)
        self.assertEqual(person["remaining_amount"], 0)

        # 已无余额：再付一分钱也不行
        with self.assertRaises(DomainError) as ctx:
            c.pay_decision(did, "payer-1", PAYER, amount=1,
                           request_id="req-4")
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_BALANCE")

        # 案件级合计与逐人合计一致
        view = c.explain_case(case_id)
        self.assertEqual(view["paid_total"], 28_000)
        self.assertEqual(view["remaining_total"], 0)

    def test_default_amount_pays_full_remaining(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                       request_id="req-1")
        # 不带金额：取剩余全部
        rest = c.pay_decision(did, "payer-1", PAYER, request_id="req-2")
        self.assertEqual(rest["amount"], 18_000)


class IdempotentReplayTest(unittest.TestCase):
    def test_replay_returns_first_voucher_without_new_booking(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        first = c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                               request_id="stable-req-1")
        self.assertFalse(first["replayed"])
        before = len(manual_payments(c))

        # 网络重试：同标识同内容（金额显式一致）
        replay = c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                                request_id="stable-req-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["payment_id"], first["payment_id"])
        self.assertEqual(replay["request_id"], "stable-req-1")
        self.assertEqual(len(manual_payments(c)), before)  # 未重复落账
        self.assertEqual(c._paid_amount(did), 10_000)

        # 重试时省略金额同样视为原样重放
        replay2 = c.pay_decision(did, "payer-1", PAYER,
                                 request_id="stable-req-1")
        self.assertEqual(replay2["payment_id"], first["payment_id"])
        self.assertEqual(len(manual_payments(c)), before)

    def test_same_request_id_with_different_amount_rejected(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                       request_id="stable-req-1")
        with self.assertRaises(IdempotencyConflict) as ctx:
            c.pay_decision(did, "payer-1", PAYER, amount=5_000,
                           request_id="stable-req-1")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")
        # 冲突不产生新凭证、不动余额
        self.assertEqual(c._paid_amount(did), 10_000)
        self.assertEqual(len(manual_payments(c)), 1)

    def test_same_request_id_bound_to_other_decision_rejected(self):
        c, _, d1, d2, _, _ = setup_two_decisions()
        c.pay_decision(d1, "payer-1", PAYER, amount=10_000,
                       request_id="shared-req")
        with self.assertRaises(IdempotencyConflict):
            c.pay_decision(d2, "payer-1", PAYER, amount=10_000,
                           request_id="shared-req")

    def test_request_id_is_mandatory(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        for bad in (None, "", "   "):
            with self.assertRaises(DomainError) as ctx:
                c.pay_decision(did, "payer-1", PAYER, amount=1,
                               request_id=bad)
            self.assertEqual(ctx.exception.code,
                             "PAYMENT_REQUEST_ID_REQUIRED")

    def test_payee_binding_mismatch_rejected(self):
        c, alias, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                             category="价格违法")
        # 请求显式声明的领取人与决定绑定领取人不一致
        with self.assertRaises(DomainError) as ctx:
            c.pay_decision(did, "payer-1", PAYER, amount=1,
                           request_id="req-x", alias="RPT-9999")
        self.assertEqual(ctx.exception.code, "PAYEE_MISMATCH")
        # 一致（或省略）时正常
        ok = c.pay_decision(did, "payer-1", PAYER, amount=1,
                            request_id="req-x", alias=alias)
        self.assertEqual(ok["alias"], alias)

    def test_concurrent_same_request_id_books_once(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait()
                results.append(c.pay_decision(
                    did, "payer-1", PAYER, amount=28_000,
                    request_id="exactly-once"))
            except Exception as exc:  # noqa: BLE001 - 测试需要记录一切异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({r["payment_id"] for r in results},
                         {results[0]["payment_id"]})
        self.assertEqual(len(manual_payments(c)), 1)
        self.assertEqual(c._paid_amount(did), 28_000)
        self.assertEqual({r["replayed"] for r in results}, {False, True})


class ConcurrentPaymentBarrierTest(unittest.TestCase):
    def _run_concurrent(self, c, did, amount, n, request_prefix,
                        claim_code=None):
        barrier = threading.Barrier(n)
        outcomes = {"ok": [], "errors": []}

        def worker(i):
            try:
                barrier.wait()
                rec = c.pay_decision(
                    did, "payer-1", PAYER, amount=amount,
                    claim_code=claim_code,
                    request_id=f"{request_prefix}-{i}")
                outcomes["ok"].append(rec)
            except DomainError as exc:
                outcomes["errors"].append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return outcomes

    def test_concurrent_split_payment_never_exceeds_cap(self):
        # 财政岗把一笔奖励拆成两笔并发发放：各申请 20000，上限 28000
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        outcomes = self._run_concurrent(c, did, 20_000, 2, "split")
        self.assertEqual(len(outcomes["ok"]), 1)
        self.assertEqual(len(outcomes["errors"]), 1)
        self.assertEqual(outcomes["errors"][0].code, "INSUFFICIENT_BALANCE")
        self.assertEqual(c._paid_amount(did), 20_000)

    def test_concurrent_full_amount_only_one_wins(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        outcomes = self._run_concurrent(c, did, 28_000, 8, "full")
        self.assertEqual(len(outcomes["ok"]), 1)
        self.assertEqual(len(outcomes["errors"]), 7)
        self.assertTrue(all(e.code == "INSUFFICIENT_BALANCE"
                            for e in outcomes["errors"]))
        self.assertEqual(c._paid_amount(did), 28_000)

    def test_concurrent_even_split_both_succeed_and_then_closed(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        outcomes = self._run_concurrent(c, did, 14_000, 2, "half")
        self.assertEqual(len(outcomes["ok"]), 2)
        self.assertEqual(outcomes["errors"], [])
        self.assertEqual(c._paid_amount(did), 28_000)
        # 临界区关闭后余额为 0
        with self.assertRaises(DomainError) as ctx:
            c.pay_decision(did, "payer-1", PAYER, amount=1,
                           request_id="after")
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_BALANCE")

    def test_concurrent_anonymous_payments_under_barrier(self):
        c, _, _, did, code = setup_decision(
            penalty=0, grade=3, category="广告违法", identity=None)
        self.assertEqual(c.decisions[did]["amount"], 3_000)
        outcomes = self._run_concurrent(c, did, 3_000, 4, "anon",
                                        claim_code=code)
        self.assertEqual(len(outcomes["ok"]), 1)
        self.assertEqual(c._paid_amount(did), 3_000)


class AdjustmentRaceTest(unittest.TestCase):
    def test_payment_blocked_while_adjustment_pending_and_resumes_after_reject(self):
        # 500 万一级食品 = 30 万；先部分支付 10 万
        c, _, case_id, did, _ = setup_decision()
        c.pay_decision(did, "payer-1", PAYER, amount=100_000,
                       request_id="before-adj")

        # 复议降额到 300 万（2026 版一级 6% → 18 万），进入在途
        adj_id = c.adjust_decision(
            did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=3_000_000, changed_at="2026-08-01")
        self.assertEqual(c.adjustments[adj_id]["new_amount"], 180_000)

        # 在途追加决定未办结：临界区内一律拒绝支付
        with self.assertRaises(InvalidStateError):
            c.pay_decision(did, "payer-1", PAYER, amount=10_000,
                           request_id="during-adj")
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["remaining_amount"], 0)  # 在途期间不开放余额
        self.assertEqual([p["stage"] for p in person["pending_approvals"]],
                         ["调整审核"])

        # 追加决定被驳回：旧结论维持，20 万余额重新开放
        c.review_adjustment(adj_id, "reviewer-2", REVIEWER, approve=False)
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["effective_amount"], 300_000)
        self.assertEqual(person["remaining_amount"], 200_000)
        c.pay_decision(did, "payer-1", PAYER, amount=200_000,
                       request_id="after-reject")
        self.assertEqual(c._paid_amount(did), 300_000)

    def test_pay_vs_adjust_concurrent_resolves_by_lock_order(self):
        """支付与降额调整发起并发：反复运行只可能出现两种可解释结局。"""
        payment_won = 0
        adjustment_won = 0
        for i in range(40):
            c, _, _, did, _ = setup_decision()  # 决定额度 300000
            start = threading.Barrier(2)
            pay_error = []

            def pay():
                try:
                    start.wait()
                    time.sleep(random.random() * 0.002)
                    c.pay_decision(did, "payer-1", PAYER, amount=300_000,
                                   request_id="race-pay")
                except InvalidStateError as exc:
                    pay_error.append(exc)

            def adjust():
                start.wait()
                time.sleep(random.random() * 0.002)
                c.adjust_decision(
                    did, "reconsideration", "handler-1", HANDLER,
                    new_penalty_amount=1_000_000)  # 6 万

            t1 = threading.Thread(target=pay)
            t2 = threading.Thread(target=adjust)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # 无论谁先进入临界区，在途调整都要审核后才生效
            adj_id = c.decisions[did]["superseded_by"]
            self.assertTrue(adj_id)
            c.review_adjustment(adj_id, "reviewer-2", REVIEWER)
            net = c._paid_amount(did)

            if pay_error:
                # 调整先入临界区：支付被在途闸门拦截，调整生效后额度为 6 万
                adjustment_won += 1
                self.assertIsInstance(pay_error[0], InvalidStateError)
                self.assertEqual(net, 0)
                self.assertEqual(len(manual_payments(c)), 0)
                self.assertEqual(clawbacks(c), [])
            else:
                # 支付先入临界区：30 万支付成功；降额生效后形成 24 万应追回
                payment_won += 1
                self.assertEqual(len(manual_payments(c)), 1)
                self.assertEqual(len(clawbacks(c)), 1)
                self.assertEqual(clawbacks(c)[0]["amount"], -240_000)
                self.assertEqual(net, 60_000)
                # 历史支付未被撤销或改写
                self.assertEqual(manual_payments(c)[0]["amount"], 300_000)
            # 两种顺序下的共同不变量：净额绝不超过调整后额度
            self.assertLessEqual(net, 60_000)

        # 屏障应对两种交错都实际覆盖到（40 次随机抖动竞争）
        self.assertGreater(payment_won, 0)
        self.assertGreater(adjustment_won, 0)

    def test_pay_vs_withdraw_concurrent_resolves_by_lock_order(self):
        paid, blocked = 0, 0
        for i in range(40):
            c, alias, _, did, _ = setup_decision(
                penalty=1_000_000, grade=2, category="价格违法")  # 28000
            start = threading.Barrier(2)
            pay_error = []

            def pay():
                try:
                    start.wait()
                    time.sleep(random.random() * 0.002)
                    c.pay_decision(did, "payer-1", PAYER, amount=28_000,
                                   request_id="race-wd-pay")
                except InvalidStateError as exc:
                    pay_error.append(exc)

            def withdraw():
                start.wait()
                time.sleep(random.random() * 0.002)
                c.withdraw_report(alias, "intake-1", INTAKE)

            t1 = threading.Thread(target=pay)
            t2 = threading.Thread(target=withdraw)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            if pay_error:
                blocked += 1
                self.assertEqual(c._paid_amount(did), 0)
            else:
                paid += 1
                self.assertEqual(c._paid_amount(did), 28_000)
            self.assertLessEqual(c._paid_amount(did), 28_000)

        self.assertGreater(paid, 0)
        self.assertGreater(blocked, 0)


class AdjustmentAmountSemanticsTest(unittest.TestCase):
    def _effect(self, c, adj_id, reviewer="reviewer-2", finance="finance-2"):
        """审核追加决定，必要时（额度达到会签线）完成会签使其生效。"""
        c.review_adjustment(adj_id, reviewer, REVIEWER)
        if c.adjustments[adj_id]["status"] == "待调整会签":
            c.cosign_adjustment(adj_id, finance, FINANCE)
        self.assertEqual(c.adjustments[adj_id]["status"], "已生效")

    def _lower_to(self, c, did, new_penalty):
        adj_id = c.adjust_decision(
            did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=new_penalty, changed_at="2026-08-01")
        self._effect(c, adj_id)
        return adj_id

    def test_partial_payment_then_reduction_within_paid_no_clawback(self):
        # 决定 30 万，已付 10 万；降额到 18 万（实付未超新额度）：不追回
        c, _, case_id, did, _ = setup_decision()
        c.pay_decision(did, "payer-1", PAYER, amount=100_000,
                       request_id="p1")
        self._lower_to(c, did, 3_000_000)
        self.assertEqual(clawbacks(c), [])
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["effective_amount"], 180_000)
        self.assertEqual(person["paid_total"], 100_000)
        self.assertEqual(person["remaining_amount"], 80_000)
        # 剩余 8 万可继续支付
        c.pay_decision(did, "payer-1", PAYER, amount=80_000,
                       request_id="p2")
        self.assertEqual(c._paid_amount(did), 180_000)

    def test_partial_payment_then_reduction_below_paid_claws_back_delta(self):
        # 已付 10 万；降额到 6 万：只形成 4 万应追回差额，原支付不撤销
        c, _, case_id, did, _ = setup_decision()
        original = c.pay_decision(did, "payer-1", PAYER, amount=100_000,
                                  request_id="p1")
        self._lower_to(c, did, 1_000_000)  # 6 万
        self.assertEqual(len(clawbacks(c)), 1)
        self.assertEqual(clawbacks(c)[0]["amount"], -40_000)
        self.assertEqual(clawbacks(c)[0]["note"], "应追回差额")
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["paid_out_total"], 100_000)
        self.assertEqual(person["clawed_back_total"], 40_000)
        self.assertEqual(person["paid_total"], 60_000)
        self.assertEqual(person["remaining_amount"], 0)
        # 历史支付凭证原样保留，未被改写
        still_there = manual_payments(c)[0]
        self.assertEqual(still_there["payment_id"], original["payment_id"])
        self.assertEqual(still_there["amount"], 100_000)

    def test_increase_opens_new_balance_without_auto_supplement(self):
        # 全额支付 30 万后，复议升额到 36 万：不自动补付，只开放 6 万新余额
        c, _, case_id, did, _ = setup_decision()
        c.pay_decision(did, "payer-1", PAYER, request_id="p1")
        bookings_before = len(c.payments)
        adj_id = c.adjust_decision(
            did, "reconsideration", "handler-1", HANDLER,
            new_penalty_amount=6_000_000, changed_at="2026-08-01")  # 36 万
        # 36 万达到会签线：审核 + 会签后才生效
        self._effect(c, adj_id)
        # 生效不产生任何账务记录（无自动补付）
        self.assertEqual(len(c.payments), bookings_before)
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["effective_amount"], 360_000)
        self.assertEqual(person["paid_total"], 300_000)
        self.assertEqual(person["remaining_amount"], 60_000)
        # 新增余额须由新的支付请求逐笔领取
        c.pay_decision(did, "payer-1", PAYER, amount=60_000,
                       request_id="p2")
        self.assertEqual(c._paid_amount(did), 360_000)

    def test_down_then_up_chain_reopens_only_remaining(self):
        # 10 万已付 → 降到 6 万（追回 4 万）→ 升到 8 万：开放 2 万新余额
        c, _, case_id, did, _ = setup_decision()
        c.pay_decision(did, "payer-1", PAYER, amount=100_000,
                       request_id="p1")
        self._lower_to(c, did, 1_000_000)   # 6 万
        adj_up = c.adjust_decision(
            did, "judgment", "handler-1", HANDLER,
            new_penalty_amount=1_400_000,  # 140 万 × 6% = 8.4 万
            changed_at="2026-09-01")
        # 84000 低于会签线，审核即生效
        self._effect(c, adj_up)
        person = c.explain_case(case_id)["reporters"][0]
        self.assertEqual(person["effective_amount"], 84_000)
        self.assertEqual(person["paid_total"], 60_000)
        self.assertEqual(person["remaining_amount"], 24_000)
        # 调整链完整可核对
        kinds = [a["kind_label"] for a in person["adjustments"]]
        self.assertEqual(kinds, ["行政复议变化", "司法判决变化"])
        self.assertEqual([a["status"] for a in person["adjustments"]],
                         ["已生效", "已生效"])
        # 原决定金额始终未被改写
        self.assertEqual(c.decisions[did]["amount"], 300_000)


class ClaimCodeConfidentialityTest(unittest.TestCase):
    def test_claim_code_never_in_voucher_logs_or_errors(self):
        c, alias, case_id, did, code = setup_decision(
            penalty=0, grade=3, category="广告违法", identity=None)
        voucher = c.pay_decision(did, "payer-1", PAYER, claim_code=code,
                                 request_id="anon-pay")

        # 凭证不含领取码
        self.assertNotIn("claim_code", json.dumps(voucher, ensure_ascii=False))
        # 逐人说明与逐笔支付不含领取码
        explained = json.dumps(c.explain_case(case_id), ensure_ascii=False)
        self.assertNotIn(code, explained)
        # 普通办案日志不含领取码
        log = json.dumps(c.ordinary_case_log(case_id), ensure_ascii=False)
        self.assertNotIn(code, log)
        # 内存支付记录本身也不保存领取码
        for p in c.payments:
            self.assertNotIn("claim_code", p)

        # 错码错误信息不回显提交值
        try:
            c.pay_decision(did, "payer-1", PAYER, claim_code="abcd1234ef",
                           request_id="bad-code")
        except PermissionDenied as exc:
            self.assertNotIn("abcd1234ef", str(exc))
        else:  # pragma: no cover - 已付清应抛余额不足而非成功
            self.fail("已付清决定不应再次支付成功")

    def test_wrong_code_rejected_before_balance_check(self):
        c, _, _, did, code = setup_decision(
            penalty=0, grade=3, category="广告违法", identity=None)
        c.pay_decision(did, "payer-1", PAYER, claim_code=code,
                       request_id="anon-pay")
        # 再次支付时错误领取码优先报 403，而不是泄露余额已尽
        with self.assertRaises(PermissionDenied):
            c.pay_decision(did, "payer-1", PAYER, claim_code="00000000",
                           request_id="anon-pay-2")


class StableErrorCodeTest(unittest.TestCase):
    def test_domain_errors_carry_stable_codes(self):
        c, _, _, did, _ = setup_decision(penalty=1_000_000, grade=2,
                                         category="价格违法")
        with self.assertRaises(PermissionDenied) as ctx:
            c.pay_decision(did, "handler-1", HANDLER, request_id="x")
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

        # 未生效决定不能支付（实名举报，避免领取码校验先行）
        c2, _, _, dids = _proposed_only()
        with self.assertRaises(InvalidStateError) as ctx:
            c2.pay_decision(dids, "payer-1", PAYER, request_id="x")
        self.assertEqual(ctx.exception.code, "INVALID_STATE")


def _proposed_only():
    c = make_center()
    alias, case_id, _ = c.intake_report(
        "intake-1", INTAKE, "价格违法", ["事实A"], received_at="2026-02-01",
        identity={"name": "待决举报人"})
    c.close_case(case_id, 1_000_000)
    c.enter_reward_stage(case_id)
    c.assess_contributions(case_id, [
        {"alias": alias, "grade": 2, "new_facts": ["事实A"]}
    ], "intake-1", INTAKE)
    did = c.propose_rewards(case_id, "handler-1", HANDLER)[0]
    return c, alias, case_id, did


if __name__ == "__main__":
    unittest.main()
