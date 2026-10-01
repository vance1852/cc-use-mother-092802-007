# 核验酿造蒸汽技改的节能收益基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

# 蒸汽利用技改收益核验服务

`src/steam_retrofit_verification/` 在基础服务的同一 SQLite 数据库与审计链上，提供蒸汽技改节能收益的登记、冻结、计算、复核与更正能力，回应工程口径与财务口径的分歧。

## 核验口径与生命周期

- **设备边界与计量点**：登记技改覆盖边界，边界内的蒸汽表构成核验口径；每个计量点带校准有效期，校准证书可续期或被更正证书缩短。
- **基准期/验证期批次**：登记生产批次（产品、产量、起止时间）与蒸汽读数；同一批次保留全部上报历史。
- **经批准的调整因子**：产品结构、停机、负荷等折算因子只能由复核角色批准，工程人员不能自批。
- **数据快照冻结**：冻结时刻固化当时的批次、读数、校准证书、失效窗口、调整因子与替代值并计算 SHA-256 摘要；冻结之后到达的读数判为**迟到**，永不进入该次计算，只在 `reading-quality` 中标记。
- **读数三分类**：`missing`（截止前无读数）、`anomalous`（非正、校准失效、处于失效窗口、强度 MAD 离群）、`late`（冻结后到达）。
- **工程替代计算**：工程人员可为缺失/异常批次提供替代蒸汽量并说明理由；替代值只顶替有问题的批次，绝不静默替换有效测量。标准口径结果始终随快照保留供复核对照。
- **独立复核**：收益必须由具备复核角色且**不是提单人**的人 `approve`/`reject`；驳回后可修改、重新冻结再提交。
- **计量失效传播**：
  - 已确认但**未结算**收益 → 自动置为 `suspended`，结算被阻断；问题排除并重新校准后由复核者恢复。
  - **已结算或已关闭**期间 → 不改原结论，只能登记**更正记录**披露影响金额。
  - 影响定位精确到“失效窗口/校准有效期是否覆盖了该结论实际采用的读数”。
- **可解释性**：`explain` 返回节省量、单位蒸汽成本变化、Welch-t 置信区间分别由哪些计量点/批次/校准证书/调整因子支撑，以及被剔除的缺失、异常、迟到批次。

置信区间用两周期批次单位蒸汽强度的 Welch-t 区间（分位数为标准库内的数值近似），节省量 = 基准强度 × 调整因子 × 验证期产量 − 验证期实际蒸汽。

## 时间与跨班/跨月测试

服务构造时注入时钟（`SystemClock` / 基础库 `FixedClock` / 本包 `clock.MutableClock`）。冻结时刻、读数接收时刻、校准到期判定都走该时钟，因此测试可以推进时钟模拟跨班次与跨月窗口、迟到读数和校准过期。

## 启动

```bash
PYTHONPATH=src python3 -m steam_retrofit_verification.api --database verification.sqlite3 --host 127.0.0.1 --port 8081
```

核验服务路由与基础服务路由共存（健康检查、组织、操作者、场所等仍可用）。主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/boundaries` `/meters` `/calibrations` | 设备边界、计量点、校准证书 |
| POST | `/batches` `/readings` | 生产批次与蒸汽读数 |
| POST | `/adjustment-factors` | 复核角色批准调整因子 |
| POST | `/meter-issues` `/meter-issues/resolve` `/calibration-check` | 计量失效登记/排除/到期巡检 |
| POST | `/verifications` | 创建核验单（基准/验证时间窗、置信水平） |
| POST | `/verifications/alternative` `/verifications/alternative/withdraw` | 提出/撤回工程替代计算 |
| POST | `/snapshots/freeze` | 冻结数据快照并计算 |
| POST | `/verifications/submit` `/verifications/review` `/verifications/resume` | 提交、独立复核、暂停后恢复 |
| POST | `/verifications/settle` `/verifications/close` | 结算、关闭期间 |
| POST | `/verifications/corrections` | 关闭期后更正披露 |
| GET | `/verifications` `/verifications/explain` `/verifications/reading-quality` `/snapshots` | 查询、来源解释、读数质量、快照 |

## 测试与离线验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m steam_retrofit_verification.acceptance
```

验收脚本在临时库中走通登记 → 冻结 → 迟到读数不改变结果 → 独立复核 → 计量失效暂停 → 结算关闭 → 更正披露，并校验审计哈希链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。
