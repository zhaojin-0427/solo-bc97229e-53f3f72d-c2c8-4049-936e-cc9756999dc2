"""Flask HTTP 接口端到端测试（测试客户端，无需监听端口）。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))


class ApiIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LINEAGE_DB_PATH"] = os.path.join(self.tmp.name, "api.db")
        # 延迟导入，确保环境变量生效
        import importlib
        import server
        importlib.reload(server)
        self.server = server
        self.client = server.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("LINEAGE_DB_PATH", None)

    def get_json(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.get_json()

    def post_json(self, path, body, expected=200):
        resp = self.client.post(path, json=body)
        self.assertEqual(resp.status_code, expected, resp.data)
        return resp.get_json()

    def test_pages_render_and_original_lineage_still_works(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/migration").status_code, 200)
        data = self.post_json("/api/analyze", {
            "schema": "t(a, b*)",
            "sql": "SELECT a, b FROM t",
        })
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["result"]["outputs"]), 2)

    def test_full_rename_flow_over_http(self):
        state = self.get_json("/api/state")
        self.assertTrue(state["ok"])
        self.assertGreaterEqual(len(state["views"]), 3)

        # 预览
        preview_resp = self.post_json("/api/rename/preview", {
            "table": "customer", "old_column": "mobile",
            "new_column": "phone_no",
        })
        self.assertTrue(preview_resp["ok"])
        preview = preview_resp["preview"]
        self.assertIn("phone_no*", preview["schema_text"])
        token = preview["token"]

        # 预览后编辑视图 → 确认必须被拒绝
        views = self.get_json("/api/state")["views"]
        target = next(v for v in views if v["name"] == "seg_event_join")
        edited = dict(target)
        edited["sql_text"] = target["sql_text"] + "\n-- changed after preview\n"
        resp = self.client.put(f"/api/views/{target['id']}", json=edited)
        self.assertEqual(resp.status_code, 200)

        stale = self.post_json("/api/rename/confirm", {
            "table": "customer", "old_column": "mobile",
            "new_column": "phone_no", "token": token,
        }, expected=400)
        self.assertFalse(stale["ok"])

        # 重新预览并确认
        preview2 = self.post_json("/api/rename/preview", {
            "table": "customer", "old_column": "mobile",
            "new_column": "phone_no",
        })["preview"]
        confirm = self.post_json("/api/rename/confirm", {
            "table": "customer", "old_column": "mobile",
            "new_column": "phone_no", "token": preview2["token"],
        })
        self.assertTrue(confirm["ok"])

        # 库中状态已整体更新
        state2 = self.get_json("/api/state")
        self.assertIn("phone_no*", state2["schema"]["schema_text"])
        self.assertEqual(len(state2["migrations"]), 1)
        star_view = next(v for v in state2["views"]
                         if v["name"] == "seg_star_overview")
        self.assertIn("phone_no", star_view["sql_text"])
        self.assertIn("'mobile'", star_view["sql_text"])

        # 迁移详情接口可查完整结果
        mid = state2["migrations"][0]["id"]
        detail = self.get_json(f"/api/migrations/{mid}")
        self.assertTrue(detail["ok"])
        self.assertEqual(len(detail["migration"]["detail"]["views"]),
                         len(state2["views"]))

    def test_conflict_returns_400_and_persists_nothing(self):
        before = self.get_json("/api/state")
        data = self.post_json("/api/rename/preview", {
            "table": "customer", "old_column": "mobile",
            "new_column": "city",
        }, expected=400)
        self.assertFalse(data["ok"])
        after = self.get_json("/api/state")
        self.assertEqual(before["schema"], after["schema"])
        self.assertEqual(after["migrations"], [])

    def test_view_create_validation_and_crud(self):
        bad = self.post_json("/api/views", {
            "name": "bad_view", "sql_text": "SELECT nope FROM customer",
        }, expected=400)
        self.assertFalse(bad["ok"])

        ok = self.post_json("/api/views", {
            "name": "ok_view", "description": "d",
            "sql_text": "SELECT cust_id FROM customer",
        })
        vid = ok["view"]["id"]

        dup = self.post_json("/api/views", {
            "name": "ok_view", "sql_text": "SELECT 1",
        }, expected=400)
        self.assertFalse(dup["ok"])

        resp = self.client.put(f"/api/views/{vid}", json={
            "name": "ok_view2", "description": "d2",
            "sql_text": "SELECT name FROM customer",
        })
        self.assertEqual(resp.status_code, 200)

        resp = self.client.delete(f"/api/views/{vid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(f"/api/views/{vid}").status_code, 404)

    def test_schema_save_rejects_invalid_text(self):
        resp = self.client.put("/api/schema", json={"schema_text": "garbage"})
        self.assertEqual(resp.status_code, 400)

    def test_persistence_across_store_reopen(self):
        preview = self.post_json("/api/rename/preview", {
            "table": "customer", "old_column": "mobile",
            "new_column": "phone_no",
        })["preview"]
        self.post_json("/api/rename/confirm", {
            "table": "customer", "old_column": "mobile",
            "new_column": "phone_no", "token": preview["token"],
        })
        # 重新加载 server 模块模拟进程重启，数据库文件不变
        import importlib
        import server
        importlib.reload(server)
        client2 = server.app.test_client()
        state = client2.get("/api/state").get_json()
        self.assertIn("phone_no*", state["schema"]["schema_text"])
        self.assertEqual(len(state["migrations"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
