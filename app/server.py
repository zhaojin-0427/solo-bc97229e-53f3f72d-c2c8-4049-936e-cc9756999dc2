"""Flask Web 入口：溯源页面、改名迁移页面与 JSON 接口。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, abort, jsonify, render_template, request

from lineage import analyze
from lineage.demo_data import DEMO_SCHEMA, DEMO_SQL
from lineage.errors import LineageError
from lineage.storage import Store, _validate_view_sql

app = Flask(__name__)
store = Store()


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    return render_template("index.html",
                           demo_schema=DEMO_SCHEMA,
                           demo_sql=DEMO_SQL)


@app.route("/migration")
def migration_page():
    return render_template("migration.html")


# ---------------------------------------------------------------- 溯源（原功能）

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(silent=True) or {}
    schema_text = payload.get("schema", "")
    sql_text = payload.get("sql", "")
    try:
        result = analyze(schema_text, sql_text)
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return jsonify({"ok": False,
                        "error": f"分析失败：{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, "result": result})


# ---------------------------------------------------------------- 源表 / 视图管理

@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify({
        "ok": True,
        "schema": store.get_schema(),
        "views": store.list_views(),
        "migrations": store.list_migrations(),
    })


@app.route("/api/schema", methods=["PUT"])
def api_save_schema():
    payload = request.get_json(silent=True) or {}
    try:
        # 保存前先用解析器校验，拒绝非法源表文本
        from lineage.schema import parse_schemas
        parse_schemas(payload.get("schema_text", ""))
        store.save_schema(payload.get("schema_text", ""))
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "schema": store.get_schema()})


@app.route("/api/views", methods=["POST"])
def api_create_view():
    payload = request.get_json(silent=True) or {}
    try:
        schema_text = store.get_schema()["schema_text"]
        _validate_view_sql(payload.get("sql_text", ""), schema_text)
        view_id = store.create_view(
            payload.get("name", ""), payload.get("sql_text", ""),
            payload.get("description", ""))
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "view": store.get_view(view_id)})


@app.route("/api/views/<int:view_id>", methods=["PUT", "DELETE", "GET"])
def api_view_detail(view_id):
    if request.method == "GET":
        view = store.get_view(view_id)
        if view is None:
            abort(404)
        return jsonify({"ok": True, "view": view})

    if request.method == "DELETE":
        store.delete_view(view_id)
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}
    try:
        schema_text = store.get_schema()["schema_text"]
        _validate_view_sql(payload.get("sql_text", ""), schema_text)
        store.update_view(view_id, payload.get("name", ""),
                          payload.get("sql_text", ""),
                          payload.get("description", ""))
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "view": store.get_view(view_id)})


# ---------------------------------------------------------------- 改名迁移

@app.route("/api/rename/preview", methods=["POST"])
def api_rename_preview():
    payload = request.get_json(silent=True) or {}
    try:
        preview = store.preview_rename(
            payload.get("table", ""),
            payload.get("old_column", ""),
            payload.get("new_column", ""))
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "preview": preview})


@app.route("/api/rename/confirm", methods=["POST"])
def api_rename_confirm():
    payload = request.get_json(silent=True) or {}
    try:
        detail = store.confirm_rename(
            payload.get("table", ""),
            payload.get("old_column", ""),
            payload.get("new_column", ""),
            payload.get("token", ""))
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True,
                    "detail": detail,
                    "schema": store.get_schema(),
                    "views": store.list_views(),
                    "migrations": store.list_migrations()})


@app.route("/api/migrations/<int:migration_id>", methods=["GET"])
def api_migration_detail(migration_id):
    data = store.get_migration(migration_id)
    if data is None:
        abort(404)
    return jsonify({"ok": True, "migration": data})


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"ok": False, "error": "资源不存在"}), 404


@app.errorhandler(405)
def method_not_allowed(_err):
    return jsonify({"ok": False, "error": "不支持的请求方法"}), 405


@app.errorhandler(Exception)
def handle_unexpected(exc):
    # 已由路由处理的 LineageError/HTTP 异常会先走对应处理器；
    # 这里兜底保证任何意外错误都返回 JSON 而非 HTML 错误页
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return jsonify({"ok": False, "error": exc.description}), exc.code
    return jsonify({"ok": False,
                    "error": f"服务器内部错误：{type(exc).__name__}: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
