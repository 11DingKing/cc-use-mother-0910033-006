# 双边成交确认撤销

本项目维护双边成交确认撤销的领域约定、角色边界与样例数据，并提供场外积分协商成交的 Python 后端：管理报价版本、双方签署、额度冻结、确认期限与清算指令，只有同一版本达到双签后才原子成交。当前契约覆盖企业申报员、核算专员、交易运营员、监管审计员，并明确双边同版签署、额度双向冻结、签署失效传播、撤销补偿分录等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/trade_confirmation/`：成交确认后端（模型、台账、服务、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试与后端规则测试。

## 后端设计

- **报价版本**：创建报价为草稿，发布后版本生效；每次修订产生新版本，旧版本作废。
- **额度双向冻结**：版本生效即同时冻结卖方积分与买方资金（先校验后冻结，保证原子性）。
- **双边同版签署**：签署只作用于当前版本；集齐同一版本双签后，在同一锁内消耗双方冻结、
  生成清算指令与托管户，原子成交。修订使旧签署失效并联动释放冻结，跨版本签署不可能成交。
- **确认期限**：每个版本带确认期限，超时未成交自动过期，冻结以补偿分录释放。
- **补偿分录**：单方撤回、超时、部分交割的余量，全部通过引用原分录的
  RELEASE / COMPENSATE 分录冲回，双方在同一动作内一并处理，杜绝只释放一方额度。
- **幂等**：创建按 `client_ref`、签署按参与方、交割回调按 `callback_id` 去重，
  重复回调直接返回首次处理结果，不产生新分录。
- **台账**：全部资金变动落分录，可回放校验（`ledger.verify()`）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts/{owner}/deposit` | 注资 `{asset, amount}` |
| GET | `/accounts/{owner}` | 余额与全部分录 |
| POST | `/quotes` | 创建报价 `{buyer, seller, points, unit_price_cents, confirm_seconds, client_ref?}` |
| GET | `/quotes/{qid}` | 报价全量视图：版本、签署、冻结、分录、时间线 |
| POST | `/quotes/{qid}/publish` | 发布当前版本（双向冻结） |
| POST | `/quotes/{qid}/revise` | 修订条款，旧签署失效 |
| POST | `/quotes/{qid}/sign` | 签署 `{party: BUYER\|SELLER}` |
| POST | `/quotes/{qid}/withdraw` | 单方撤回，双方冻结一并补偿释放 |
| POST | `/quotes/{qid}/expire`、`POST /sweep` | 超时检查 |
| GET | `/clearing/{iid}` | 清算指令与托管户状态 |
| POST | `/clearing/{iid}/deliver` | 交割回调 `{callback_id, points, cash}`，支持部分交割、幂等 |
| POST | `/clearing/{iid}/close` | 关闭清算，未交割余量补偿退回原主 |

金额约定：积分为整数个，资金为整数分。

## 运行

启动服务：`PYTHONPATH=src python3 -m trade_confirmation --port 8000 --demo`

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
