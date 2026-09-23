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
  追加决定同样走审核（及必要时会签）。追加决定在途（待审核/待会签）期间禁止
  支付；**已成功的历史支付凭证一律不撤销、不改写、不补记**——降额只在调整
  生效时固化“应追回差额”（累计支付超过新生效金额的部分），升额只开放新增
  可付余额，由之后的新支付指令逐笔领取。
- **幂等支付**：每个支付请求必须携带调用方生成的稳定业务标识 `request_id`
  （网络重试原样复用，不得换新号），并绑定决定、金额、领取人（`alias`）。
  同标识同内容重试返回**首次凭证**（同一 `payment_id`，不重复落账）；同标识
  异内容（决定/金额/领取人不同）返回 409 `IDEMPOTENCY_CONFLICT`；匿名指令
  即使是重放也必须重新出示正确领取码。支持部分支付：余额检查与支付落账在
  同一临界区（进程内全局屏障锁）完成，并发拆付合计绝不超过最新已生效结论。
- **匿名支付**：匿名举报生成一次性领取码，支付时校验；领取码（含哈希）不进
  普通日志、支付凭证与任何响应，业务记录仍只写别名。

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
| `POST /rewards/pay` | 支付（须带稳定 `request_id`，可选 `alias`/`amount` 部分支付；匿名须带 `claim_code`） |
| `POST /rewards/adjust` | 追加决定：withdrawal/duplicate/reconsideration/judgment |
| `POST /rewards/adjustment/approve` | 追加决定审核 |
| `POST /rewards/adjustment/cosign` | 追加决定会签 |
| `POST /commendations` | 登记精神奖励 |
| `POST /identity/reveal` | 查看真实身份（受限且留痕） |
| `GET /cases/{id}/explain` | 逐人说明：资格/待办审批/累计支付/剩余金额/应追回/凭证与调整沿革 |
| `GET /cases/{id}/file` | 承办人办案视图（仅别名） |
| `GET /cases/{id}/public` | 对外材料 |
| `GET /cases/{id}/log` | 普通办案日志 |
| `GET /identity/access-log?role=audit_viewer` | 身份访问台账 |

## 支付幂等与并发语义

- `request_id` 由调用方生成并在**同一指令的所有重试中保持不变**；成功后
  绑定 `(decision_id, alias, amount)`，服务端返回 `payment_id` 稳定的首次
  凭证。校验失败（余额、领取码、状态）不占用该标识，纠正后可用同一标识重试。
- 不带金额（`amount` 缺省）即结清当前剩余可付金额；部分支付可逐笔进行。
- 在途追加决定（撤回/重复/复议/判决变化待审核或待会签）期间所有支付被拒，
  裁决顺序为“先到先得、全程持同一屏障锁”：审核先生效则按新结论支付
  （降额仅余应追回、升额开放新增余额），支付先落账则调整在既有支付之上
  固化差额。两种顺序下历史凭证均不变化，差额都可在 `explain` 核对。
- 错误响应统一携带稳定错误码 `error_code`：

  | HTTP | error_code | 含义 |
  |---|---|---|
  | 403 | `PERMISSION_DENIED` | 角色/领取码不符 |
  | 404 | `NOT_FOUND` | 决定/案件等不存在 |
  | 409 | `INVALID_STATE` | 决定未生效、已撤回或存在在途追加决定 |
  | 409 | `IDEMPOTENCY_CONFLICT` | 同 `request_id` 绑定的决定/金额/领取人不同 |
  | 422 | `PAYMENT_REJECTED` | 缺 `request_id`、领取人不匹配、金额超出可付余额 |
  | 400 | `DOMAIN_ERROR` / `BAD_REQUEST` | 其他业务或参数错误 |

## 逐人说明核对口径

`GET /cases/{id}/explain` 中每名举报人含：

- `paid_total`：累计**发放**净额（仅真实支付凭证，调整不再产生负向凭证）；
- `remaining_amount`：剩余可付金额，满足 `paid_total + remaining_amount`
  不超过 `effective_amount`；
- `clawback_due`：降额生效后固化的应追回差额，
  满足 `paid_total = effective_amount + clawback_due`（超付时）；
- `payment_vouchers`：逐笔凭证（`payment_id`/`request_id`/金额/支付人/日期）；
- `adjustment_chain`：完整调整链，含每道决定的 `old_amount`/`new_amount`/
  `delta`、生效时累计支付快照、`clawback_due` 与 `open_balance`。

## 运行与测试

```bash
python3 service.py --check   # 规则与服务自检
python3 service.py --port 8000
npm test                     # 契约 + 领域规则 + HTTP 端到端 + 支付链并发/重放/竞态，共 53 项
```

`fixtures/domain.json` 保存领域名词与状态样例，便于接口联调时保持一致语义。
当前状态保存在进程内存中，适合规则验证与联调；正式部署需接入持久化存储。

## 构建检查

```bash
python3 -m compileall -q .
```
