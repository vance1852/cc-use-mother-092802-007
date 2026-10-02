# 核验酿造蒸汽技改的节能收益基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

在此基础上，本项目实现了**酒厂蒸汽利用技改的收益核验服务**：登记设备边界、计量点、校准有效期、基准期/验证期生产批次与批准的调整因子，冻结每次核验采用的数据快照，区分缺失、异常和迟到读数，支持工程人员提出替代计算并由独立复核者确认最终收益；校准失效时定位受影响的核验结论，未结算收益暂停，已关闭期间仅通过更正记录披露。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、技改核验服务和离线验收；
- `tests/`：基础规则、统计计算、核验服务、接口路由和端到端验收测试。

### 技改核验相关模块

| 模块 | 职责 |
| --- | --- |
| `retrofit_service.py` | 设备边界/计量点/校准/批次/调整因子登记，读数质量判定，快照冻结与计算，替代计算、独立确认、关闭，校准失效暂停与更正披露，结果解释 |
| `statistics.py` | 纯函数统计：基于有效测量的单位蒸汽、单位成本变化、节省量与 Welch 双样本 t 置信边界（仅标准库） |
| `clock.py` | `SystemClock`、`FixedClock`、`ManualClock`（可手动推进，用于跨班次与跨月窗口测试） |

## 核验模型与规则

- **数据质量**：读数状态为 `recorded`（有效）、`missing`（未上报，快照按缺失派生）、`anomalous`（超出计量点登记量程）。读数在某次快照冻结之后才到达时，对该快照属于**迟到**：在 `explain` 中派生披露、不改变既有快照；重新冻结核验时可被正常采纳。
- **有效测量**：只有状态为 recorded、观测时点存在未失效校准、批次未被替代计算排除、且蒸汽与产量均为正的测量才进入计算。
- **调整因子**：工程人员（operator/admin）可提出产品结构、停机时间等归一化因子，必须经独立复核者（reviewer）批准后才会进入快照计算。
- **快照冻结**：每次核验把计量点、批次、读数来源、校准引用、调整因子和窗口序列化为 canonical JSON 并计算 SHA-256 `snapshot_hash`，逐表持久化，之后不再变化。
- **计算口径**：批次单位蒸汽 = 蒸汽消耗 × 批准因子 / 产量；两期均值之差用 Welch t 区间（不假设等方差）给出 95% 置信边界；节省量 = −单位蒸汽差 × 验证期产量，节省金额按蒸汽单价折算。
- **职责分离**：工程角色可以登记、冻结和提出替代计算，但**最终收益只能由 reviewer 确认**；已确认的期间可关闭结算。
- **替代计算**：复核者接受剔除特定批次的提案时，系统用相同窗口冻结一份子核验（父核验标记为 `superseded`），子核验仍需独立确认。
- **校准失效**：登记失效日后，系统定位快照中校准在观测时点有效、且实际进入结论的读数：`frozen`/`confirmed` 核验转入 `calibration_hold`（暂停结算、禁止确认，需重新冻结）；`closed` 期间状态不变，只写更正披露记录；`superseded` 核验仅披露。
- **解释性**：`explain` 输出每条有效测量来自哪些读数与校准证书、被排除批次及原因、缺失/异常/迟到清单、置信区间口径和金额推导公式。

## HTTP 接口（节选）

写接口均通过 `X-Actor-Id` 标识操作者，除健康检查外请求体携带 `request_id` 实现幂等。

- `POST /retrofit/boundaries`、`/meters`、`/calibrations`、`/batches`、`/readings`
- `POST /retrofit/factors`（提议）、`POST /retrofit/factors/review`（复核批准/驳回）
- `POST /retrofit/verifications`（冻结快照并计算）
- `POST /retrofit/alternates`（工程提替代计算）、`POST /retrofit/alternates/review`
- `POST /retrofit/verifications/confirm`（reviewer 确认最终收益）
- `POST /retrofit/verifications/close`
- `POST /retrofit/calibration-failures`（校准失效，自动暂停/披露）、`POST /retrofit/corrections/clear`
- `GET /retrofit/verifications?boundary_id=...`
- `GET /retrofit/verifications/{id}`
- `GET /retrofit/verifications/{id}/explain`（节省量、单位成本变化与置信边界的来源解释）

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

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，并完成技改核验的登记、批准调整因子、冻结快照、独立确认、关闭结算与校准失效更正披露，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
