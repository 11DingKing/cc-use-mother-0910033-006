# 双边成交确认撤销

本项目维护双边成交确认撤销的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖企业申报员、核算专员、交易运营员、监管审计员，并明确双边同版签署、额度双向冻结、签署失效传播、撤销补偿分录等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/trade_confirmation/`：双边成交确认撤销后端（纯标准库实现）。
  - `models.py`：报价版本、双方签署、清算指令、台账分录等领域模型。
  - `ledger.py`：复式台账——分录按资产平衡、普通账户不得透支，余额由分录推导。
  - `service.py`：核心状态机——同版双签原子成交、修订失效传播、撤回/超时/部分交割以补偿分录收尾、回调幂等去重。
  - `api.py`：HTTP 接口，逐步展示签署与资金状态。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动后端服务。
- `tests/`：契约完整性、台账、状态机与接口回归测试。

## 后端设计

- **报价版本**：每次条款修订生成新版本（v1、v2…），旧版签署立即失效，对应冻结额度以补偿分录（`RELEASE`）释放。
- **双方签署**：签署必须指定当前版本，版本不一致拒绝（`VERSION_MISMATCH`）；卖方签署冻结积分、买方签署冻结资金，实现额度双向冻结。
- **原子成交**：同一版本集齐双签后，一条 `SETTLE` 分录内双方冻结同时转在途，生成双腿清算指令（积分腿、资金腿）。
- **确认期限**：每个版本携带确认截止时刻，超时报价自动置为 `EXPIRED` 并释放冻结（支持惰性检查与 `POST /maintenance/sweep` 批量处理）。
- **补偿分录**：单方撤回、超时、条款修订释放冻结（`RELEASE`）；清算部分交割的缺口回补付款方（`COMPENSATE`）。
- **重复回调**：清算回调按 `callback_id` 幂等去重；累计交割量回退或超出应付会被拒绝（`CALLBACK_CONFLICT`）。
- **可观测**：`GET /quotes/{id}` 返回版本、签署、清算进度、全部分录、时间线与双方资金状态；`GET /accounts/{party}` 返回账户维度资金状态与分录。

## 运行

启动服务：`python3 tools/run_server.py --port 8080`

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts/{party}/deposit` | 初始额度入账（`asset`/`amount`） |
| GET | `/accounts/{party}` | 账户资金状态与分录 |
| POST | `/quotes` | 创建报价（`seller`/`buyer`/`points`/`price`/`confirm_seconds`） |
| GET | `/quotes` / `/quotes/{id}` | 报价列表 / 全量详情 |
| POST | `/quotes/{id}/sign` | 签署指定版本（`party`/`version`） |
| POST | `/quotes/{id}/revise` | 修订条款，旧签署失效 |
| POST | `/quotes/{id}/withdraw` | 单方撤回 |
| POST | `/quotes/{id}/expire` / `/maintenance/sweep` | 超时检查（单个 / 批量） |
| POST | `/settlements/{id}/callbacks` | 清算交割回调（`callback_id`/`asset`/`delivered`/`final`） |
| GET | `/settlements/{id}` | 清算指令详情 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
