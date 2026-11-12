"""改名迁移引擎测试：仅依赖 Python 标准库。

运行：python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from lineage.analyzer import analyze  # noqa: E402
from lineage.errors import LineageError  # noqa: E402
from lineage.rename import plan_rename, rename_schema_text  # noqa: E402

SCHEMA = """
customer(cust_id, name, mobile*, id_no*, city)
orders(order_id, cust_id, amount, status, create_time)
log(id, cust_id, mobile*, event)
"""

T, O, N = "customer", "mobile", "phone_no"


def rename_one(sql, schema=SCHEMA, table=T, old=O, new=N):
    res = plan_rename(schema, [{"name": "v", "sql": sql}], table, old, new)
    return res["plans"][0].new_sql, res["schema_text"], res["plans"][0]


def assert_renamed(test, new_sql, expected):
    test.assertEqual(expected, new_sql)


class SchemaRenameTests(unittest.TestCase):

    def test_schema_rename_keeps_sensitive_star_and_order(self):
        text = rename_schema_text(SCHEMA, "customer", "mobile", "phone_no")
        self.assertIn("customer(cust_id, name, phone_no*, id_no*, city)", text)
        # 其他表的同名列不动
        self.assertIn("log(id, cust_id, mobile*, event)", text)

    def test_schema_rename_preserves_comments_and_spacing(self):
        text = "# 注释保留\ncustomer(cust_id,  mobile* , name)\n"
        out = rename_schema_text(text, "customer", "mobile", "phone_no")
        self.assertEqual(out, "# 注释保留\ncustomer(cust_id,  phone_no* , name)\n")

    def test_conflict_existing_column_rejected(self):
        with self.assertRaises(LineageError):
            plan_rename(SCHEMA, [{"name": "v", "sql": "SELECT 1"}],
                        "customer", "mobile", "city")

    def test_unknown_table_or_column_rejected(self):
        with self.assertRaises(LineageError):
            plan_rename(SCHEMA, [{"name": "v", "sql": "SELECT 1"}],
                        "nope", "mobile", "phone_no")
        with self.assertRaises(LineageError):
            plan_rename(SCHEMA, [{"name": "v", "sql": "SELECT 1"}],
                        "customer", "nope", "phone_no")

    def test_same_name_rejected_and_bad_identifier_rejected(self):
        with self.assertRaises(LineageError):
            plan_rename(SCHEMA, [{"name": "v", "sql": "SELECT 1"}],
                        "customer", "mobile", "mobile")
        with self.assertRaises(LineageError):
            plan_rename(SCHEMA, [{"name": "v", "sql": "SELECT 1"}],
                        "customer", "mobile", "1bad")


class OnlyBoundTargetRewrittenTests(unittest.TestCase):

    def test_string_literal_same_name_untouched(self):
        sql = "SELECT cust_id FROM customer WHERE name = 'mobile'"
        new_sql, _, plan = rename_one(sql)
        self.assertIn("'mobile'", new_sql)
        self.assertNotIn("phone_no", new_sql)
        self.assertFalse(plan.affected)

    def test_other_table_same_name_column_untouched(self):
        sql = ("SELECT c.cust_id, l.mobile AS log_mobile "
               "FROM customer c INNER JOIN log l ON c.cust_id = l.cust_id")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("l.mobile", new_sql)
        self.assertNotIn("phone_no", new_sql)

    def test_qualified_and_alias_rewritten(self):
        sql = "SELECT c.mobile FROM customer AS c WHERE c.mobile IS NOT NULL"
        new_sql, _, _ = rename_one(sql)
        self.assertEqual(
            "SELECT c.phone_no FROM customer AS c "
            "WHERE c.phone_no IS NOT NULL", new_sql)

    def test_bare_column_rewritten(self):
        sql = "SELECT mobile, cust_id FROM customer WHERE mobile IS NULL"
        new_sql, _, _ = rename_one(sql)
        self.assertEqual(
            "SELECT phone_no, cust_id FROM customer WHERE phone_no IS NULL",
            new_sql)

    def test_case_when_and_arithmetic_refs_rewritten(self):
        sql = ("SELECT CASE WHEN mobile IS NOT NULL THEN 1 ELSE 0 END AS has_m, "
               "mobile FROM customer")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("WHEN phone_no IS NOT NULL", new_sql)
        self.assertIn("phone_no FROM customer", new_sql)
        self.assertNotIn(" mobile", new_sql.replace("phone_no", ""))

    def test_join_on_rewritten_when_bound_to_target(self):
        sql = ("SELECT c.cust_id FROM customer c "
               "INNER JOIN orders o ON c.cust_id = o.cust_id "
               "WHERE c.mobile = o.status")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("c.phone_no = o.status", new_sql)


class CteShadowingTests(unittest.TestCase):

    def test_nested_cte_shadow_blocks_rewrite(self):
        # 外层 CTE 直通目标列（随源列改名）；
        # 内层 CTE 用列别名清单把输出重新命名为 mobile，遮蔽外层改名，
        # 内层查询体之后及最终查询里的 mobile 指向内层输出，不再改名
        sql = """
        WITH outer_c AS (
            SELECT cust_id, mobile FROM customer
        ),
        inner_c (cust_id, mobile) AS (
            SELECT cust_id, phone FROM (SELECT 1)
        )
        SELECT mobile FROM inner_c
        UNION ALL
        SELECT mobile FROM outer_c
        """
        # 上面的 FROM (SELECT 1) 不被语法允许，改用常量表达式构造内层独立列
        sql = """
        WITH outer_c AS (
            SELECT cust_id, mobile FROM customer
        ),
        inner_c (cust_id, mobile) AS (
            SELECT cust_id, 'X' FROM customer
        )
        SELECT mobile FROM inner_c
        UNION ALL
        SELECT mobile FROM outer_c
        """
        new_sql, _, _ = rename_one(sql)
        # 第一层：源列改名，外层 CTE 输出列跟随
        self.assertIn("SELECT cust_id, phone_no FROM customer", new_sql)
        # 遮蔽层：别名清单与体内常量列保持 mobile，最终查询对 inner_c 的引用不动
        self.assertIn("inner_c (cust_id, mobile)", new_sql)
        self.assertIn("SELECT mobile FROM inner_c", new_sql)
        # outer_c 输出已改名，UNION 第二分支对它的引用同步改名
        self.assertIn("SELECT phone_no FROM outer_c", new_sql)

    def test_cte_passthrough_column_renamed_and_downstream_refs_follow(self):
        sql = ("WITH c AS (SELECT mobile, cust_id FROM customer) "
               "SELECT c.mobile FROM c WHERE c.mobile IS NOT NULL")
        new_sql, _, _ = rename_one(sql)
        self.assertEqual(
            "WITH c AS (SELECT phone_no, cust_id FROM customer) "
            "SELECT c.phone_no FROM c WHERE c.phone_no IS NOT NULL", new_sql)

    def test_explicit_alias_keeps_downstream_interface(self):
        sql = ("WITH c AS (SELECT mobile AS m, cust_id FROM customer) "
               "SELECT c.m FROM c WHERE c.m IS NOT NULL")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("phone_no AS m", new_sql)
        self.assertIn("SELECT c.m FROM c WHERE c.m IS NOT NULL", new_sql)
        self.assertNotIn("phone_no AS phone_no", new_sql)

    def test_cte_column_alias_list_keeps_name(self):
        sql = ("WITH c (m, id) AS (SELECT mobile, cust_id FROM customer) "
               "SELECT m FROM c")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("WITH c (m, id) AS (SELECT phone_no, cust_id", new_sql)
        self.assertIn("SELECT m FROM c", new_sql)


class StarTests(unittest.TestCase):

    def test_bare_star_expanded_with_renamed_column_in_position(self):
        sql = "SELECT * FROM customer"
        new_sql, _, plan = rename_one(sql)
        self.assertEqual(
            "SELECT cust_id, name, phone_no, id_no, city FROM customer",
            new_sql)
        self.assertEqual(len(plan.expanded_stars), 1)
        # 展开后溯源仍与原星号一致
        before = analyze(SCHEMA, sql)
        after = analyze(
            "customer(cust_id, name, phone_no*, id_no*, city)\n"
            "orders(order_id, cust_id, amount, status, create_time)\n"
            "log(id, cust_id, mobile*, event)\n", new_sql)
        self.assertEqual([o["name"] for o in before["outputs"]],
                         [o["name"].replace("phone_no", "mobile")
                          for o in after["outputs"]])

    def test_table_star_expanded_qualified(self):
        sql = ("SELECT c.* FROM customer AS c "
               "INNER JOIN orders o ON c.cust_id = o.cust_id")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("c.cust_id, c.name, c.phone_no, c.id_no, c.city", new_sql)

    def test_star_unrelated_to_target_left_untouched(self):
        sql = "SELECT * FROM orders"
        new_sql, _, plan = rename_one(sql)
        self.assertEqual("SELECT * FROM orders", new_sql)
        self.assertEqual(plan.expanded_stars, [])
        self.assertFalse(plan.affected)

    def test_star_over_cte_without_rename_left_untouched(self):
        # CTE 显式别名遮蔽目标列，CTE 输出名不变，星号无需展开
        sql = ("WITH c (id, m) AS (SELECT cust_id, mobile FROM customer) "
               "SELECT * FROM c")
        new_sql, _, plan = rename_one(sql)
        self.assertIn("SELECT * FROM c", new_sql)
        self.assertEqual(plan.expanded_stars, [])

    def test_bare_star_multi_source_qualified_after_expansion(self):
        sql = ("SELECT * FROM customer c INNER JOIN orders o "
               "ON c.cust_id = o.cust_id")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("c.cust_id, c.name, c.phone_no", new_sql)
        self.assertIn("o.order_id, o.cust_id, o.amount", new_sql)


class UnionAllTests(unittest.TestCase):

    def test_union_all_both_branches_renamed(self):
        sql = ("SELECT mobile FROM customer WHERE city = 'BJ' "
               "UNION ALL SELECT mobile FROM customer WHERE city = 'SH'")
        new_sql, _, _ = rename_one(sql)
        self.assertEqual(2, new_sql.count("phone_no"))
        self.assertNotIn(" mobile", new_sql.replace("phone_no", ""))

    def test_union_all_mixed_passthrough_and_aliased(self):
        # 分支1直通目标列（改名），分支2显式别名 mobile（接口名保持）
        sql = ("SELECT mobile FROM customer "
               "UNION ALL SELECT mobile AS mobile FROM customer")
        new_sql, _, _ = rename_one(sql)
        self.assertIn("SELECT phone_no FROM customer", new_sql)
        # 分支2别名保持，但其内部列引用仍需改名
        self.assertIn("SELECT phone_no AS mobile FROM customer", new_sql)

    def test_union_all_first_branch_alias_preserves_downstream_interface(self):
        # 第一分支显式别名保持旧名：UNION 输出名（SQLite 取首分支）不变，
        # 下游对该 UNION 的 mobile 引用不得改名，星号也无需展开
        sql = """
        WITH u AS (
            SELECT mobile AS mobile, cust_id FROM customer
            UNION ALL
            SELECT mobile, cust_id FROM customer
        )
        SELECT mobile, cust_id FROM u
        """
        new_sql, _, plan = rename_one(sql)
        self.assertIn("SELECT phone_no AS mobile, cust_id FROM customer", new_sql)
        self.assertIn("SELECT phone_no, cust_id FROM customer", new_sql)
        # 下游接口列名保持 mobile
        self.assertIn("SELECT mobile, cust_id FROM u", new_sql)
        self.assertEqual(plan.expanded_stars, [])

    def test_union_all_star_preserved_when_output_name_kept(self):
        sql = """
        WITH u AS (
            SELECT mobile AS mobile, cust_id FROM customer
            UNION ALL
            SELECT mobile, cust_id FROM customer
        )
        SELECT * FROM u
        """
        new_sql, _, plan = rename_one(sql)
        self.assertIn("SELECT * FROM u", new_sql)
        self.assertEqual(plan.expanded_stars, [])


class EquivalenceGuardTests(unittest.TestCase):

    def test_sensitive_flag_follows_column(self):
        new_schema = rename_schema_text(SCHEMA, "customer", "mobile", "phone_no")
        result = analyze(new_schema, "SELECT phone_no FROM customer")
        col = result["outputs"][0]
        self.assertTrue(col["sensitive"])
        self.assertIn("value", col["sensitive_kind"])

    def test_output_names_order_and_sources_unchanged(self):
        sql = ("SELECT cust_id, name, mobile AS m, city, 1 AS one "
               "FROM customer WHERE id_no IS NOT NULL")
        new_sql, new_schema, _ = rename_one(sql)
        before = analyze(SCHEMA, sql)
        after = analyze(new_schema, new_sql)
        self.assertEqual([o["name"] for o in before["outputs"]],
                         [o["name"] for o in after["outputs"]])
        self.assertEqual(len(before["outputs"]), len(after["outputs"]))
        for b, a in zip(before["outputs"], after["outputs"]):
            self.assertEqual(b["sensitive"], a["sensitive"])
            self.assertEqual(
                {(d["table"], d["column"]) for d in b["condition_sources"]},
                {(d["table"], d["column"].replace("phone_no", "mobile"))
                 for d in a["condition_sources"]})

    def test_invalid_view_fails_whole_batch(self):
        views = [
            {"name": "ok", "sql": "SELECT mobile FROM customer"},
            {"name": "bad", "sql": "SELECT nope FROM customer"},
        ]
        with self.assertRaises(LineageError):
            plan_rename(SCHEMA, views, "customer", "mobile", "phone_no")

    def test_idempotent_second_rename_preview(self):
        # 改名后的结构上再做一次别的列改名，视图仍可正常分析
        res1 = plan_rename(SCHEMA, [{"name": "v",
                                    "sql": "SELECT mobile, city FROM customer"}],
                           "customer", "mobile", "phone_no")
        schema2 = res1["schema_text"]
        sql2 = res1["plans"][0].new_sql
        res2 = plan_rename(schema2, [{"name": "v", "sql": sql2}],
                           "customer", "city", "city_code")
        self.assertIn("phone_no", res2["plans"][0].new_sql)
        self.assertIn("city_code", res2["plans"][0].new_sql)


class BatchAndEdgeTests(unittest.TestCase):

    def test_batch_all_or_nothing_plans(self):
        views = [
            {"name": "v1", "sql": "SELECT mobile FROM customer"},
            {"name": "v2", "sql": "SELECT * FROM orders"},
            {"name": "v3",
             "sql": "SELECT l.mobile FROM log l INNER JOIN customer c "
                    "ON l.cust_id = c.cust_id"},
        ]
        res = plan_rename(SCHEMA, views, "customer", "mobile", "phone_no")
        self.assertEqual(3, len(res["plans"]))
        self.assertTrue(res["plans"][0].affected)
        self.assertFalse(res["plans"][1].affected)
        # v3 只引用 log.mobile，不受影响
        self.assertFalse(res["plans"][2].affected)
        self.assertIn("l.mobile", res["plans"][2].new_sql)

    def test_ambiguous_bare_column_still_rejected(self):
        # customer 与 log 都有 cust_id 时裸列歧义（分析器原本就拒绝）
        sql = ("SELECT cust_id FROM customer INNER JOIN log "
               "ON customer.cust_id = log.cust_id")
        with self.assertRaises(LineageError):
            rename_one(sql)

    def test_quoted_identifier_target(self):
        sql = 'SELECT "mobile" FROM customer'
        new_sql, _, _ = rename_one(sql)
        self.assertIn('"phone_no"', new_sql)

    def test_qualified_quoted_identifier_target(self):
        # 限定名 + 双引号 / 反引号 / 方括号三种引用风格
        for sql, quote in [
            ('SELECT c."mobile" FROM customer AS c', '"'),
            ("SELECT c.`mobile` FROM customer AS c", "`"),
            ("SELECT c.[mobile] FROM customer AS c", "["),
        ]:
            new_sql, _, _ = rename_one(sql)
            expected = (f"c.{quote}phone_no{quote}" if quote != "["
                        else "c.[phone_no]")
            self.assertIn(expected, new_sql, msg=sql)
            self.assertNotIn("mobile", new_sql.replace("customer", ""))

    def test_comment_preserved(self):
        sql = "SELECT mobile -- 手机号\n  FROM customer /* block */"
        new_sql, _, _ = rename_one(sql)
        self.assertIn("-- 手机号", new_sql)
        self.assertIn("/* block */", new_sql)
        self.assertIn("phone_no", new_sql)

    def test_dollar_identifier_new_name(self):
        new_sql, schema, _ = rename_one(
            "SELECT mobile FROM customer", new="mobile$new")
        self.assertIn("mobile$new", new_sql)
        self.assertIn("mobile$new*", schema)

    def test_similar_identifier_names_not_confused(self):
        # mobile2 / mobile_backup 是不同标识符，改名 mobile 时不得误伤
        schema = "customer(cust_id, mobile, mobile2, mobile_backup)\n"
        sql = "SELECT mobile2, mobile_backup, mobile FROM customer"
        new_sql, _, plan = rename_one(sql, schema=schema)
        self.assertIn("mobile2", new_sql)
        self.assertIn("mobile_backup", new_sql)
        self.assertIn("phone_no", new_sql)
        self.assertEqual(1, len(plan.changed_refs))

    def test_new_name_substring_of_existing_allowed(self):
        # 新名仅是现有列名的前缀而非完全相同，不构成冲突
        schema = "customer(cust_id, mobile, mobile_phone)\n"
        new_sql, _, _ = rename_one(
            "SELECT mobile FROM customer", schema=schema, new="mob")
        self.assertIn("mob FROM customer", new_sql)
        self.assertNotIn("mobile FROM customer", new_sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
