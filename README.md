# 道路养护工时纠错服务

道路养护工时必须归到预算科目才能入仓。本服务用 FastAPI + SQLite 管理同月三张表，
判定每条工时的完整路径（工单 → 项目 → 预算科目），对重复工时提供事务化纠错。

## 一、数据模型（同月每表行号唯一）

| 表 | 含义 | 关键字段 | 主键 |
| --- | --- | --- | --- |
| `work_hour` | 工时表 | 工单号 `work_order`、分钟数 `minutes` | `(month, row_id)` |
| `dispatch` | 分派表 | 工单 → 项目 | `(month, row_id)` |
| `attribution` | 归科表 | 项目 → 预算科目 `budget_subject` | `(month, row_id)` |
| `correction_audit` | 纠错审计 | 撤除的表/行号、对应工时行 | 自增 id |

## 二、供数核算规则（集合式，不逐条展开路径）

完整路径 = 工时按工单号连分派、分派按项目连归科。核算由**一条集合式 GROUP BY SQL**
完成（见 `app/services.py` 的 `PATH_SQL`），服务端不按工时逐行 JOIN、不在 Python 追链：

- `path_count = 1`：**可供数**，返回工时及唯一预算科目；
- `path_count = 0`：扣留，原因 `no_path`；
- `path_count >= 2`：扣留，原因 `duplicate`。**两条路径即使落到同一预算科目也算重复**
  （计数按 分派×归科 组合，而非按科目去重）。

## 三、纠错事务语义

`POST /months/{month}/corrections` 指定工时行号 + 要撤除的一条分派或归科记录，
在单个 `BEGIN IMMEDIATE` 事务内完成：

1. 以**撤除前已提交数据**为快照校验：工时行、被撤记录必须存在；被撤记录必须在该工时
   的某条完整路径上；目标工时当前必须为重复（路径数 ≥ 2）；
2. 撤除记录并写审计；
3. 撤除后重新集合核算，强制两条不变量：
   - 目标工时恰好剩 1 条路径（能供数）；
   - 原本可供数的工时全部保有科目（共享归科被误撤会在此被拦）；
4. 校验通过才 COMMIT，撤除与供数变化一起生效，返回 `restored_work_row_ids`
   （本次由扣留恢复为可供数的全部工时行号）；
5. 任一校验失败即 ROLLBACK，数据保持撤除前状态，响应体用中文说明原因
   （404 不存在 / 422 撤错记录 / 409 违反供数或保科规则）。

并发：所有写事务均为 `BEGIN IMMEDIATE` + WAL + `busy_timeout`，SQLite 写锁使并发纠错
在数据库层串行化，后到事务读到的必是前一个已提交版本，满足“以本次撤除前的已提交
数据为准”；锁冲突返回 409 提示基于最新数据重试。

## 四、启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs 即可录入、查询、纠错
```

SQLite 文件落在命名卷 `maintenance-data:/data/maintenance.db`，容器重建数据不丢。

## 五、接口一览（均在 /docs 可试）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/months/{month}/work-hours` | 录入工时表（`replace=true` 整月覆盖，`false` 追加，行号冲突 409） |
| POST | `/months/{month}/dispatches` | 录入分派表 |
| POST | `/months/{month}/attributions` | 录入归科表 |
| GET | `/months/{month}/supply` | 供数视图：可供数带科目，扣留带路径数与原因 |
| POST | `/months/{month}/corrections` | 纠错：撤除一条分派/归科 |
| GET | `/months/{month}/audit` | 纠错撤除审计 |
| DELETE | `/months/{month}` | 测试辅助：清空某月 |
| GET | `/health` | 健康检查（容器 HEALTHCHECK） |

`month` 格式固定 `YYYY-MM`。示例：

```bash
curl -X POST localhost:8000/months/2026-11/work-hours -H 'Content-Type: application/json' \
  -d '{"rows":[{"row_id":1,"work_order":"WO1","minutes":120}]}'
curl localhost:8000/months/2026-11/supply
curl -X POST localhost:8000/months/2026-11/corrections -H 'Content-Type: application/json' \
  -d '{"work_row_id":1,"remove":"dispatch","remove_row_id":2}'
```

## 六、实测结果

本机无 Docker 守护进程，使用与镜像一致的 Python 3.11 + 同款依赖（requirements.txt 钉版）
实跑（真实 uvicorn 进程 + HTTP + 线程并发）：

### 6.1 功能 e2e（`tests/run_e2e.py`）：全部断言通过

覆盖
  - 同科目重复路径仍判重复（两条分派同项目同科目，path_count=2 扣留）；
  - 无路径工时 path_count=0、原因 `no_path`；
  - **共享归科**：撤除被 H2/H3 共用的归科记录会打掉 H3 的唯一路径 → 409 回滚，
    H3 保有科目、其余数据不变；
  - 撤除无关记录 422、对可供数工时撤除 409，失败后数据保持原状；
  - 正确撤除后目标恢复供数、返回恢复行号、其他可供数工时不受影响，审计落表；
  - **并发纠错**：两个不同重复工时同时撤除 → 均成功且各自科目正确；同一工时两个
    并发撤除 → 恰好一个成功，另一个基于已提交数据失败，最终数据自洽。
  - 历史上修过一个真实缺陷：FastAPI 同步依赖与端点落在不同线程池线程，sqlite3 默认
    跨线程检查在回滚路径抛 500；已通过 `check_same_thread=False`（每请求独占连接，
    写一致性仍由 `BEGIN IMMEDIATE` 保证）修复并复测通过。

### 6.2 规模与并发压力（`tests/run_stress.py`，本次新增）

数据集：5000 工时、4600 分派（200 条集中到同一项目/归科）、4401 归科。

- **集合式核算实测**：全月供数视图（含一次完整三表 GROUP BY）**约 140–170 ms**；万级行下
  单次纠错事务（撤除前快照 + 撤除/审计 + 撤除后重算，共两次全月核算）**约 230 ms**。
  两次自包含实跑（测试脚本自行拉起 uvicorn）数值随机器负载略有浮动。
  `EXPLAIN QUERY PLAN` 确认三表全部走索引（`idx_dispatch_month_wo`、
  `idx_attribution_month_proj`），服务端不逐行展开路径，耗时只随源表行数与关联键数增长。
- **集中归科**：100 个重复工时（每个 2 条分派、同项目同科目）全部 path_count=2 扣留且
  原因 `duplicate`；4400 个单路径工时供数并带唯一科目；500 个无路径工时 path_count=0、
  `no_path`；扣留响应不含预算科目。
- **撤除共享归科**：撤除被上百工时共用的归科记录 → 409 回滚，审计条数不变，
  原本可供数的工时全部保科；不存在记录 404、不在路径上的记录 422，均不动数据。
- **并发录入**：4 次纠错连续持写锁期间，4 个其他月份并发录入（每月工时/分派/归科
  各一批）全部在 `busy_timeout` 内串行通过，无超时/500/409；外部服务实测请求耗时
  10–28 ms，自包含实跑因同机争用约 0.2–1.0 s（远低于 30 s 超时），各月最终数据自洽。
- 另跑 **8 路并发纠错**：8 个独立重复工时同时撤除，8 个请求全部 200、恢复行号
  [1..8]、科目逐个正确、审计 8 条。
- 边界：3 条路径计数准确（按 分派×归科 组合计 3，不按科目去重）；撤除后仍剩 2 条
  路径的纠错被 409 拦截并回滚到 path_count=3；非法月份 422、批内重复行号 422、
  追加行号冲突 409；`/docs` 与 `/openapi.json` 返回 200。
- `docker-compose.yml` YAML 解析通过，全部 Python 源 `py_compile` 通过。

### 6.3 本次代码改动

- `app/services.py` `_check_unique`：原实现对每批行号做 `list.count` 逐条计数，
  随批量行数平方级增长，与“核算规模只应由源表行数决定”相违；改为单次遍历的
  集合查重（O(n)），错误回执与状态码不变。
- 新增 `tests/run_stress.py`：集中归科、撤除共享归科、跨月并发录入三类压力场景，
  仅依赖标准库，可指向外部已启动服务（`SERVER_EXTERNAL=1`）或自行拉起 uvicorn。

## 七、余项

1. 当前环境无 Docker 守护进程，镜像未实际 `docker compose build`，仅做了 compose/Dockerfile
   静态校验；逻辑已用与镜像同款的钉版依赖（Python 3.11）在本机完整试跑。部署时
   `docker compose up --build` 后从 `http://localhost:8000/docs` 操作即可。
2. 入参为 JSON 批量录入；如需 Excel/CSV 导入可在现有服务上层加解析，表结构无需改动。
3. 认证鉴权、分页未纳入本次范围；服务默认监听 8000，建议部署在内网或网关之后。
4. 测试仅用标准库：`tests/run_e2e.py` 自起 uvicorn 跑功能 e2e；`tests/run_stress.py`
   默认自起服务（端口 8001），也可先启动服务再以 `SERVER_EXTERNAL=1` 指向它跑压力场景。
   若团队引入 pytest，可将断言直接迁移。
