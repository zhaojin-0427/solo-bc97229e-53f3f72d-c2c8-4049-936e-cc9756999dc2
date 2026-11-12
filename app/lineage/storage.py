"""SQLite 持久层：保存源表结构说明与具名视图 SQL。

设计要点：
- 单文件 SQLite（标准库 sqlite3，无额外依赖），路径可经环境变量
  LINEAGE_DB_PATH 覆盖，容器内默认 /data/lineage.db（挂载卷）。
- 首次启动自动建表并播种演示数据（源表 + 多个具名分群视图）。
- 所有写操作在单事务内提交；改名确认采用「先校验后一次事务写全部」，
  任何失败或进程中断都不会留下半迁移（原子提交 + journal 回滚）。
- 不存放任何连接信息，不执行视图 SQL，只存文本。
"""

import hashlib
import json
import os
import sqlite3
import threading

from .analyzer import analyze
from .errors import LineageError
from .rename import plan_rename, plan_to_dict


def _validate_view_sql(sql_text, schema_text):
    """视图保存前用原溯源引擎做完整解析+溯源，保证库里只存合法视图。"""
    if not sql_text or not sql_text.strip():
        raise LineageError("视图 SQL 不能为空")
    analyze(schema_text, sql_text)

SCHEMA_VERSION = 1

_LOCK = threading.Lock()

# 测试钩子：确认事务写完源表、写视图之前调用；抛异常即可模拟进程中断，
# 验证事务回滚不会留下半迁移。生产环境保持为 None。
CRASH_HOOK = None


# ---------------------------------------------------------------- 演示数据

DEMO_SCHEMA = """# CRM 客户库源表（字段名后 * 为敏感标记，顺序即 SELECT * 展开顺序）
customer(cust_id, name, mobile*, id_no*, city, gender)
orders(order_id, cust_id, amount, status, create_time)
event_log(log_id, cust_id, event, event_time)
"""

DEMO_VIEWS = [
    {
        "name": "seg_vip_customer",
        "description": "高价值付费客户分群：嵌套 CTE + JOIN + CASE + UNION ALL",
        "sql": """WITH paid_cust AS (
    SELECT c.cust_id,
           c.name,
           c.mobile,
           c.id_no,
           c.city
      FROM customer AS c
     WHERE c.mobile IS NOT NULL
       AND c.city IN ('BJ', 'SH', 'GZ')
),
cust_order AS (
    SELECT p.cust_id,
           p.name,
           p.mobile,
           p.id_no,
           o.order_id,
           CASE WHEN o.amount > 1000 THEN o.amount * 0.9
                ELSE o.amount
           END AS final_amount
      FROM paid_cust AS p
      INNER JOIN orders AS o
              ON p.cust_id = o.cust_id
     WHERE o.status = 'PAID'
)
SELECT cust_id,
       name,
       'VIP' AS seg_tag,
       final_amount + 0 AS amount
  FROM cust_order
 WHERE mobile IS NOT NULL
UNION ALL
SELECT cust_id,
       name,
       'NORMAL' AS seg_tag,
       0 AS amount
  FROM paid_cust
 WHERE id_no IS NULL;
""",
    },
    {
        "name": "seg_star_overview",
        "description": "星号直读源表：演示改名时星号就地展开、仅目标列改名",
        "sql": """SELECT *
  FROM customer
 WHERE name <> 'mobile';
""",
    },
    {
        "name": "seg_event_join",
        "description": "表别名 + JOIN ON + 谓词中的目标列引用",
        "sql": """SELECT c.cust_id,
       c.name,
       e.event,
       e.event_time
  FROM customer AS c
  INNER JOIN event_log AS e
          ON c.cust_id = e.cust_id
 WHERE c.mobile IS NOT NULL
   AND e.event = 'login';
""",
    },
    {
        "name": "seg_cte_shadow",
        "description": "CTE 列别名清单遮蔽：演示内层别名保持下游接口不变",
        "sql": """WITH base_c AS (
    SELECT cust_id, mobile, city FROM customer
),
masked_c (cust_id, mobile, city) AS (
    SELECT cust_id, '***', city FROM base_c
)
SELECT cust_id, mobile, city
  FROM masked_c;
""",
    },
]


# ---------------------------------------------------------------- 数据库连接

def default_db_path():
    return os.environ.get(
        "LINEAGE_DB_PATH",
        os.path.join(os.environ.get("LINEAGE_DATA_DIR", "/data"), "lineage.db"))


class Store:
    """线程安全的 SQLite 封装：每个操作使用独立连接（短事务）。"""

    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_schema (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    schema_text TEXT NOT NULL,
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS views (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    sql_text    TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS migrations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name  TEXT NOT NULL,
                    old_column  TEXT NOT NULL,
                    new_column  TEXT NOT NULL,
                    schema_text TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """)
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO app_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
            row = conn.execute("SELECT id FROM source_schema WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO source_schema(id, schema_text) VALUES (1, ?)",
                    (DEMO_SCHEMA,))
                for view in DEMO_VIEWS:
                    conn.execute(
                        "INSERT INTO views(name, description, sql_text) "
                        "VALUES (?, ?, ?)",
                        (view["name"], view["description"], view["sql"]))

    # ---------------------------------------------------------------- 读取

    def get_schema(self):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT schema_text, updated_at FROM source_schema WHERE id = 1"
            ).fetchone()
            return {"schema_text": row["schema_text"],
                    "updated_at": row["updated_at"]}

    def list_views(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, description, sql_text, created_at, updated_at "
                "FROM views ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_view(self, view_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, description, sql_text, created_at, updated_at "
                "FROM views WHERE id = ?", (view_id,)).fetchone()
            return dict(row) if row else None

    def list_migrations(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, table_name, old_column, new_column, created_at "
                "FROM migrations ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def get_migration(self, migration_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM migrations WHERE id = ?",
                (migration_id,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["detail"] = json.loads(data.pop("detail_json"))
            return data

    # ---------------------------------------------------------------- 写入

    def save_schema(self, schema_text):
        if not schema_text or not schema_text.strip():
            raise LineageError("源表结构不能为空")
        with _LOCK, self._connect() as conn:
            conn.execute(
                "UPDATE source_schema SET schema_text = ?, "
                "updated_at = datetime('now') WHERE id = 1",
                (schema_text,))

    def create_view(self, name, sql_text, description=""):
        name = (name or "").strip()
        if not name:
            raise LineageError("视图名称不能为空")
        schema_text = self.get_schema()["schema_text"]
        _validate_view_sql(sql_text, schema_text)
        with _LOCK, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM views WHERE name = ?", (name,)).fetchone()
            if exists:
                raise LineageError(f"视图名 {name} 已存在")
            cur = conn.execute(
                "INSERT INTO views(name, description, sql_text) VALUES (?, ?, ?)",
                (name, description.strip(), sql_text))
            return cur.lastrowid

    def update_view(self, view_id, name, sql_text, description=""):
        name = (name or "").strip()
        if not name:
            raise LineageError("视图名称不能为空")
        schema_text = self.get_schema()["schema_text"]
        _validate_view_sql(sql_text, schema_text)
        with _LOCK, self._connect() as conn:
            row = conn.execute("SELECT id FROM views WHERE id = ?",
                               (view_id,)).fetchone()
            if row is None:
                raise LineageError("待编辑的视图不存在")
            clash = conn.execute(
                "SELECT 1 FROM views WHERE name = ? AND id <> ?",
                (name, view_id)).fetchone()
            if clash:
                raise LineageError(f"视图名 {name} 已被其他视图占用")
            conn.execute(
                "UPDATE views SET name = ?, description = ?, sql_text = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (name, description.strip(), sql_text, view_id))

    def delete_view(self, view_id):
        with _LOCK, self._connect() as conn:
            conn.execute("DELETE FROM views WHERE id = ?", (view_id,))

    # ---------------------------------------------------------------- 改名

    def preview_rename(self, table_name, old_column, new_column):
        """纯计算：读取当前快照，运行整批改名引擎并返回预览（不落库）。"""
        schema = self.get_schema()["schema_text"]
        views = [{"name": r["name"], "sql": r["sql_text"]}
                 for r in self.list_views()]
        result = plan_rename(schema, views, table_name, old_column, new_column)
        token = self._preview_token(
            schema, views, table_name, old_column, new_column)
        return {
            "token": token,
            "table": table_name,
            "old_column": old_column,
            "new_column": new_column,
            "schema_text": result["schema_text"],
            "views": [plan_to_dict(p) for p in result["plans"]],
        }

    def confirm_rename(self, table_name, old_column, new_column, token):
        """确认改名：重算校验 + 单事务一次写入源表与全部视图。

        - token 不匹配（预览后任一输入被编辑）→ 拒绝，不写任何内容。
        - 重算失败 → 事务回滚，数据库保持改名前完整状态。
        - 进程中断 → SQLite journal 回滚未提交事务，无半迁移。
        """
        with _LOCK, self._connect() as conn:
            # 事务内先取当前快照并比对 token（token 覆盖源表与全部视图 SQL）
            schema_row = conn.execute(
                "SELECT schema_text FROM source_schema WHERE id = 1").fetchone()
            schema = schema_row["schema_text"]
            view_rows = conn.execute(
                "SELECT id, name, sql_text FROM views ORDER BY id").fetchall()
            views = [{"name": r["name"], "sql": r["sql_text"]}
                     for r in view_rows]
            expected = self._preview_token(
                schema, views, table_name, old_column, new_column)
            if not token or not _hash_equal(token, expected):
                raise LineageError(
                    "预览结果已失效：源表或任一视图在预览后被编辑过，"
                    "请重新预览后再确认（本次未写入任何内容）")

            # 事务内重新完整计算，保证确认内容与库中当前快照严格一致
            result = plan_rename(
                schema, views, table_name, old_column, new_column)

            detail = {
                "table": table_name,
                "old_column": old_column,
                "new_column": new_column,
                "schema_text": result["schema_text"],
                "views": [plan_to_dict(p) for p in result["plans"]],
            }
            conn.execute(
                "UPDATE source_schema SET schema_text = ?, "
                "updated_at = datetime('now') WHERE id = 1",
                (result["schema_text"],))
            if CRASH_HOOK is not None:
                CRASH_HOOK()
            for row, plan in zip(view_rows, result["plans"]):
                conn.execute(
                    "UPDATE views SET sql_text = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (plan.new_sql, row["id"]))
            conn.execute(
                "INSERT INTO migrations(table_name, old_column, new_column, "
                "schema_text, detail_json) VALUES (?, ?, ?, ?, ?)",
                (table_name, old_column, new_column,
                 result["schema_text"], json.dumps(detail, ensure_ascii=False)))
            # with 退出时统一 commit；中途异常自动 rollback
            return detail

    @staticmethod
    def _preview_token(schema_text, views, table_name, old_column, new_column):
        # 覆盖全部可编辑输入：源表文本、每个视图的名称与 SQL、改名三要素
        payload = json.dumps(
            [schema_text,
             [[v["name"], v["sql"]] for v in views],
             table_name, old_column, new_column],
            ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_equal(a, b):
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
