# 港口调度：泊位航道 + 危险品堆场与装船计划

纯Python标准库实现的港口调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。
包含两部分：原有泊位与航道调度，以及危险品集装箱**堆场落位**与**装船计划**。

## 模块结构

泊位调度（原有）：

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突。
- `src/repository.py`：泊位记录SQLite建表、事务和查询。
- `src/service.py`：泊位用例编排、权限检查、乐观并发和审计。
- `src/audit.py`：泊位事件时间线。

堆场与装船（新增，资料/规则/事务/页面分开）：

- `src/yard_data.py`：**堆场资料**——箱区目录（单垛限重/限层/垛位数）、岸桥目录、危险品类别与不相容矩阵。
- `src/yard_rules.py`：**规则**——同垛不相容判定、重量与层数限制、落位选垛（先同类垛后空垛）、理货闸口、吊机/箱区时段冲突。纯函数，不碰数据库。
- `src/yard_repository.py`：**事务**——箱区/箱子/装船计划表、垛位快照、`BEGIN IMMEDIATE`锁内复核后提交落位、改配、计划锁定/释放/完成。
- `src/yard_service.py`：用例编排与角色权限（登记/落位/理货/计划四组角色）。
- `src/yard_api.py`：堆场HTTP路由，只做参数解析。

页面：

- `static/index.html`：泊位调度演示页（`GET /`）。
- `static/yard.html`：堆场与装船演示页（`GET /yard`）。

测试 `tests/`：泊位流程/规则/失败用例，堆场规则、完整流程（登记→落位→理货→计划→装船）、待落区原因、并发落位与失败场景。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

服务启动时自动建表并写入箱区/岸桥主数据（`yard_data.py`中的目录，`INSERT OR IGNORE`幂等）。

## 堆场业务规则

- **登记**：箱号（6-12位字母数字，唯一）、危险品类别（IMDG目录）、重量、意向/到港时段。登记后状态为`pending`（待落区）。
- **落位**：同一垛只放相容类别（同类优先叠加，不相容自动开新垛）；垛内总重不得超过箱区单垛限重；层数不得超过箱区限层；写操作在事务锁内重读垛位快照复核，两个班次并发也不会串垛。
- **放不下**：箱子留在待落区，记录`pending_reason`：`no_compatible_stack`（无相容空垛）、`block_weight_exceeded`（超重）、`block_layers_exceeded`（限层）、`block_locked`（箱区已被装船计划锁定）。
- **理货**：箱区完成理货后才能编制装船计划；装船完成后理货状态自动复位。
- **装船计划**：锁定箱区、吊机、时段（小时）。同一吊机同一时段只能服务一船；同一箱区同一时段只能锁定给一船（首尾相接不冲突）；箱区未完成理货不能编计划。
- **改配/改期**：已落位箱调整位置走`reallocate`，在同一事务中**先释放原箱位**再占新位；未执行的计划可取消释放箱区与吊机时段。
- **装船完成**：箱区内全部已落位箱转为`loaded`，计划置`done`，箱区锁定与理货复位；箱区为空时不允许完成。

角色：`gate_clerk`（登记）、`yard_planner`（落位/改配）、`tally_clerk`（理货）、`vessel_planner`（装船计划）、`admin`（全部）。

## 堆场接口

除`/health`、`/`、`/yard`外均需`X-User-Id`、`X-Role`头。

资料与查询：

- `GET /api/yard/blocks` / `GET /api/yard/blocks/{code}`：箱区列表 / 详情（含各垛快照与锁定计划）。
- `GET /api/yard/cranes`：岸桥目录。
- `GET /api/yard/containers?state=&block=&limit=` / `GET /api/yard/containers/{id}`。
- `GET /api/yard/plans?state=` / `GET /api/yard/plans/{id}`。
- `GET /api/yard/stats`：箱子/计划状态统计、已理货箱区数。
- `GET /api/yard/blocks/{code}/audit`、`/api/yard/containers/{id}/audit`、`/api/yard/plans/{id}/audit`：审计时间线。

写操作（POST，带`expected_version`乐观锁）：

- `POST /api/yard/containers`：`{"data":{"container_no":"CBHU2001","hazard_class":"3","weight_t":10,"arrival_hour":8}}`。
- `POST /api/yard/containers/{id}/place`：`{"expected_version":1,"data":{"block_code":"A01"}}`；放不下返回`state=pending`及原因。
- `POST /api/yard/containers/{id}/reallocate`：改配，先释放原箱位。
- `POST /api/yard/blocks/{code}/tally`：`{"expected_version":1,"data":{"tally_completed":true}}`。
- `POST /api/yard/plans`：`{"data":{"vessel":"MV1","block_code":"A01","crane_code":"QC-01","start_hour":8,"end_hour":14}}`。
- `POST /api/yard/plans/{id}/complete` / `POST /api/yard/plans/{id}/cancel`（cancel需`data.reason`）。

泊位调度原接口（`/api/records`等）保持不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
