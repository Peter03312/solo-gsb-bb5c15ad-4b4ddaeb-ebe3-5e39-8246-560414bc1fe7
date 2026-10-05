# 微孔板配液规划服务（Microplate Dilution Planner）

给定母液、稀释液、目标孔、中间液容器和移液器能力，计算一份**可直接执行的精确转移计划**：
每一步的来源、目的、体积，以及各孔的最终浓度。

- 体积一律使用**整数纳升（nL）**；
- 浓度一律使用**有理数**（`fractions.Fraction`，输入如 `"2/3"`、`"0.25"`、`"40"`）；
- 中间液可逐级稀释，容器间转移构成的图**不得有环**；
- **目标孔只能是目的，永不作为来源**；
- 每次移液器转移必须满足 `min ≤ 体积 ≤ max` 且为 `step` 的整数倍；
- 来源不得透支、目的容器/孔不得溢出；
- 混合严格按溶质质量守恒（`浓度 × 体积` 线性混合），所有目标的体积与浓度**精确达成**。

无解时返回受阻目标与具体约束，**不会返回部分计划**。非法数值或非法引用以 `400` 明确拒绝。
同一 `X-Request-ID` + 相同请求体重试返回原冻结计划（`200`）；同 ID 异内容返回 `409 Conflict`。

**库存模式（本轮新增）**：多份配液计划可共享同一批母液。批次先以唯一 ID、浓度、初始体积
登记（`POST /batches`）；计划请求改用 `batch_ids` 引用批次，求解器按**当前可用量**
（初始 − 已预留 − 已用）求解，计划实际从各批次转出的体积在提交时**原子预留**，
并发提交不会超卖。预留可**确认**（计入已用，不可再取消）或**取消**（归还可用量，只归还一次）。
详见第 4 节。

---

## 1. 启动

依赖：Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

启动时会：

1. 拉取 `postgres:16-alpine` 并健康检查通过；
2. 在应用镜像内 `pip install -r requirements.txt`；
3. 应用启动时自动连接数据库并执行建表（`plans`、`batches`、`inventory_plans`、`reservations`，均为 `CREATE TABLE IF NOT EXISTS`，旧数据卷可直接升级）。

服务地址与端口：

- API：`http://localhost:8000`
- 交互文档（OpenAPI / Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/health`

停止并保留数据：`docker compose stop`；连同数据卷清除：`docker compose down -v`。

---

## 2. 配置项（环境变量）

均位于 `docker-compose.yml`，可通过同名环境变量或 `.env` 覆盖：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVICE_PORT` | `8000` | 宿主机映射端口；容器内固定监听 8000 |
| `SERVICE_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `DATABASE_URL` | `postgresql://planner:planner@db:5432/planner` | PostgreSQL 连接串 |
| `POSTGRES_USER` | `planner` | 数据库用户 |
| `POSTGRES_PASSWORD` | `planner` | 数据库密码 |
| `POSTGRES_DB` | `planner` | 数据库名 |
| `POSTGRES_PORT` | `5432` | 宿主机数据库端口 |

---

## 3. HTTP API

### `GET /health`

返回 `{"status": "ok"}`，同时探活数据库。

### `POST /plans`

请求头：

- `X-Request-ID`（**必填**）：请求号，用于重试幂等与冲突检测（两种模式共用一个命名空间）。

请求体字段（两种模式**二选一**，其余字段相同）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `stocks[]` | array | **经典模式**：母液随请求给出：`id`、`concentration`（整数或有理数字符串，非负）、`available_nl`（正整数） |
| `batch_ids[]` | array | **库存模式**：引用已登记批次的 id（非空、不重复）；浓度与可用量取自库存 |
| `diluent.available_nl` | int | 稀释液可用体积（nL，≥0） |
| `wells[]` | array | 目标孔：`id`、`final_volume_nl`（正整数）、`final_concentration`、可选 `capacity_nl` |
| `containers[]` | array | 有容量上限的中间液容器（数量有限）：可选 `id`、`capacity_nl`（正整数）。省略 `id` 时返回 `C0,C1,...` |
| `pipette` | object | `min_nl`、`max_nl`、`step_nl`，均为正整数 |
| `default_well_capacity_nl` | int | 未给单孔容量时的孔容量 |
| `limits` | object | 可选：`max_depth`（默认 3）、`max_search_nodes`（默认 200000）、`max_enumeration_units`、`max_factor` |

`stocks` 与 `batch_ids` 同时给出或同时缺失均返回 `400`。

**响应（经典模式，与既往版本一致）：**

- `201 Created`：新计划。体含 `status:"feasible"`、`request_id`、`plan`：
  - `plan.transfers[]`：`step`（自 1 连续编号）、`source`、`destination`、`volume_nl`；
  - `plan.intermediates[]`：`id`、`container`、`concentration`、`produced_nl`、`remaining_nl`、`recipe{high,low,high_fraction}`；
  - `plan.wells[]`：`id`、`final_volume_nl`、`final_concentration`。
- `200 OK`：相同请求号 + 相同内容重试，返回原冻结计划。
- `409 Conflict`：相同请求号但内容不同。
- `422 Unprocessable Entity`：请求合法但**无解**；体含 `blocked[]`（受阻孔与约束代码）、`reason`、`constraints`（全局约束，如 `insufficient_total_solute`、`insufficient_total_volume`、`target_above_max_stock_concentration`、`container_shortage` 等）。无解结果同样被冻结，重试返回 `422`。
- `400 Bad Request`：非法数值/类型/引用（负数、0、浮点浓度、重复 id、移液器区间非法、终体积超孔容量等）。

节点引用的 `kind` 为 `stock` / `diluent` / `intermediate` / `well`。

**响应（库存模式）：**

- `201 Created`：计划可行且各批次预留已原子提交。体含 `status:"feasible"`、`mode:"inventory"`、
  `state:"reserved"`、`plan`（结构同上）、`reservations[]`（`{batch_id, reserved_nl}`，
  即计划实际从各批次转出的体积；未使用的批次不出现）。
- `200 OK`：同号同体重试，返回原创建结果，**不会重复预留**。
- `409 Conflict`：同号异体。
- `422 Unprocessable Entity`：两种情形，均**不占库存、不冻结请求号**（库存是瞬态，稍后重试可能成功）：
  - 求解无解（含可用量不足导致的 `stock_overdraft`）：体含 `blocked[]`、`reason`、`constraints`，
    并附 `inventory`（各批次 `initial/reserved/used/available` 快照）说明原因；
  - 提交预留时库存被并发请求抢占：`reason:"insufficient_inventory"`，附 `shortage`
    （缺口批次的 `required_nl`/`available_nl`）。
- `400 Bad Request`：非法字段，或引用了未登记的批次（`detail` 列出未知批次 id）。

### `GET /plans/{request_id}`

取回冻结结果（可行 `200`，无解 `422`，不存在 `404`）。库存模式返回的是**创建时**冻结的
payload（`state` 恒为 `"reserved"`）；实时预留状态请用 `GET /plans/{request_id}/reservation`。

### `POST /batches`

登记母液批次：`{"id": "...", "concentration": "1000", "initial_nl": 2000000}`。

- `201 Created`：新批次。返回 `{id, concentration, initial_nl, reserved_nl, used_nl, available_nl}`。
- `200 OK`：同 id 同内容重复登记（幂等）。
- `409 Conflict`：同 id 异内容（`error:"batch_conflict"`，附已登记内容）。
- `400 Bad Request`：非法浓度/体积/id。

批次登记后浓度与初始体积不可变；库存只随计划预留/确认/取消变动。

### `GET /batches` / `GET /batches/{batch_id}`

列出全部批次或查询单个批次（含 `reserved_nl`、`used_nl`、`available_nl`）；未知批次 `404`。

### `POST /plans/{request_id}/confirm`

确认执行预留计划：`reserved → confirmed`，各批次 `reserved_nl` 转为 `used_nl`。

- `200 OK`：确认成功；**重复确认返回同一最终状态**（幂等，不会重复计数）。
- `409 Conflict`：计划已取消（`error:"state_conflict"`）。
- `400 Bad Request`：该请求号存在但不是库存预留计划（经典模式或库存失败计划）。
- `404 Not Found`：请求号不存在。

### `POST /plans/{request_id}/cancel`

取消预留计划：`reserved → cancelled`，各批次 `reserved_nl` 归还可用量，**只归还一次**。

- `200 OK`：取消成功；**重复取消返回同一最终状态**（不会重复归还）。
- `409 Conflict`：计划已确认（确认后计入已用量，不可取消）。
- `400` / `404`：同上。

确认与取消并发时只会有一个生效，两个调用最终观察到**同一个最终状态**。

### `GET /plans/{request_id}/reservation`

查询预留的实时状态：`{request_id, state, reservations[]}`，
`state ∈ reserved/confirmed/cancelled`；无库存预留时 `404`。

---

## 4. 调用示例

### 经典模式

```bash
# 1) 提交一个含两级逐级稀释的请求
curl -sS -X POST http://localhost:8000/plans \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: demo-run-001" \
  --data @examples/request.json

# 2) 相同请求号与内容重试 -> 200
# 3) 同号异内容 -> 409
# 4) GET 取回
curl -sS http://localhost:8000/plans/demo-run-001
```

完整脚本（含 200/409/422/400 各种情况）：

```bash
bash examples/curl.sh
python3 examples/call_api.py
```

示例（1000 浓度母液，移液器 500–1000nL、步进 100nL，目标 A1=400、A2=100）会生成
两个中间液（如 400 与 100），并把较大的批量安排进较大的容器，所有转移均为 500–1000nL。

### 库存模式

```bash
# 1) 登记批次（同 id 同内容重登 -> 200；异内容 -> 409）
curl -sS -X POST http://localhost:8000/batches \
  -H "Content-Type: application/json" \
  --data @examples/batch.json

# 2) 库存模式计划：batch_ids 引用批次，201 时各批次预留量已原子提交
curl -sS -X POST http://localhost:8000/plans \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: inv-demo-001" \
  --data @examples/inventory_request.json
#    响应含 reservations: [{"batch_id": "B-stock-1000", "reserved_nl": 1100}]

# 3) 查看批次库存（reserved/used/available）
curl -sS http://localhost:8000/batches/B-stock-1000

# 4) 确认执行（reserved -> used，可重复调用）或取消（归还可用量，只归还一次）
curl -sS -X POST http://localhost:8000/plans/inv-demo-001/confirm
curl -sS -X POST http://localhost:8000/plans/inv-demo-001/cancel   # 已确认则 409

# 5) 查询预留实时状态
curl -sS http://localhost:8000/plans/inv-demo-001/reservation
```

完整脚本：

```bash
bash examples/inventory.sh
python3 examples/call_inventory_api.py
```

### 库存一致性与并发语义

- 求解时使用批次的**当前可用量**（`initial_nl − reserved_nl − used_nl`）作为母液体积；
- 可行计划的各批次用量在**单个数据库事务**内冻结并预留：条件更新
  （`WHERE initial − used − reserved >= 本次用量`）保证并发提交**不会超卖**；
  任一批次不足则整个事务回滚——不冻结、不占库存，返回 `422 insufficient_inventory`；
- 同号并发提交：只有第一个请求完成冻结与预留，其余同体请求读回冻结结果返回 `200`，
  异体请求返回 `409`，库存只预留一次；
- 确认/取消通过原子状态翻转（`UPDATE ... WHERE state='reserved'`）实现：
  并发与重复调用下，库存移动（转已用 / 归还）**恰好执行一次**，所有调用观察到同一最终状态；
- 库存模式的失败（400/422）**不冻结请求号**：库存变化后同号重试可重新求解。

---

## 5. 求解方法与正确性

- 每个中间液 = 一个“高浓度父液 + 低浓度父液”的精确有理比例混合，整体依赖图是 **DAG**。
- 深度优先搜索为每个孔选择一到两个来源；当现有液体无法直接配制时，按整数稀释因子
  合成逐级稀释链（支持多父液中间液与多目标共享同一中间液）。
- 中间液批量体积自底向上取**最小可行值**：同时满足下游需求整数、两份父液份额均为
  步进整数倍、且每份都可拆成若干次 `[min,max]` 移液器转移；容器用精确二分图匹配分配。
- 生成计划后再做一次**独立的有理数质量守恒回放校验**：逐步检查来源不透支、目的不溢出、
  移液器区间、编号连续、目标体积/浓度精确、依赖无环。校验不过的计划绝不会返回。

本地无 Docker 时运行测试（仅依赖解释器标准库的求解器测试可直接跑）：

```bash
python3 tests/smoke.py
python3 tests/api_smoke.py   # 需已安装 fastapi/pydantic/httpx
python3 -m pytest tests/     # 完整测试（求解器 + HTTP + 库存并发）
```

## 6. 本轮变动（库存模式）

新增：

- `POST /batches`、`GET /batches`、`GET /batches/{id}`：批次登记与库存查询；
- `POST /plans` 支持 `batch_ids`（库存模式），与 `stocks`（经典模式）二选一；
  库存模式 201 返回 `mode/state/reservations`，预留原子提交；
- `POST /plans/{request_id}/confirm`、`POST /plans/{request_id}/cancel`、
  `GET /plans/{request_id}/reservation`：预留生命周期管理；
- 数据表 `batches`、`inventory_plans`、`reservations`（启动时自动建表）；
- 示例：`examples/batch.json`、`examples/inventory_request.json`、`examples/inventory.sh`、
  `examples/call_inventory_api.py`；测试：`tests/test_inventory.py`，`tests/api_smoke.py` 增加库存流程。

兼容性与内部调整：

- 经典模式请求格式、状态码、冻结语义、幂等冲突行为**完全不变**（请求哈希对经典请求
  与上一版逐字节一致，旧数据卷中的冻结结果重试仍返回 `200`）；
- `db.connect()` 现返回**连接池**（`asyncpg.Pool`）以支持并发事务；对路由层接口不变；
- 修复：字段校验器抛出的 `ValueError`（如浓度字符串无法解析、id 含保留前缀 `@`）
  此前会让 400 处理器崩溃为 500，现统一返回 `400 invalid_request`。

## 7. 目录结构

```
app/
  main.py          FastAPI 路由与状态码（/plans、/batches、confirm/cancel）
  models.py        Pydantic 请求/响应模型与浓度校验（含批次模型）
  planning.py      请求 -> 求解器映射、引用校验、幂等哈希、批次用量统计
  db.py            asyncpg 连接池、建表、冻结/取回、批次与预留事务
  solver/
    core.py        数据结构、移液器可行性、最小批量
    options.py     候选方案（直接/双源/新中间液/逐级稀释链）
    engine.py      回溯状态、批量与容器可行性
    solve.py       solve() 入口、计划组装、独立校验、无解诊断
examples/          request.json、batch.json、inventory_request.json、
                   curl.sh、inventory.sh、call_*_api.py
tests/             smoke.py（求解器）、api_smoke.py（HTTP+幂等+库存）、
                   test_solver.py、test_api.py、test_inventory.py（含并发）
Dockerfile, docker-compose.yml, requirements.txt
```
