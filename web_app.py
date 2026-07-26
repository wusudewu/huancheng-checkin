#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
幻城网安签到 · Web 控制台（性能优化版）
==========================
优化项：
  - waitress 生产级 WSGI 服务器替代 Flask 开发服务器
  - /api/status 异步缓存 + 后台定期刷新，避免阻塞
  - 签到任务内使用并行签到（复用 hcnsec_auto_checkin 的线程池）
  - 路由修正：<path:username> → <string:username>

启动：
    python web_app.py
然后浏览器访问 http://127.0.0.1:5000
"""

import os
import sys
_is_android = hasattr(sys, 'getandroidapilevel')
import shutil
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


def get_base_dir():
    """获取应用基础目录（兼容 PyInstaller 打包）"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.resolve()


def get_user_data_dir():
    """获取用户数据目录（%LOCALAPPDATA%\幻城签到\），自动创建"""
    local_appdata = os.environ.get('LOCALAPPDATA', '')
    if not local_appdata:
        local_appdata = os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    user_dir = Path(local_appdata) / '幻城签到'
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


import hcnsec_auto_checkin as core

# 将 core 模块的配置文件路径重定向到用户数据目录
core.CONFIG_FILE = get_user_data_dir() / "accounts.json"
core.LOG_FILE = get_user_data_dir() / "checkin_log.txt"

# 初始化 accounts.json：用户数据目录不存在时，从应用目录复制初始模板
if not core.CONFIG_FILE.exists():
    template = get_base_dir() / "accounts.json"
    if template.exists():
        shutil.copy2(template, core.CONFIG_FILE)

# Flask static_folder 适配打包路径（PyInstaller / Android）
if _is_android:
    static_dir = Path(__file__).parent / 'static'
elif getattr(sys, 'frozen', False):
    static_dir = Path(sys._MEIPASS) / 'static'
else:
    static_dir = Path(__file__).parent / 'static'
app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")


# ============================================================
# 后台签到任务状态（单例，本地工具足够）
# ============================================================

_task_lock = threading.Lock()
_task_state = {
    "running": False,
    "progress": {"done": 0, "total": 0, "current": ""},
    "results": [],
    "started_at": None,
    "finished_at": None,
    "log_lines": [],
}

# 捕获 core.logger 输出，供前端实时查看
_log_buffer = []
try:
    _orig_write = core.logger._write
except AttributeError:
    _orig_write = None


def _capture_write(level, msg):
    if _orig_write:
        _orig_write(level, msg)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log_buffer.append(f"[{ts}] [{level}] {msg}")
    with _task_lock:
        _task_state["log_lines"] = list(_log_buffer[-300:])


core.logger._write = _capture_write


# ============================================================
# /api/status 异步缓存（优化：避免串行阻塞）
# ============================================================

_status_cache = {
    "data": [],
    "updated_at": None,
    "refreshing": False,
}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 60  # 缓存有效期（秒）


def _refresh_status_background():
    """后台线程：并行查询所有账号状态并更新缓存"""
    with _status_cache_lock:
        if _status_cache["refreshing"]:
            return
        _status_cache["refreshing"] = True

    try:
        accounts = core.get_accounts()
        if not accounts:
            with _status_cache_lock:
                _status_cache["data"] = []
                _status_cache["updated_at"] = datetime.now().isoformat()
                _status_cache["refreshing"] = False
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed

        data = []
        valid = [(a.get("username", ""), a.get("password", ""))
                 for a in accounts if a.get("username") and a.get("password")]

        with ThreadPoolExecutor(max_workers=core.MAX_CONCURRENT_CHECKIN) as executor:
            future_map = {
                executor.submit(core._query_one_account_status, u, p): u
                for u, p in valid
            }
            for future in as_completed(future_map):
                info = future.result()
                entry = {
                    "username": info["username"],
                    "online": info.get("online", False),
                    "balance": info.get("balance", "—"),
                    "used": info.get("used", "—"),
                    "total_days": info.get("total_days", 0),
                    "continuous_days": info.get("continuous_days", 0),
                    "checked_today": info.get("checked_today", False),
                    "today_award": info.get("today_award", "—"),
                }
                data.append(entry)

        # 按原始账号顺序排序
        username_order = {u: i for i, (u, _) in enumerate(valid)}
        data.sort(key=lambda x: username_order.get(x["username"], 999))

        with _status_cache_lock:
            _status_cache["data"] = data
            _status_cache["updated_at"] = datetime.now().isoformat()
    except Exception:
        pass
    finally:
        with _status_cache_lock:
            _status_cache["refreshing"] = False


def _calc_today_award(status: dict) -> str:
    """从签到记录的 records 中查找今天的 quota_awarded"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        records = status.get("stats", {}).get("records", [])
        if isinstance(records, list):
            for rec in records:
                if rec.get("checkin_date") == today:
                    awarded = rec.get("quota_awarded", 0)
                    yuan = awarded / 500000
                    return f"+¥{yuan:.2f}"
    except Exception:
        pass
    return "—"


def _result_to_dict(r: core.CheckinResult) -> dict:
    return {
        "username": r.username,
        "success": r.success,
        "already_checked": r.already_checked,
        "quota_awarded": r.quota_awarded,
        "balance": r.balance,
        "message": r.message,
    }


def _run_checkin_task():
    """后台签到任务（复用 core.checkin_all_accounts 的并行逻辑）"""
    accounts = core.get_accounts()
    total = len(accounts)
    with _task_lock:
        _task_state["running"] = True
        _task_state["progress"] = {"done": 0, "total": total, "current": ""}
        _task_state["results"] = []
        _task_state["started_at"] = datetime.now().isoformat()
        _task_state["finished_at"] = None
        _task_state["log_lines"] = list(_log_buffer[-300:])

    # 直接调用 core 的并行签到
    raw_results = core.checkin_all_accounts()
    results = [_result_to_dict(r) for r in raw_results]

    with _task_lock:
        _task_state["running"] = False
        _task_state["finished_at"] = datetime.now().isoformat()
        _task_state["results"] = results
        _task_state["progress"] = {"done": total, "total": total, "current": ""}

    # 签到完成后主动刷新状态缓存，避免前端拿到旧数据
    threading.Thread(target=_refresh_status_background, daemon=True).start()


# ============================================================
# 页面路由
# ============================================================

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ============================================================
# 账号管理 API
# ============================================================

@app.route("/api/accounts")
def api_accounts():
    accounts = core.get_accounts()
    return jsonify([
        {"username": a.get("username", ""), "index": i}
        for i, a in enumerate(accounts)
    ])


@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "message": "用户名和密码不能为空"}), 400
    core.add_account(username, password)
    # 账号变动后触发后台刷新缓存
    threading.Thread(target=_refresh_status_background, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/accounts/<string:username>", methods=["DELETE"])
def api_remove_account(username):
    core.remove_account(username)
    threading.Thread(target=_refresh_status_background, daemon=True).start()
    return jsonify({"success": True})


# ============================================================
# 签到 API
# ============================================================

@app.route("/api/checkin", methods=["POST"])
def api_checkin():
    with _task_lock:
        if _task_state["running"]:
            return jsonify({"success": False, "message": "已有签到任务正在进行中"}), 409
    if not core.get_accounts():
        return jsonify({"success": False, "message": "还没有配置任何账号"}), 400
    threading.Thread(target=_run_checkin_task, daemon=True).start()
    return jsonify({"success": True, "message": "签到任务已启动"})


@app.route("/api/checkin/status")
def api_checkin_status():
    with _task_lock:
        return jsonify({
            "running": _task_state["running"],
            "progress": _task_state["progress"],
            "results": _task_state["results"],
            "started_at": _task_state["started_at"],
            "finished_at": _task_state["finished_at"],
            "log_lines": _task_state["log_lines"],
        })


# ============================================================
# 实时状态查询 API（优化：带缓存）
# ============================================================

@app.route("/api/status")
def api_status():
    """返回账号状态，优先使用缓存；若缓存过期则触发后台刷新"""
    with _status_cache_lock:
        data = _status_cache["data"]
        updated_at = _status_cache["updated_at"]
        refreshing = _status_cache["refreshing"]

    # 缓存为空或过期（>60s），触发后台刷新
    should_refresh = False
    if updated_at:
        try:
            cache_time = datetime.fromisoformat(updated_at)
            if (datetime.now() - cache_time).total_seconds() > STATUS_CACHE_TTL:
                should_refresh = True
        except Exception:
            should_refresh = True
    else:
        should_refresh = True

    if should_refresh and not refreshing:
        threading.Thread(target=_refresh_status_background, daemon=True).start()

    return jsonify(data)


# ============================================================
# 日志 API
# ============================================================

@app.route("/api/logs")
def api_logs():
    try:
        with open(core.LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-300:]
        return jsonify({"lines": [l.rstrip("\n") for l in lines]})
    except FileNotFoundError:
        return jsonify({"lines": []})


@app.route("/api/logs", methods=["POST"])
def api_clear_logs():
    """清空日志文件，使清屏操作持久化"""
    try:
        with open(core.LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
    with _task_lock:
        _log_buffer.clear()
        _task_state["log_lines"] = []
    return jsonify({"success": True})


@app.route("/api/summary")
def api_summary():
    """快速汇总：账号数 + 最近一次签到结果统计"""
    accounts = core.get_accounts()
    with _task_lock:
        results = list(_task_state["results"])
        running = _task_state["running"]
    success = sum(1 for r in results if r.get("success"))
    already = sum(1 for r in results if r.get("already_checked"))
    return jsonify({
        "account_count": len(accounts),
        "last_success": success,
        "last_already": already,
        "last_total": len(results),
        "running": running,
    })


def start_server():
    """供 Chaquopy/Android 调用的启动入口"""
    import threading as _threading
    import time as _time
    import webbrowser as _wb
    _threading.Thread(target=_refresh_status_background, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    if not _is_android:
        url = f"http://127.0.0.1:{port}"
        _threading.Thread(target=lambda: (_time.sleep(1.5), _wb.open(url)), daemon=True).start()
        print("=" * 52)
        print("  幻城网安签到 · Web 控制台已启动")
        print("  访问地址: http://127.0.0.1:5000")
        print("=" * 52)
    host = "127.0.0.1"
    try:
        from waitress import serve
        serve(app, host=host, port=port)
    except ImportError:
        app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    start_server()
