# 港口泊位、危险品堆场与装船调度

纯Python标准库实现的港口调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。
包含两部分：泊位与航道调度（原有）和危险品箱堆场与装船计划（扩展）。

## 模块结构

### 泊位调度
- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突。
- `src/repository.py`：SQLite建表、事务和查询（泊位表与堆场表）。
- `src/service.py`：泊位用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：两类审计时间线（泊位记录 / 堆场实体）。

### 堆场与装船计划（资料、规则、事务、页面分离）
- `src/yard_data.py`：**堆场资料**——危险品类别、不相容类别对、班次、箱区/垛位/吊机主数据（纸质平面图数字化），首次启动自动写入。
- `src/yard_rules.py`：**堆场规则**——同垛类别不相容判定、层数与每垛总重限制、选垛算法、装船吊机时段冲突、理货闸门、计划状态转换。纯函数，不碰数据库。
- `src/yard_service.py`：堆场用例编排（角色权限、输入校验、事务调用）。
- `src/repository.py`：**事务**——落位、改配、释放、理货、装船锁定/执行均在单事务内完成，带乐观版本。
- `static/yard.html`：**页面**——箱区平面图、到港登记、待落区、落位/改配/释放、理货、装船计划。
- `tests/test_yard_rules.py`：规则单测；`tests/test_yard_workflow.py`：完整流程；`tests/test_yard_failures.py`：失败与权限场景。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`。服务启动时自动建表并写入箱区/垛位/吊机资料。
- `/`：泊位调度演示页
- `/yard`：堆场与装船计划页面

## 堆场业务规则

1. **登记**：箱号（4字母+7数字）、危险品类别（IMDG 1-9类）、重量、目标箱区、到港日期与班次；登记后进入待落区。
2. **落位**：同一垛不能混放不相容类别（1类爆炸品、7类放射性物质只能与同类同垛；2/3/4类远离5类氧化剂）；不得超过箱区最大层数与每垛最大总重。自动按垛位顺序选位，也可指定垛位；任何垛位都放不下时**留在待落区并记录具体原因**（超重/层数/类别不相容）。
3. **理货**：每个箱区维护数据版本，落位/改配/释放/装船后版本前进；理货记录对应版本，版本不一致即理货失效。
4. **装船计划**：编制时锁定箱区、吊机、日期时段与箱号清单（箱子必须已落位且在锁定箱区）；同一吊机时段只能服务一船（时间窗重叠即冲突，已取消计划不占位）；**箱区未完成理货或理货过期不能装船**；装船执行后箱位自动释放。
5. **改配**：改配先释放原箱位（同事务），再在目标箱区重新选位；放不下仍留待落区；原箱区数据版本前进使旧理货失效。

角色：`yard_planner`（登记）、`yard_worker`（落位/改配/释放）、`tally_clerk`（理货）、`vessel_planner`（装船计划）、`port_controller`/`admin`（全部）。

## 主要接口

泊位：`GET /api/records`、`GET /api/records/{id}`、`GET /api/records/{id}/audit`、
`POST /api/records`、`POST /api/records/{id}/actions/{action}`、`GET /api/stats`。

堆场与装船（GET均支持`state`/`limit`）：
- `GET /api/yard/reference`：类别、班次、箱区、垛位、吊机、理货版本。
- `GET /api/zones`：箱区平面图（每垛层数/总重/类别、待落数）。
- `GET /api/zones/{code}/audit`：箱区审计时间线。
- `POST /api/containers`：登记（`{"data":{箱号,类别,重量,箱区,到港日期,到港班次}}`）。
- `GET /api/containers`、`GET /api/containers/{id}`、`GET /api/containers/{id}/audit`。
- `POST /api/containers/{id}/actions/place|reassign|release`：
  落位（`preferred_stack`可选）、改配（`new_zone_code`+`preferred_stack`）、释放（`reason`）。
- `POST /api/tallies`：完成箱区理货（`{"data":{"zone_code":"B"}}`）。
- `POST /api/plans`：编制装船计划（`{"reference":"...","data":{船名,箱区,吊机,日期,起/止小时,箱号清单}}`）。
- `GET /api/plans`、`GET /api/plans/{id}`、`GET /api/plans/{id}/audit`。
- `POST /api/plans/{id}/actions/confirm|execute|cancel`：锁定（校验吊机冲突+理货闸门）、装船、取消。

除`/health`、`/`、`/yard`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。
冲突返回409（吊机时段、版本冲突、理货闸门），校验失败422，权限不足403。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

共30个测试：泊位流程/规则/失败（6），堆场规则计算、落位-理货-装船完整流程、超重/不相容/吊机冲突/理货失效/权限等失败场景（24）。
