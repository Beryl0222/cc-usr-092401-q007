"""HTTP 端到端：三类业务情形走完整接口，并验证响应不泄露身份。"""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import service
from service import Handler


def actor(actor_id, role):
    return {"id": actor_id, "role": role}


class HttpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_center()

    def post(self, path, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(f"{self.base_url}{path}", data=data, method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def get(self, path, **query):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        with urlopen(url, timeout=3) as resp:
            return resp.status, json.load(resp)

    # ------------------------------------------------------------------

    def test_three_reporters_flow_over_http(self):
        def report(facts, day):
            status, body = self.post("/reports/intake", {
                "actor": actor("intake-1", "intake_officer"),
                "violation_category": "食品药品安全",
                "facts": facts, "received_at": day,
                "identity": {"name": "真实姓名-不应出现在任何响应里"}})
            self.assertEqual(status, 201)
            return body["alias"], body["case_id"]

        a1, case_id = report(["事实A"], "2026-02-01")
        a2, c2 = report(["事实A"], "2026-02-03")
        a3, c3 = report(["事实B"], "2026-02-10")
        self.assertEqual((c2, c3), (case_id, case_id))

        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 5_000_000,
            "at": "2026-03-01"})
        _, stage = self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-03-05"})
        self.assertEqual(stage["rule_version"], "2026-01")

        _, assessed = self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [
                {"alias": a1, "grade": 1, "new_facts": ["事实A"]},
                {"alias": a2, "grade": 1, "duplicate": True},
                {"alias": a3, "grade": 2, "new_facts": ["事实B"],
                 "key_contribution": True},
            ]})
        self.assertEqual(assessed["contributions"][a1], "最先有效贡献")
        self.assertEqual(assessed["contributions"][a2], "重复举报")
        self.assertEqual(assessed["contributions"][a3], "独立关键贡献")

        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        self.assertEqual(len(proposed["decision_ids"]), 2)

        # 承办人批准自己的建议：403
        status_self, _ = self.post("/rewards/approve", {
            "actor": actor("handler-1", "reward_reviewer"),
            "decision_id": proposed["decision_ids"][0]})
        self.assertEqual(status_self, 403)

        for idx, did in enumerate(proposed["decision_ids"]):
            status, body = self.post("/rewards/approve", {
                "actor": actor("reviewer-1", "reward_reviewer"),
                "decision_id": did})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "待财政会签")
            status, body = self.post("/rewards/cosign", {
                "actor": actor("finance-1", "finance_cosigner"),
                "decision_id": did})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "已生效")
            status, paid = self.post("/rewards/pay", {
                "actor": actor("payer-1", "payment_officer"),
                "decision_id": did, "request_id": f"pay-{idx}"})
            self.assertEqual(status, 200)
            self.assertNotIn("claim_code", paid)

        _, explain = self.get(f"/cases/{case_id}/explain")
        by_alias = {r["alias"]: r for r in explain["reporters"]}
        self.assertEqual(by_alias[a1]["paid_total"], 300_000)
        self.assertEqual(by_alias[a3]["paid_total"], 200_000)
        self.assertFalse(by_alias[a2]["eligible"])
        self.assertEqual(by_alias[a2]["paid_total"], 0)

        # 所有对外/办案视图都不得出现真实姓名
        _, case_file = self.get(f"/cases/{case_id}/file",
                                actor="h-1", role="case_handler")
        _, public = self.get(f"/cases/{case_id}/public")
        _, log = self.get(f"/cases/{case_id}/log")
        for view in (explain, case_file, public, log):
            self.assertNotIn("真实姓名", json.dumps(view, ensure_ascii=False))

        # 承办人无权查看身份（403），受理员查看留痕
        status_forbidden, _ = self.post("/identity/reveal", {
            "actor": actor("h-1", "case_handler"), "alias": a1,
            "reason": "想看看"})
        self.assertEqual(status_forbidden, 403)
        status_ok, revealed = self.post("/identity/reveal", {
            "actor": actor("intake-1", "intake_officer"), "alias": a1,
            "reason": "核实联系方式"})
        self.assertEqual(status_ok, 200)
        self.assertIn("identity", revealed)
        _, access = self.get("/identity/access-log", role="audit_viewer")
        self.assertEqual(len(access["events"]), 1)

    def test_no_penalty_anonymous_flow(self):
        status, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "广告违法",
            "facts": ["发布违法医疗广告"], "received_at": "2026-04-01"})
        self.assertEqual(status, 201)
        alias, case_id, code = body["alias"], body["case_id"], body["claim_code"]
        self.assertTrue(body["hint"])

        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 0, "at": "2026-05-01"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-05-05"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [
                {"alias": alias, "grade": 3,
                 "new_facts": ["发布违法医疗广告"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})

        # 无领取码 / 错码：403
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "anon-pay"})[0], 403)
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "anon-pay",
            "claim_code": "deadbeef"})[0], 403)
        status, paid = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "anon-pay",
            "claim_code": code})
        self.assertEqual(status, 200)
        self.assertEqual(paid["amount"], 3000)  # 2026 版三级无罚没款定额
        self.assertEqual(paid["request_id"], "anon-pay")
        self.assertNotIn("claim_code", json.dumps(paid))

        # 网络重试：同标识原样返回首次凭证，不重复落账
        status, replay = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "anon-pay",
            "claim_code": code})
        self.assertEqual(status, 200)
        self.assertEqual(replay["payment_id"], paid["payment_id"])
        _, explain = self.get(f"/cases/{case_id}/explain")
        self.assertEqual(len(explain["reporters"][0]["payment_vouchers"]), 1)

        # 缺少稳定业务标识：422 稳定错误码
        status, bad = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "claim_code": code})
        self.assertEqual(status, 422)
        self.assertEqual(bad["error_code"], "PAYMENT_REJECTED")

    def test_cross_effective_date_reconsideration(self):
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "食品药品安全",
            "facts": ["事实A"], "received_at": "2025-11-01",
            "identity": {"name": "王五-不外露"}})
        alias, case_id = body["alias"], body["case_id"]
        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 5_000_000,
            "at": "2025-12-10"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2025-12-15"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 1,
                             "new_facts": ["事实A"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})
        self.post("/rewards/cosign", {
            "actor": actor("finance-1", "finance_cosigner"),
            "decision_id": did})
        status, paid_orig = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "orig-pay"})
        self.assertEqual(status, 200)
        self.assertEqual(paid_orig["amount"], 250_000)

        # 2026 年发起复议，仍按 2023 版规则重算
        status, adj = self.post("/rewards/adjust", {
            "actor": actor("handler-1", "case_handler"),
            "decision_id": did, "kind": "reconsideration",
            "new_penalty_amount": 3_000_000, "at": "2026-03-01"})
        self.assertEqual(status, 201)
        self.assertEqual(adj["old_amount"], 250_000)
        self.assertEqual(adj["new_amount"], 150_000)
        self.assertFalse(adj["needs_cosign"])

        # 追加决定同样禁止自审
        self.assertEqual(self.post("/rewards/adjustment/approve", {
            "actor": actor("handler-1", "reward_reviewer"),
            "adjustment_id": adj["adjustment_id"]})[0], 403)
        status, approved = self.post("/rewards/adjustment/approve", {
            "actor": actor("reviewer-2", "reward_reviewer"),
            "adjustment_id": adj["adjustment_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "已生效")

        _, explain = self.get(f"/cases/{case_id}/explain")
        person = explain["reporters"][0]
        self.assertEqual(person["effective_amount"], 150_000)
        # 降额不撤销历史支付：25 万凭证保留，另列应追回 10 万、剩余为 0
        self.assertEqual(person["paid_total"], 250_000)
        self.assertEqual(person["clawback_due"], 100_000)
        self.assertEqual(person["remaining_amount"], 0)
        self.assertEqual(len(person["payment_vouchers"]), 1)
        self.assertEqual(person["adjustments"][0]["kind_label"], "行政复议变化")
        self.assertEqual(person["adjustments"][0]["clawback_due"], 100_000)
        # 旧结论保留
        self.assertEqual(person["current_decision"]["amount"], 250_000)

    def test_concurrent_split_payments_over_http_never_exceed_cap(self):
        """真实 HTTP 多线程：两笔并发全额支付恰好一笔成功、一笔 422。"""
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "特种设备安全",
            "facts": ["事实X"], "received_at": "2026-02-01"})
        alias, case_id, claim_code = body["alias"], body["case_id"], body["claim_code"]
        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 0, "at": "2026-03-01"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-03-05"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 1,
                             "new_facts": ["事实X"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]  # 2026 版一级无罚没款定额 8000
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})

        barrier = threading.Barrier(2)

        def pay(req_id):
            barrier.wait(timeout=5)
            return self.post("/rewards/pay", {
                "actor": actor("payer-1", "payment_officer"),
                "decision_id": did, "request_id": req_id,
                "claim_code": claim_code})

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(pay, "http-req-1")
            f2 = pool.submit(pay, "http-req-2")
            s1, b1 = f1.result(timeout=5)
            s2, b2 = f2.result(timeout=5)
        statuses = sorted([s1, s2])
        self.assertEqual(statuses, [200, 422])
        ok = b1 if s1 == 200 else b2
        bad = b1 if s1 == 422 else b2
        self.assertEqual(ok["amount"], 8000)
        self.assertEqual(bad["error_code"], "PAYMENT_REJECTED")

        # 失败方（未成功的 request_id）重试同一内容：仍无余额，422
        loser_req = "http-req-1" if ok["request_id"] == "http-req-2" \
            else "http-req-2"
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": loser_req,
            "claim_code": claim_code})[0], 422)
        # 成功方重放时领取码错误：403，且不会改变首次凭证
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": ok["request_id"],
            "claim_code": "00000000"})[0], 403)
        # 成功方原样重放：200 且凭证号不变
        status, replay = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": ok["request_id"],
            "claim_code": claim_code})
        self.assertEqual(status, 200)
        self.assertEqual(replay["payment_id"], ok["payment_id"])

        _, explain = self.get(f"/cases/{case_id}/explain")
        person = explain["reporters"][0]
        self.assertEqual(person["paid_total"], 8000)
        self.assertEqual(person["remaining_amount"], 0)
        self.assertEqual(len(person["payment_vouchers"]), 1)

    def test_idempotency_conflict_has_stable_code(self):
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "无照经营",
            "facts": ["事实Y"], "received_at": "2026-02-01"})
        alias, case_id, claim_code = body["alias"], body["case_id"], body["claim_code"]
        self.post("/cases/close", {"case_id": case_id, "penalty_amount": 0})
        self.post("/cases/reward-stage", {"case_id": case_id})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 1,
                             "new_facts": ["事实Y"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]  # 8000
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})
        status, first = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "idem-1", "amount": 3000,
            "claim_code": claim_code})
        self.assertEqual(status, 200)
        self.assertEqual(first["amount"], 3000)
        # 同标识异金额：409 + IDEMPOTENCY_CONFLICT
        status, conflict = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "idem-1", "amount": 5000,
            "claim_code": claim_code})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error_code"], "IDEMPOTENCY_CONFLICT")
        # 同标识同金额原样重放：仍是首次凭证
        status, replay = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "idem-1", "amount": 3000,
            "claim_code": claim_code})
        self.assertEqual(status, 200)
        self.assertEqual(replay["payment_id"], first["payment_id"])

    def test_unknown_route_and_bad_json(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        req = Request(f"{self.base_url}/reports/intake",
                      data=b"{bad json", method="POST",
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
