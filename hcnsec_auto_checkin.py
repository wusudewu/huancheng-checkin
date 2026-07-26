#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
幻城网安 API 自动签到工具（性能优化版）
=========================
优化项：
  - Logger 持有文件句柄，消除反复 open/close
  - load_config 带 mtime 缓存，避免重复 JSON 解析
  - 账号并行签到（ThreadPoolExecutor），可配置并发数
  - requests.Session 复用以减少 TCP 握手
  - 签到后优先从响应中取余额，跳过冗余 get_self_info
  - 限流检测精确化为 status_code == 429
  - show_all_status 并行查询

功能：
  - 多账号批量自动签到
  - 账号管理（添加 / 删除 / 列表）
  - 签到状态查询
  - 日志记录
  - 命令行批量模式（可配合 Windows 计划任务实现每日自动签到）

用法：
  交互模式:  python hcnsec_auto_checkin.py
  批量签到:  python hcnsec_auto_checkin.py --run
  查看状态:  python hcnsec_auto_checkin.py --status
  添加账号:  python hcnsec_auto_checkin.py --add 用户名 密码
  删除账号:  python hcnsec_auto_checkin.py --remove 用户名
  列出账号:  python hcnsec_auto_checkin.py --list
"""

import os
import sys
import json
import time
import atexit
import argparse
import getpass
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

# 修复 Windows 控制台编码
os.environ["PYTHONIOENCODING"] = "utf-8"

# ============================================================
# 配置常量
# ============================================================

BASE_URL = "https://api.hcnsec.cn"
LOGIN_API = f"{BASE_URL}/api/user/login"
CHECKIN_STATUS_API = f"{BASE_URL}/api/user/checkin"
DO_CHECKIN_API = f"{BASE_URL}/api/user/checkin"
LOGOUT_API = f"{BASE_URL}/api/user/auth/logout"
SELF_INFO_API = f"{BASE_URL}/api/user/self"

def get_base_dir():
    """获取应用基础目录（兼容 PyInstaller 打包）"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.resolve()


def get_user_data_dir():
    """获取用户数据目录（Windows: %LOCALAPPDATA%\幻城签到\ | Android: app私有目录），自动创建"""
    if hasattr(sys, 'getandroidapilevel'):
        from java import jclass
        context = jclass('com.chaquo.python.Python').getPlatform().getApplication()
        base = context.getFilesDir().getAbsolutePath()
    else:
        base = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
    data_dir = os.path.join(base, '幻城签到')
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# 配置文件路径（用户数据目录，避免打包后 Program Files 等受限位置写入失败）
CONFIG_FILE = get_user_data_dir() / "accounts.json"
LOG_FILE = get_user_data_dir() / "checkin_log.txt"

# 请求超时（秒）
REQUEST_TIMEOUT = 30
# 账号之间的间隔（秒），避免触发速率限制
ACCOUNT_DELAY = 3
# 登录失败重试次数
MAX_RETRIES = 2
# 并行签到最大并发数
MAX_CONCURRENT_CHECKIN = 3

# HTTP 请求头，模拟浏览器行为
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/sign-in",
}


def make_headers(user_id: str | None = None) -> dict:
    """构建请求头，必要时附加 New-Api-User（登录返回的用户 ID）"""
    headers = dict(HEADERS)
    if user_id:
        headers["New-Api-User"] = user_id
    return headers


# ============================================================
# 日志模块（优化：持有文件句柄，消除反复 open/close）
# ============================================================

# 修复 Windows 控制台编码问题（只执行一次）
try:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


class Logger:
    """同时输出到控制台和日志文件的简易日志器（持有文件句柄）"""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._file = None
        self._lock = Lock()
        self._open_file()
        atexit.register(self.close)

    def _open_file(self):
        try:
            self._file = open(self.log_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self._file = None

    def _write(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {msg}"
        try:
            print(line)
        except Exception:
            try:
                print(line.encode("utf-8", errors="replace").decode("utf-8"))
            except Exception:
                pass
        with self._lock:
            if self._file is not None:
                try:
                    self._file.write(line + "\n")
                    self._file.flush()
                except Exception:
                    pass

    def info(self, msg: str):
        self._write("INFO", msg)

    def success(self, msg: str):
        self._write("OK  ", msg)

    def warn(self, msg: str):
        self._write("WARN", msg)

    def error(self, msg: str):
        self._write("ERR ", msg)

    def close(self):
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None


logger = Logger(LOG_FILE)


# ============================================================
# 会话工厂（每线程独立创建 Session，避免并发时请求头混淆）
# ============================================================

def _make_session() -> requests.Session:
    """为调用者创建一个新的 Session（线程独立，避免并发时请求头混淆）"""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=2,
        pool_maxsize=2,
        max_retries=0,  # 重试由上层控制
    )
    session.mount("https://", adapter)
    return session


# ============================================================
# 配置文件管理（优化：mtime 缓存）
# ============================================================

_config_cache: dict | None = None
_config_mtime: float = 0.0


def load_config() -> dict:
    """读取 accounts.json，带 mtime 缓存"""
    global _config_cache, _config_mtime
    try:
        current_mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else 0
    except OSError:
        current_mtime = 0

    if _config_cache is not None and current_mtime == _config_mtime:
        return _config_cache

    if not CONFIG_FILE.exists():
        _config_cache = {"accounts": []}
        _config_mtime = current_mtime
        return _config_cache

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
        _config_mtime = current_mtime
        return _config_cache
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取配置文件失败: {e}")
        _config_cache = {"accounts": []}
        _config_mtime = current_mtime
        return _config_cache


def save_config(config: dict):
    """保存配置到 accounts.json，并更新缓存"""
    global _config_cache, _config_mtime
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        _config_cache = config
        _config_mtime = CONFIG_FILE.stat().st_mtime
    except IOError as e:
        logger.error(f"保存配置文件失败: {e}")


def get_accounts() -> list:
    """获取账号列表"""
    config = load_config()
    return config.get("accounts", [])


def add_account(username: str, password: str) -> bool:
    """添加账号（去重）"""
    config = load_config()
    accounts = config.get("accounts", [])
    for acc in accounts:
        if acc.get("username") == username:
            logger.warn(f"账号 '{username}' 已存在，已更新密码")
            acc["password"] = password
            save_config(config)
            return True
    accounts.append({"username": username, "password": password})
    config["accounts"] = accounts
    save_config(config)
    logger.success(f"已添加账号: {username}")
    return True


def remove_account(username: str) -> bool:
    """删除账号"""
    config = load_config()
    accounts = config.get("accounts", [])
    original_len = len(accounts)
    config["accounts"] = [a for a in accounts if a.get("username") != username]
    if len(config["accounts"]) < original_len:
        save_config(config)
        logger.success(f"已删除账号: {username}")
        return True
    logger.warn(f"未找到账号: {username}")
    return False


# ============================================================
# 签到核心逻辑
# ============================================================

class CheckinResult:
    """单个账号的签到结果"""

    def __init__(self, username: str):
        self.username = username
        self.success = False
        self.already_checked = False
        self.quota_awarded = 0
        self.message = ""
        self.balance = ""

    def __str__(self):
        if self.already_checked:
            return f"{self.username}: 今日已签到 | 余额: {self.balance}"
        if self.success:
            return f"{self.username}: 签到成功 +{self.quota_awarded} | 余额: {self.balance}"
        return f"{self.username}: 签到失败 - {self.message}"


def login(session: requests.Session, username: str, password: str) -> dict | None:
    """
    登录账号，返回用户信息字典；失败返回 None
    """
    payload = {"username": username, "password": password}
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = session.post(
                LOGIN_API,
                json=payload,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            # 429 限流：等待时间较短，避免长时间阻塞
            if resp.status_code == 429:
                wait = min(5 * attempt, 30)
                logger.warn(f"  请求过于频繁（第{attempt}次），等待 {wait} 秒后重试...")
                time.sleep(wait)
                continue
            # 明确区分"密码错误"和"需要重试"
            try:
                data = resp.json()
            except Exception:
                if attempt <= MAX_RETRIES:
                    logger.warn(f"  响应解析失败（第{attempt}次），{ACCOUNT_DELAY}秒后重试...")
                    time.sleep(ACCOUNT_DELAY)
                    continue
                logger.error(f"  登录响应解析失败")
                return None

            if data.get("success"):
                # 检查是否需要 2FA
                if data.get("data", {}).get("require_2fa"):
                    logger.warn(f"  账号 {username} 启用了双因素认证(2FA)，无法自动签到")
                    return None
                return data.get("data")
            else:
                msg = data.get("message", "未知错误")
                # 密码错误/账号不存在 → 不重试
                if any(kw in msg.lower() for kw in ["password", "密码", "错误", "invalid", "error"]):
                    logger.error(f"  登录失败（密码或账号错误）: {msg}")
                    return None
                if attempt <= MAX_RETRIES:
                    logger.warn(f"  登录失败（第{attempt}次）: {msg}，{ACCOUNT_DELAY}秒后重试...")
                    time.sleep(ACCOUNT_DELAY)
                else:
                    logger.error(f"  登录失败: {msg}")
                    return None
        except requests.RequestException as e:
            if attempt <= MAX_RETRIES:
                logger.warn(f"  网络错误（第{attempt}次）: {e}，{ACCOUNT_DELAY}秒后重试...")
                time.sleep(ACCOUNT_DELAY)
            else:
                logger.error(f"  网络错误，已达到最大重试次数: {e}")
                return None
        except Exception as e:
            logger.error(f"  登录异常: {e}")
            return None
    return None


def get_checkin_status(session: requests.Session, user_id: str | None = None) -> dict | None:
    """获取签到状态"""
    try:
        resp = session.get(
            CHECKIN_STATUS_API,
            headers=make_headers(user_id),
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if data.get("success"):
            return data.get("data")
    except Exception as e:
        logger.warn(f"  获取签到状态失败: {e}")
    return None


def do_checkin(session: requests.Session, user_id: str | None = None) -> dict | None:
    """执行签到"""
    try:
        resp = session.post(
            DO_CHECKIN_API,
            headers=make_headers(user_id),
            timeout=REQUEST_TIMEOUT,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"  签到请求异常: {e}")
        return None


def get_self_info(session: requests.Session, user_id: str | None = None) -> dict | None:
    """获取当前用户信息（含余额）"""
    try:
        resp = session.get(
            SELF_INFO_API,
            headers=make_headers(user_id),
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if data.get("success"):
            return data.get("data")
    except Exception:
        pass
    return None


def logout(session: requests.Session, user_id: str | None = None):
    """退出登录"""
    try:
        session.post(
            LOGOUT_API,
            headers=make_headers(user_id),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass


def format_quota(quota: int) -> str:
    """将内部额度转换为可读余额（1元 = 500000 额度）"""
    if quota is None:
        return "未知"
    yuan = quota / 500000
    return f"¥{yuan:.2f}"


def calc_continuous_days(records: list) -> int:
    """从签到记录列表中计算连续签到天数。

    规则：从今天往前数，每天有一条签到记录就 +1，遇到断档就停止。
    """
    if not records:
        return 0
    dates = {rec["checkin_date"] for rec in records if "checkin_date" in rec}
    continuous = 0
    today = datetime.now()
    for i in range(365):  # 最多回溯一年
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        if day in dates:
            continuous += 1
        else:
            break
    return continuous


def _extract_balance_from_checkin_resp(checkin_resp: dict) -> str | None:
    """尝试从签到响应中直接提取余额，避免额外 API 调用"""
    try:
        data = checkin_resp.get("data", {})
        quota = data.get("quota")
        if quota is not None:
            return format_quota(quota)
    except Exception:
        pass
    return None


def checkin_one_account(username: str, password: str) -> CheckinResult:
    """
    对单个账号执行完整的签到流程：
      登录 → 查状态 → 签到 → 查余额(按需) → 登出

    每个线程创建独立的 Session，避免并发时 New-Api-User 请求头混淆。
    """
    result = CheckinResult(username)
    session = _make_session()  # 线程独立 Session

    # 1. 登录
    logger.info(f"正在登录: {username}")
    user_info = login(session, username, password)
    if user_info is None:
        result.message = "登录失败"
        return result

    # 获取用户 ID（用于 New-Api-User 请求头）
    user_id = str(user_info.get("id", ""))
    logger.success(f"  登录成功: {username} (ID: {user_id})")

    # 2. 获取签到状态
    status = get_checkin_status(session, user_id)
    if status:
        stats = status.get("stats", {})
        today = datetime.now().strftime("%Y-%m-%d")
        # 检查今天是否已签到
        checkin_dates = stats.get("checkin_dates", [])
        if isinstance(checkin_dates, list) and today in checkin_dates:
            result.already_checked = True
            result.success = True
            # 获取余额
            self_info = get_self_info(session, user_id)
            if self_info:
                result.balance = format_quota(self_info.get("quota"))
            logger.info(f"  {username}: 今日已签到，无需重复签到")
            logout(session, user_id)
            return result

    # 3. 执行签到
    logger.info(f"  正在签到: {username}")
    checkin_resp = do_checkin(session, user_id)

    if checkin_resp is None:
        result.message = "签到请求失败"
        logout(session, user_id)
        return result

    if checkin_resp.get("success"):
        result.success = True
        awarded = checkin_resp.get("data", {}).get("quota_awarded", 0)
        result.quota_awarded = awarded
        # 优先从签到响应中提取余额，无则再调 API
        balance = _extract_balance_from_checkin_resp(checkin_resp)
        if balance is not None:
            result.balance = balance
        else:
            self_info = get_self_info(session, user_id)
            if self_info:
                result.balance = format_quota(self_info.get("quota"))
        logger.success(f"  {username}: 签到成功! 获得 {awarded} 额度")
    else:
        msg = checkin_resp.get("message", "未知错误")
        # 如果提示今天已签到，也算成功
        if "已签到" in msg or "already" in msg.lower():
            result.already_checked = True
            result.success = True
            balance = _extract_balance_from_checkin_resp(checkin_resp)
            if balance is not None:
                result.balance = balance
            else:
                self_info = get_self_info(session, user_id)
                if self_info:
                    result.balance = format_quota(self_info.get("quota"))
            logger.info(f"  {username}: 今日已签到")
        else:
            result.message = msg
            logger.error(f"  {username}: 签到失败 - {msg}")

    # 4. 登出
    logout(session, user_id)
    logger.info(f"  已登出: {username}")

    return result


def checkin_all_accounts() -> list[CheckinResult]:
    """对所有账号执行并行签到，返回结果列表"""
    accounts = get_accounts()
    if not accounts:
        logger.warn("没有配置任何账号，请先添加账号")
        return []

    logger.info(f"========== 开始批量签到 ==========")
    logger.info(f"共 {len(accounts)} 个账号")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"最大并发: {MAX_CONCURRENT_CHECKIN}")
    logger.info("")

    # 过滤无效账号
    valid_accounts = []
    for acc in accounts:
        username = acc.get("username", "")
        password = acc.get("password", "")
        if not username or not password:
            logger.warn(f"跳过无效账号")
            continue
        valid_accounts.append((username, password))

    if not valid_accounts:
        logger.warn("没有有效的账号")
        return []

    results = []
    # 并行签到
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHECKIN) as executor:
        future_map = {
            executor.submit(checkin_one_account, username, password): (username, password)
            for username, password in valid_accounts
        }
        for future in as_completed(future_map):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                username = future_map[future][0]
                logger.error(f"  {username}: 签到异常 - {e}")
                results.append(CheckinResult(username))

    # 按原始账号顺序排序结果
    username_order = {u: i for i, (u, _) in enumerate(valid_accounts)}
    results.sort(key=lambda r: username_order.get(r.username, 999))

    # 汇总
    logger.info("========== 签到汇总 ==========")
    success_count = sum(1 for r in results if r.success)
    already_count = sum(1 for r in results if r.already_checked)
    new_checkin_count = sum(1 for r in results if r.success and not r.already_checked)
    fail_count = len(results) - success_count

    for r in results:
        logger.info(str(r))

    logger.info("")
    logger.info(f"新增签到: {new_checkin_count} | 已签到: {already_count} | 失败: {fail_count} | 总计: {len(results)}")
    logger.info(f"日志已保存到: {LOG_FILE}")

    return results


# ============================================================
# 状态查询（优化：并行查询）
# ============================================================

def _query_one_account_status(username: str, password: str) -> dict:
    """查询单个账号状态（供并行调用）"""
    info = {"username": username, "online": False, "balance": "—", "total_days": 0,
            "continuous_days": 0, "checked_today": False, "today_award": "—", "error": None}
    session = _make_session()  # 线程独立 Session
    user_info = login(session, username, password)
    if user_info is None:
        info["error"] = "登录失败"
        return info

    info["online"] = True
    user_id = str(user_info.get("id", ""))
    self_info = get_self_info(session, user_id)
    status = get_checkin_status(session, user_id)

    if self_info:
        info["balance"] = format_quota(self_info.get("quota"))
        info["used"] = format_quota(self_info.get("used_quota"))
    if status:
        stats = status.get("stats", {})
        info["total_days"] = stats.get("total_checkins", 0)
        info["continuous_days"] = calc_continuous_days(stats.get("records", []))
        # 判断今日是否已签到：优先用 checked_in_today 布尔值，回退到 checkin_dates 列表
        if stats.get("checked_in_today"):
            info["checked_today"] = True
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            dates = stats.get("checkin_dates", [])
            if isinstance(dates, list) and today in dates:
                info["checked_today"] = True
        # 计算今日签到奖励
        records = status.get("records", [])
        if not records:
            records = stats.get("records", [])
        if isinstance(records, list):
            today = datetime.now().strftime("%Y-%m-%d")
            for rec in records:
                if rec.get("checkin_date") == today:
                    awarded = rec.get("quota_awarded", 0)
                    yuan = awarded / 500000
                    info["today_award"] = f"+¥{yuan:.2f}"
                    break
    logout(session, user_id)
    return info


def show_all_status():
    """查询所有账号的签到状态和余额（并行）"""
    accounts = get_accounts()
    if not accounts:
        logger.warn("没有配置任何账号")
        return

    logger.info("========== 账号状态查询 ==========")

    valid = [(a.get("username", ""), a.get("password", "")) for a in accounts
             if a.get("username") and a.get("password")]

    if not valid:
        logger.warn("没有有效的账号")
        return

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHECKIN) as executor:
        future_map = {
            executor.submit(_query_one_account_status, u, p): u
            for u, p in valid
        }
        for future in as_completed(future_map):
            info = future.result()
            username = info["username"]
            if info.get("error"):
                logger.error(f"  {username}: {info['error']}")
            else:
                logger.info(f"  {username}: 余额: {info['balance']} | 已用: {info['used']}")
                logger.info(f"    累计签到: {info['total_days']} 天 | 连续签到: {info['continuous_days']} 天")

    logger.info("")


# ============================================================
# 交互式菜单
# ============================================================

def interactive_menu():
    """交互式命令行菜单"""
    while True:
        print()
        print("=" * 50)
        print("       幻城网安 API 自动签到工具")
        print("=" * 50)
        print("  1. 一键签到所有账号")
        print("  2. 查看所有账号状态")
        print("  3. 添加账号")
        print("  4. 删除账号")
        print("  5. 列出所有账号")
        print("  0. 退出")
        print("=" * 50)

        choice = input("请选择操作 [0-5]: ").strip()

        if choice == "1":
            checkin_all_accounts()
        elif choice == "2":
            show_all_status()
        elif choice == "3":
            username = input("请输入用户名: ").strip()
            if not username:
                print("用户名不能为空")
                continue
            password = getpass.getpass("请输入密码 (输入时不显示): ").strip()
            if not password:
                print("密码不能为空")
                continue
            add_account(username, password)
        elif choice == "4":
            accounts = get_accounts()
            if not accounts:
                print("没有账号可删除")
                continue
            print("当前账号列表:")
            for i, acc in enumerate(accounts, 1):
                print(f"  {i}. {acc['username']}")
            username = input("请输入要删除的用户名: ").strip()
            if username:
                remove_account(username)
        elif choice == "5":
            accounts = get_accounts()
            if not accounts:
                print("还没有配置任何账号")
            else:
                print(f"\n共 {len(accounts)} 个账号:")
                for i, acc in enumerate(accounts, 1):
                    print(f"  {i}. {acc['username']}")
        elif choice == "0":
            print("再见！")
            break
        else:
            print("无效选择，请重新输入")

        if choice in ("1", "2"):
            input("\n按回车键继续...")


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="幻城网安 API 自动签到工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python hcnsec_auto_checkin.py              # 交互模式
  python hcnsec_auto_checkin.py --run        # 批量签到
  python hcnsec_auto_checkin.py --status     # 查看状态
  python hcnsec_auto_checkin.py --add user1 pass1   # 添加账号
  python hcnsec_auto_checkin.py --remove user1      # 删除账号
  python hcnsec_auto_checkin.py --list       # 列出账号
""",
    )
    parser.add_argument("--run", action="store_true", help="批量签到所有账号")
    parser.add_argument("--status", action="store_true", help="查看所有账号签到状态")
    parser.add_argument("--add", nargs=2, metavar=("USER", "PASS"), help="添加账号")
    parser.add_argument("--remove", nargs=1, metavar="USER", help="删除账号")
    parser.add_argument("--list", action="store_true", help="列出所有账号")

    args = parser.parse_args()

    if args.run:
        checkin_all_accounts()
    elif args.status:
        show_all_status()
    elif args.add:
        add_account(args.add[0], args.add[1])
    elif args.remove:
        remove_account(args.remove[0])
    elif args.list:
        accounts = get_accounts()
        if not accounts:
            print("还没有配置任何账号")
        else:
            print(f"共 {len(accounts)} 个账号:")
            for i, acc in enumerate(accounts, 1):
                print(f"  {i}. {acc['username']}")
    else:
        interactive_menu()


if __name__ == "__main__":
    main()
