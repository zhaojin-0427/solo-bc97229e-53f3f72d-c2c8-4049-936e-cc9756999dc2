"""SQLite 存储层与改名迁移 API 语义测试（标准库 unittest）。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from lineage.errors import LineageError  # noqa: E402
from lineage.storage import Store  # noqa: E402


class StoreTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "lineage.db")
        self.store = Store(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()


class SeedAndPersistenceTests(StoreTestBase):

    def test_seed_data_loaded_once(self):
        state = self.store.get_schema()
        self.assertIn("customer(", state["schema_text"])
        views = self.store.list_views()
        self.assertGreaterEqual(len(views), 3)
        self.assertTrue(all(v["sql_text"].strip() for v in views))

    def test_all_seed_views_analyze_cleanly_against_seed_schema(self):
        # 种子视图必须全部能通过原溯源引擎（只引用源表或自身 CTE）
        from lineage import analyze
        schema_text = self.store.get_schema()["schema_text"]
        for view in self.store.list_views():
            model = analyze(schema_text, view["sql_text"])
            self.assertTrue(model["outputs"], view["name"])

    def test_reopen_keeps_all_data(self):
        before = self.store.list_views()
        # 模拟进程重启：用同一路径新建 Store
        reopened = Store(self.db_path)
        after = reopened.list_views()
        self.assertEqual([v["name"] for v in before],
                         [v["name"] for v in after])
        self.assertEqual(reopened.get_schema()["schema_text"],
                         self.store.get_schema()["schema_text"])

    def test_view_crud_and_validation(self):
        with self.assertRaises(LineageError):
            self.store.create_view("bad", "SELECT nope FROM customer")
        vid = self.store.create_view(
            "tmp_view", "SELECT cust_id FROM customer", "临时视图")
        self.store.update_view(
            vid, "tmp_view2", "SELECT name FROM customer", "改名")
        self.assertEqual(self.store.get_view(vid)["name"], "tmp_view2")
        self.store.delete_view(vid)
        self.assertIsNone(self.store.get_view(vid))


class RenameTransactionTests(StoreTestBase):

    def _preview_confirm(self, table="customer", old="mobile", new="phone_no"):
        preview = self.store.preview_rename(table, old, new)
        detail = self.store.confirm_rename(table, old, new, preview["token"])
        return preview, detail

    def test_preview_does_not_persist(self):
        schema_before = self.store.get_schema()["schema_text"]
        views_before = self.store.list_views()
        self.store.preview_rename("customer", "mobile", "phone_no")
        self.assertEqual(schema_before, self.store.get_schema()["schema_text"])
        self.assertEqual(
            [v["sql_text"] for v in views_before],
            [v["sql_text"] for v in self.store.list_views()])
        self.assertEqual(self.store.list_migrations(), [])

    def test_confirm_atomically_updates_schema_and_all_views(self):
        _, detail = self._preview_confirm()
        schema = self.store.get_schema()["schema_text"]
        self.assertIn("phone_no*", schema)
        self.assertNotIn(" mobile*", schema)
        for view in self.store.list_views():
            # 含目标列绑定的视图应已改写；字符串字面量保持
            if view["name"] == "seg_star_overview":
                self.assertIn("phone_no", view["sql_text"])
                self.assertIn("'mobile'", view["sql_text"])
        migrations = self.store.list_migrations()
        self.assertEqual(len(migrations), 1)
        self.assertEqual(migrations[0]["new_column"], "phone_no")
        # 迁移记录保留完整结果，重启后仍可查
        reopened = Store(self.db_path)
        record = reopened.get_migration(migrations[0]["id"])
        self.assertEqual(len(record["detail"]["views"]),
                         len(self.store.list_views()))

    def test_stale_token_after_schema_edit_rejected_without_writes(self):
        preview = self.store.preview_rename(
            "customer", "mobile", "phone_no")
        # 预览后编辑源表
        self.store.save_schema(
            self.store.get_schema()["schema_text"] + "\nnote(x, y)\n")
        with self.assertRaises(LineageError):
            self.store.confirm_rename(
                "customer", "mobile", "phone_no", preview["token"])
        # 原数据完好
        self.assertIn("mobile*", self.store.get_schema()["schema_text"])
        self.assertEqual(self.store.list_migrations(), [])

    def test_stale_token_after_view_edit_rejected(self):
        preview = self.store.preview_rename(
            "customer", "mobile", "phone_no")
        views = self.store.list_views()
        target = next(v for v in views if v["name"] == "seg_event_join")
        self.store.update_view(
            target["id"], target["name"],
            target["sql_text"] + "\n-- 预览后被编辑\n", target["description"])
        with self.assertRaises(LineageError):
            self.store.confirm_rename(
                "customer", "mobile", "phone_no", preview["token"])
        self.assertEqual(self.store.list_migrations(), [])

    def test_conflict_rejects_whole_batch(self):
        with self.assertRaises(LineageError):
            self.store.preview_rename("customer", "mobile", "city")
        # 无任何落库变化
        self.assertIn("mobile*", self.store.get_schema()["schema_text"])

    def test_crash_between_schema_and_view_writes_rolls_back(self):
        import lineage.storage as st
        preview = self.store.preview_rename(
            "customer", "mobile", "phone_no")

        def boom():
            raise RuntimeError("simulated crash mid-transaction")

        st.CRASH_HOOK = boom
        try:
            with self.assertRaises(RuntimeError):
                self.store.confirm_rename(
                    "customer", "mobile", "phone_no", preview["token"])
        finally:
            st.CRASH_HOOK = None

        # 事务整体回滚：源表、视图、迁移历史都保持改名前状态
        self.assertIn("mobile*", self.store.get_schema()["schema_text"])
        for view in self.store.list_views():
            self.assertNotIn("phone_no", view["sql_text"])
        self.assertEqual(self.store.list_migrations(), [])

        # 数据库未损坏，可重新打开并正常完成迁移
        Store(self.db_path)
        preview2 = self.store.preview_rename(
            "customer", "mobile", "phone_no")
        self.store.confirm_rename(
            "customer", "mobile", "phone_no", preview2["token"])
        self.assertIn("phone_no*", self.store.get_schema()["schema_text"])
        self.assertEqual(len(self.store.list_migrations()), 1)

    def test_confirm_recomputes_and_migration_history_appends(self):
        self._preview_confirm()
        # 第二次改名在新结构上进行，历史追加而非覆盖
        preview2 = self.store.preview_rename("customer", "city", "city_code")
        self.store.confirm_rename(
            "customer", "city", "city_code", preview2["token"])
        names = [m["new_column"] for m in self.store.list_migrations()]
        self.assertIn("phone_no", names)
        self.assertIn("city_code", names)
        schema = self.store.get_schema()["schema_text"]
        self.assertIn("phone_no*", schema)
        self.assertIn("city_code", schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)
