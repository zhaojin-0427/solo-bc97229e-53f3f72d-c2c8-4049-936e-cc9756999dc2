# SQL 字段溯源 · 敏感列影响分析（Flask + Docker Compose）

面向「客户分群视图交付广告代理商前，评估手机号等敏感字段影响」的静态血缘工具。
粘贴**源表有序字段及敏感标记**和**单条 SQLite SELECT**，逐列展示每个输出列
源自哪些源字段、经由哪些 CTE 列传播，并高亮敏感来源。

同时提供**源列改名迁移页**：CRM 侧客户字段改名后，数仓工程师选择源表/旧列/新列，
整批预览并同步全部具名分群视图定义，保持下游列接口不变。

**只做静态语法分析与字段级溯源，不连接数据库、不执行 SQL，也不判断表达式能否脱敏。**

---

## 1. 快速启动（Docker Compose）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> 即字段溯源页（原功能，打开即自动分析示例）；
改名迁移页位于 <http://localhost:8000/migration>，两页顶部可互相导航。

停止：

```bash
docker compose down          # 保留 SQLite 数据卷 lineage-data
docker compose down -v       # 同时删除数据卷
```

容器内使用 gunicorn 启动 `server:app`，健康检查访问 `/`。
SQLite 数据库位于容器内 `/data/lineage.db`，通过命名卷 `lineage-data` 持久化，
容器重建/重启后源表说明、视图 SQL 与迁移历史完整保留。
可用环境变量 `LINEAGE_DB_PATH` 或 `LINEAGE_DATA_DIR` 覆盖数据库位置。

## 2. 本地直接运行（可选，需自行准备 Python 3.11 与 Flask）

```bash
pip install -r app/requirements.txt
python app/server.py        # 监听 8000
```

核心溯源引擎（`app/lineage/`）仅依赖 Python 标准库，无需 Flask 即可测试：

```bash
python -m unittest discover -s tests -v
```

---

## 3. 输入格式

### 源表结构（左栏，每行一张表，`#` 注释，空行忽略）

```
表名(字段1, 字段2*, 字段3, ...)
```

- 字段名后紧跟 `*` 表示**敏感字段**，如 `mobile*`、`id_no*`
- 字段书写顺序即 `SELECT *` 的展开顺序
- 标识符支持字母/数字/`_`/`$`；SQL 中还支持 `"..."`、`` `...` ``、`[ ... ]` 引用

示例：

```
customer(cust_id, name, mobile*, id_no*, city)
orders(order_id, cust_id, amount, status)
```

### SQL（右栏，单条 SELECT，末尾允许一个分号）

## 4. 支持的语法白名单（其余一律拒绝并报错）

| 类别 | 支持 |
| --- | --- |
| CTE | 非递归 `WITH`（可多个、可嵌套，内层可见外层）、CTE 列别名清单；拒绝 `RECURSIVE` |
| SELECT | `SELECT [ALL]`、列别名（`AS` 或裸别名）、`*`、`表名.*`/`别名.*` |
| 输出表达式 | 列引用、常量（数字/字符串/NULL）、`+ - * /` 四则运算与括号、`CASE`（简单与搜索式） |
| 谓词（WHERE / ON / CASE WHEN） | 比较、`AND`/`OR`/`NOT`、`[NOT] IN (常量/表达式列表)`、`[NOT] LIKE`、`IS [NOT] NULL` |
| 表连接 | `INNER JOIN`、`LEFT [OUTER] JOIN ... ON` |
| 集合 | `UNION ALL`（按列位置合并依赖） |
| 子查询 | **仅允许出现在 CTE 体内**；FROM 内联子查询、IN/标量子查询等全部拒绝 |

明确拒绝：普通 `UNION`、`INTERSECT`、`EXCEPT`、`DISTINCT`、`GROUP BY`、`HAVING`、
`ORDER BY`、`LIMIT/OFFSET`、窗口、`RIGHT/FULL/CROSS/NATURAL JOIN`、逗号隐式连接、
无 `ON` 的连接、函数调用（含 `CAST`/`COALESCE`）、`BETWEEN`、`EXISTS`、
字符串连接 `||`、取模 `%`、多语句、任何非 SELECT 语句。

---

## 5. 两类依赖（分别追踪、分别传递，字段可兼有）

- **值依赖（value）**：输出表达式实际引用的列。沿输出表达式传播；
  `CASE` 的 `WHEN` 条件、简单 CASE 的操作数、`THEN/ELSE` 全部计入。
- **条件依赖（condition）**：沿途 `WHERE` 与 `JOIN ON` 中出现的列，
  **作用于所在查询块的全部输出列**（即使该输出表达式并未引用它们），
  并随 CTE/UNION 继续向下游传递。

一个源字段可同时属于两类（如 `SELECT mobile FROM t WHERE mobile IS NOT NULL`）。
前端用红色（值敏感）、橙色（条件敏感）、紫色（兼有）区分。

### 传播规则要点

- 星号按源表字段顺序展开；`表名.*` 仅展开该来源。
- `UNION ALL` 按**列位置**对齐，合并各分支同位置列的全部依赖；列数不一致报错。
- 未知表/列、裸列名在多表中产生歧义、限定名不存在等情况一律报错，不做猜测。
- 常量输出列没有值依赖；但只要所在查询块存在 WHERE/ON，仍会带条件依赖。
- 工具不推断「某个表达式脱敏后是否安全」，只如实呈现来源与路径。

---

## 6. 目录结构

```
.
├── docker-compose.yml          # 服务编排，端口 8000
├── app
│   ├── Dockerfile              # python:3.11-slim + gunicorn
│   ├── requirements.txt
│   ├── server.py               # Flask 路由：/ 与 POST /api/analyze
│   ├── lineage
│   │   ├── tokenizer.py        # 词法分析
│   │   ├── ast_nodes.py        # AST 定义
│   │   ├── parser.py           # 递归下降解析（白名单语法）
│   │   ├── schema.py           # 源表结构与敏感标记解析
│   │   ├── analyzer.py         # 值/条件双轨依赖传播引擎
│   │   ├── demo_data.py        # 内置示例
│   │   └── errors.py           # 带行列号的错误类型
│   ├── templates/index.html    # 单页界面
│   └── static/{style.css,app.js}
└── tests/test_lineage.py       # 62 个标准库 unittest 用例
```

## 7. HTTP 接口

### 溯源（原功能）

`POST /api/analyze`，请求体：

```json
{ "schema": "customer(cust_id, mobile*)", "sql": "SELECT cust_id FROM customer" }
```

成功 `200`：`{"ok": true, "result": { "outputs": [...], "ctes": [...], "base_tables": [...] }}`；
语法/溯源错误 `400`：`{"ok": false, "error": "第 x 行第 y 列：..."}`。

### 改名迁移与视图管理（SQLite 持久化）

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/state` | 一次性获取源表说明、全部视图、迁移历史 |
| `PUT /api/schema` | 保存源表结构说明（保存前做格式校验） |
| `POST /api/views` | 新增具名视图（保存前用溯源引擎完整校验） |
| `GET/PUT/DELETE /api/views/<id>` | 查看 / 编辑 / 删除视图 |
| `POST /api/rename/preview` | 整批改名预览，返回各视图新 SQL 与防重放 token |
| `POST /api/rename/confirm` | 凭 token 单事务原子确认（源表+全部视图+迁移记录） |
| `GET /api/migrations/<id>` | 查看某次迁移保留的完整结果 |

---

## 8. 改名迁移的正确性保证

### 8.1 只改真正绑定到目标源列的引用

改名器复用与溯源引擎一致的绑定解析规则（限定名按别名/表名、裸列名歧义即拒绝），
逐 `ColumnRef` 判断其来源是否直通目标源列，因此：

- **同名字符串字面量不改**：`WHERE name = 'mobile'` 中的 `'mobile'` 原样保留。
- **其他表/CTE 的同名列不改**：`log.mobile` 不会因 `customer.mobile` 改名而变化。
- **嵌套 CTE 遮蔽正确处理**：CTE 列别名清单、显式 `AS 别名` 固定下游接口名，
  其下游引用保持旧名；仅 CTE 体内真正绑定源列的表达式被改写。
- **表别名原样保留**：只替换 `别名.列名` 中的列名 token，别名本身不动。
- **星号**：仅当展开结果包含目标列时，才把 `*` / `表名.*` 就地展开为显式列清单
  （多来源时补限定名防歧义），只改目标列名，列顺序与其余列完全不变；
  不涉及目标列的星号原样保留。
- **UNION ALL**：按列位置处理，输出列名遵循 SQLite「取第一分支列名」规则；
  首分支显式别名保持旧名时，下游接口名与星号都保持不变。
- 标识符按词法 token 精确替换，`mobile2`、`mobile_backup` 等相似名不会被误伤。

### 8.2 整批拒绝与前后等价

- 新列名非法、与源表现有列重名、任一视图解析或溯源失败 → **整批拒绝**，无部分结果。
- 每个视图改名后都在**新源表结构**上重新溯源，并与改名前模型逐列比对：
  输出列名（位置对齐）、列顺序、值来源、条件来源、敏感标记必须一致，
  仅允许目标源列名称变化、星号展开为显式列、补充表限定名。
- 敏感标记随列保留：源表上的 `*` 标记迁移到新列名，下游敏感性结论不变。

### 8.3 预览-确认与原子持久化

- 预览只做计算不落库，返回覆盖「源表文本 + 全部视图名称与 SQL + 改名三要素」的
  SHA-256 token；预览后任一输入被编辑，确认时 token 不匹配即拒绝。
- 确认在**单个 SQLite 事务**内重算校验后一次写入源表、全部视图与迁移历史；
  任何异常或进程中断都由事务回滚，不会留下半迁移（测试中以提交钩子模拟中途崩溃验证）。
- 迁移成功后结果随 SQLite 文件持久化，进程重启后源表、视图与历史完整可查，
  网页可查看并一键复制每个视图的新 SQL 供部署。

---

## 9. 目录结构

```
.
├── docker-compose.yml          # 服务编排 + 命名卷 lineage-data，端口 8000
├── app
│   ├── Dockerfile              # python:3.11-slim + gunicorn
│   ├── requirements.txt
│   ├── server.py               # Flask 路由：溯源页、迁移页与全部 JSON API
│   ├── lineage
│   │   ├── tokenizer.py        # 词法分析
│   │   ├── ast_nodes.py        # AST 定义
│   │   ├── parser.py           # 递归下降解析（白名单语法）
│   │   ├── schema.py           # 源表结构与敏感标记解析
│   │   ├── analyzer.py         # 值/条件双轨依赖传播引擎
│   │   ├── rename.py           # 源列改名引擎：绑定解析、星号展开、等价校验
│   │   ├── storage.py          # SQLite 持久层：原子事务、token、演示种子数据
│   │   ├── demo_data.py        # 溯源页内置示例
│   │   └── errors.py           # 带行列号的错误类型
│   ├── templates/{index,migration}.html
│   └── static/{style.css,app.js,migration.css,migration.js}
└── tests/                      # 114 个标准库 unittest 用例
    ├── test_lineage.py         # 原溯源引擎
    ├── test_rename.py          # 改名引擎（遮蔽/星号/UNION/等价守卫/边界）
    ├── test_storage.py         # SQLite 事务、token 失效、崩溃回滚、重启保留
    └── test_api.py             # Flask HTTP 端到端流程
```
