"""HTTP 端到端：三类业务情形走完整接口，并验证响应不泄露身份。"""

import json
import threading
import unittest
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

        for did in proposed["decision_ids"]:
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
            self.post("/rewards/pay", {
                "actor": actor("payer-1", "payment_officer"),
                "decision_id": did})

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
            "decision_id": did})[0], 403)
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "claim_code": "deadbeef"})[0], 403)
        status, paid = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "claim_code": code})
        self.assertEqual(status, 200)
        self.assertEqual(paid["amount"], 3000)  # 2026 版三级无罚没款定额

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
        self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did})

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
        self.assertEqual(person["paid_total"], 150_000)
        self.assertEqual(person["adjustments"][0]["kind_label"], "行政复议变化")
        # 旧结论保留
        self.assertEqual(person["current_decision"]["amount"], 250_000)

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
