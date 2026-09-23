# 市场举报奖励

隔离举报身份，衔接案件贡献认定、奖励建议、财政会签与支付。

核心模块 `reward_center.py` 不依赖任何框架；`service.py` 把它暴露为 HTTP 接口。
业务规则随规则版本演进，金额由规则引擎自动产生，任何人不得手填。

## 规则要点

- **身份隔离**：举报线索、证据补充、关联案件只按别名记录；真实身份单独保存，
  仅举报中心受理员/审计查看人可凭事由查看且全程留痕。承办人视图、对外材料、
  普通办案日志只含别名。
- **贡献认定**：按接收时间认定"最先有效贡献"；后来者带来未被覆盖的新事实的为
  "独立关键贡献"，二者具备物质奖励资格；事实已被覆盖的记"重复举报"，不予奖励。
- **三级物质奖励**：金额 = 罚没款 × 举报等级比例 × 违法类别严重度（不低于等级
  保底）；无罚没款结案按等级定额；内部举报 ×1.5；单案封顶 100 万元。
- **精神奖励**：通报表扬/荣誉证书/锦旗，与物质奖励并行、互不影响。
- **二十万会签**：建议金额 ≥ 200,000 元自动转财政会签；低于该线审核后直接生效。
- **职责分离**：承办人发起建议，审核人不得是建议人本人，会签人不得与前两者同人。
- **规则版本锁定**：案件进入可奖励阶段时锁定当时生效的规则；之后的行政复议、
  判决变化即使跨越规则生效日，也按锁定版本重算。
- **只追加调整**：撤回、重复确认、复议、判决变化均产生追加决定，原决定原样保留；
  追加决定同样走审核（及必要时会签），驳回则旧结论维持。调整生效不自动改写账务：
  降额只就已超付部分形成"应追回差额"（不超过实付），升额只开放新增可支付余额，
  由后续支付请求逐笔领取。
- **支付幂等与并发屏障**：每个支付请求必须带调用方生成的稳定业务标识
  `request_id`，并与决定、金额、领取人（别名）绑定；原样重试直接回放首次凭证，
  同标识异内容返回 `IDEMPOTENCY_CONFLICT`。余额检查与落账在同一临界区内完成，
  并发拆付不会超过已生效决定的上限；在途追加决定（审核/会签中）期间支付一律
  按顺序暂缓，返回 `INVALID_STATE`。
- **匿名支付**：匿名举报生成一次性领取码，支付时校验，业务记录仍只写别名；
  领取码只参与哈希校验，不进入凭证、普通日志或错误信息。

规则版本与系数集中在 `reward_center.py` 的 `DEFAULT_RULES`（当前含 2023-01、
2026-01 两版，可扩展）。

## 接口

除 `GET /health` 外均为 `POST /...` + JSON，请求体统一带
`{"actor": {"id": "...", "role": "..."}}`，可用 `at`/`received_at` 指定业务日期。

| 接口 | 作用 |
|---|---|
| `POST /reports/intake` | 登记举报，返回别名、案件号、一次性领取码 |
| `POST /reports/supplement` | 补充证据材料 |
| `POST /reports/withdraw` | 举报人撤回（在途建议终止，已生效的生成追加决定） |
| `POST /cases/close` | 结案并登记罚没款（可为 0） |
| `POST /cases/reward-stage` | 进入可奖励阶段，锁定规则版本 |
| `POST /cases/assess` | 逐人认定贡献类别与举报等级 |
| `POST /rewards/propose` | 按规则自动生成奖励建议（禁止手填金额） |
| `POST /rewards/approve` | 奖励审核（拒绝自审） |
| `POST /rewards/cosign` | 财政会签（仅 ≥ 20 万元时需要） |
| `POST /rewards/pay` | 支付（须带 `request_id`，可选 `alias` 绑定领取人；匿名须带 `claim_code`；支持部分支付 `amount`） |
| `POST /rewards/adjust` | 追加决定：withdrawal/duplicate/reconsideration/judgment |
| `POST /rewards/adjustment/approve` | 追加决定审核 |
| `POST /rewards/adjustment/cosign` | 追加决定会签 |
| `POST /commendations` | 登记精神奖励 |
| `POST /identity/reveal` | 查看真实身份（受限且留痕） |
| `GET /cases/{id}/explain` | 逐人说明：资格/待办审批/逐笔支付/累计支付/剩余金额/调整沿革 |
| `GET /cases/{id}/file` | 承办人办案视图（仅别名） |
| `GET /cases/{id}/public` | 对外材料 |
| `GET /cases/{id}/log` | 普通办案日志 |
| `GET /identity/access-log?role=audit_viewer` | 身份访问台账 |

## 运行与测试

```bash
python3 service.py --check   # 规则与服务自检
python3 service.py --port 8000
npm test                     # 契约 + 领域规则 + HTTP 端到端 + 支付链，共 55 项
```

错误响应为稳定结构 `{"error": {"code": "...", "message": "..."}}`，
`code` 可用于客户端分支：`NOT_FOUND`(404)、`PERMISSION_DENIED`(403)、
`INVALID_STATE`(409)、`IDEMPOTENCY_CONFLICT`(409)、
`PAYMENT_REQUEST_ID_REQUIRED`/`INSUFFICIENT_BALANCE`/`PAYEE_MISMATCH`(400)。

`/rewards/pay` 幂等契约：同一 `request_id` + 相同（决定、金额、领取人）的重试
返回首次凭证（`replayed: true`，同一 `payment_id`），不重复落账；同标识异内容
返回 409。`GET /cases/{id}/explain` 中每人含 `paid_out_total`（实付）、
`clawed_back_total`（应追回）、`paid_total`（净额）、`remaining_amount`
（剩余可支付额度，在途调整期间为 0）与逐笔 `payments`；案件级含
`paid_total`/`remaining_total`。

`fixtures/domain.json` 保存领域名词与状态样例，便于接口联调时保持一致语义。
当前状态保存在进程内存中，适合规则验证与联调；正式部署需接入持久化存储。

## 构建检查

```bash
python3 -m compileall -q .
```
