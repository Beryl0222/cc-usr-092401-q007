"""市场举报奖励服务入口。

除 /health 外，业务接口均为 POST + JSON，请求体统一带：
    {"actor": {"id": "...", "role": "..."}, ...业务参数}
    部分接口接受 "at"/"received_at" 指定业务日期（用于跨生效日的复议等情形）。

服务状态保存在进程内存中，适用于联调与规则验证；持久化由正式部署另行接入。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from reward_center import (
    RewardCenter,
    DomainError,
    NotFoundError,
    PermissionDenied,
    InvalidStateError,
    basic_check,
)

SERVICE_ID = "whistleblower-reward"
SERVICE_NAME = "市场举报奖励"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 全局单例；联调场景足够，正式部署应替换为依赖注入的持久化实现
CENTER = RewardCenter()


def reset_center(today=None):
    """重置进程内状态（仅供测试/联调初始化使用）。"""
    global CENTER
    CENTER = RewardCenter(today=today)
    return CENTER


def _actor(body):
    actor = body.get("actor") or {}
    return actor.get("id"), actor.get("role")


# ---------------------------------------------------------------------------
# 各路由处理：返回 (status_code, payload)
# ---------------------------------------------------------------------------

def handle_intake(center, body):
    actor_id, role = _actor(body)
    alias, case_id, claim_code = center.intake_report(
        actor_id, role,
        violation_category=body["violation_category"],
        facts=body.get("facts"),
        received_at=body.get("received_at"),
        identity=body.get("identity"),
        is_insider=bool(body.get("is_insider")),
    )
    # 领取码仅此一次返回；身份信息不再回显
    return 201, {"alias": alias, "case_id": case_id, "claim_code": claim_code,
                 "hint": "领取码仅显示一次，请妥善保存"}


def handle_supplement(center, body):
    center.add_supplement(body["alias"], body.get("facts"),
                          added_at=body.get("at"))
    return 200, {"alias": body["alias"], "status": "已补充"}


def handle_withdraw(center, body):
    actor_id, role = _actor(body)
    ids = center.withdraw_report(body["alias"], actor_id, role,
                                 withdrawn_at=body.get("at"))
    return 200, {"alias": body["alias"], "adjustments": ids}


def handle_case_close(center, body):
    center.close_case(body["case_id"], body["penalty_amount"],
                      closed_at=body.get("at"))
    return 200, {"case_id": body["case_id"], "status": "已结案",
                 "penalty_amount": body["penalty_amount"]}


def handle_reward_stage(center, body):
    version = center.enter_reward_stage(body["case_id"], entered_at=body.get("at"))
    return 200, {"case_id": body["case_id"], "status": "可奖励",
                 "rule_version": version}


def handle_assess(center, body):
    actor_id, role = _actor(body)
    results = center.assess_contributions(
        body["case_id"], body.get("assessments", []), actor_id, role)
    return 200, {"case_id": body["case_id"], "contributions": results}


def handle_propose(center, body):
    actor_id, role = _actor(body)
    ids = center.propose_rewards(body["case_id"], actor_id, role)
    return 201, {"case_id": body["case_id"], "decision_ids": ids}


def handle_approve(center, body):
    actor_id, role = _actor(body)
    status = center.approve_decision(
        body["decision_id"], actor_id, role,
        approve=bool(body.get("approve", True)))
    return 200, {"decision_id": body["decision_id"], "status": status}


def handle_cosign(center, body):
    actor_id, role = _actor(body)
    status = center.cosign_decision(
        body["decision_id"], actor_id, role,
        agree=bool(body.get("agree", True)))
    return 200, {"decision_id": body["decision_id"], "status": status}


def handle_pay(center, body):
    actor_id, role = _actor(body)
    record = center.pay_decision(
        body["decision_id"], actor_id, role,
        amount=body.get("amount"), claim_code=body.get("claim_code"),
        paid_at=body.get("at"))
    return 200, record


def handle_adjust(center, body):
    actor_id, role = _actor(body)
    adj_id = center.adjust_decision(
        body["decision_id"], body["kind"], actor_id, role,
        new_penalty_amount=body.get("new_penalty_amount"),
        reason=body.get("reason", ""), changed_at=body.get("at"))
    adj = center.adjustments[adj_id]
    return 201, {"adjustment_id": adj_id, "status": adj["status"],
                 "old_amount": adj["old_amount"], "new_amount": adj["new_amount"],
                 "needs_cosign": adj["needs_cosign"]}


def handle_adjustment_approve(center, body):
    actor_id, role = _actor(body)
    status = center.review_adjustment(
        body["adjustment_id"], actor_id, role,
        approve=bool(body.get("approve", True)))
    return 200, {"adjustment_id": body["adjustment_id"], "status": status}


def handle_adjustment_cosign(center, body):
    actor_id, role = _actor(body)
    status = center.cosign_adjustment(
        body["adjustment_id"], actor_id, role,
        agree=bool(body.get("agree", True)))
    return 200, {"adjustment_id": body["adjustment_id"], "status": status}


def handle_commendation(center, body):
    actor_id, role = _actor(body)
    commend = center.grant_commendation(
        body["alias"], body["level"], body.get("reason", ""), actor_id, role)
    return 201, commend


def handle_reveal(center, body):
    actor_id, role = _actor(body)
    identity = center.reveal_identity(
        body["alias"], actor_id, role, body.get("reason", ""))
    # 注意：这是系统内唯一返回身份的入口，全程记录访问台账
    return 200, {"alias": body["alias"], "identity": identity}


POST_ROUTES = {
    "/reports/intake": handle_intake,
    "/reports/supplement": handle_supplement,
    "/reports/withdraw": handle_withdraw,
    "/cases/close": handle_case_close,
    "/cases/reward-stage": handle_reward_stage,
    "/cases/assess": handle_assess,
    "/rewards/propose": handle_propose,
    "/rewards/approve": handle_approve,
    "/rewards/cosign": handle_cosign,
    "/rewards/pay": handle_pay,
    "/rewards/adjust": handle_adjust,
    "/rewards/adjustment/approve": handle_adjustment_approve,
    "/rewards/adjustment/cosign": handle_adjustment_cosign,
    "/commendations": handle_commendation,
    "/identity/reveal": handle_reveal,
}

ERROR_STATUS = {
    NotFoundError: 404,
    PermissionDenied: 403,
    InvalidStateError: 409,
    DomainError: 400,
    KeyError: 400,
    TypeError: 400,
}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与举报奖励业务接口。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, health_payload())
            return
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        # /cases/{id}/explain|file|public|log
        if len(parts) == 3 and parts[0] == "cases":
            case_id, view = parts[1], parts[2]
            try:
                if view == "explain":
                    self._send_json(200, CENTER.explain_case(case_id))
                    return
                if view == "public":
                    self._send_json(200, CENTER.public_case_material(case_id))
                    return
                if view == "log":
                    self._send_json(200, {
                        "case_id": case_id,
                        "events": CENTER.ordinary_case_log(case_id)})
                    return
                if view == "file":
                    actor_id = query.get("actor", [None])[0]
                    role = query.get("role", [None])[0]
                    self._send_json(
                        200, CENTER.case_file_for_handler(case_id, actor_id, role))
                    return
            except DomainError as exc:
                self._domain_error(exc)
                return
        if parsed.path == "/identity/access-log":
            role = query.get("role", [None])[0]
            try:
                self._send_json(200, {"events": CENTER.identity_access_log(role)})
            except DomainError as exc:
                self._domain_error(exc)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        handler = POST_ROUTES.get(parsed.path)
        if handler is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        try:
            status, payload = handler(CENTER, body)
        except DomainError as exc:
            self._domain_error(exc)
            return
        except (KeyError, TypeError) as exc:
            self._send_json(400, {"error": f"缺少或错误的参数：{exc}"})
            return
        self._send_json(status, payload)

    def _domain_error(self, exc):
        status = 400
        for error_type, code in ERROR_STATUS.items():
            if isinstance(exc, error_type):
                status = code
                break
        self._send_json(status, {"error": str(exc)})

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        basic_check()
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
