#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
start_points_claimer —— 腾讯 START 云游戏（start.qq.com）积分任务自动领取助手

原理：用 Playwright 驱动一个带本地存档的真实浏览器，按真实用户路径
（头像 → 我的资产 → 积分）进入积分任务面板，发现可领取的按钮就点击。
全程不伪造任何网络请求，行为与人工操作一致，不产生任何截图。

用法：
    python main.py login        # 首次登录（弹窗扫码，登录状态保存到 profile/）
    python main.py run          # 日常运行：进入任务面板，自动领取可领奖励
    python main.py run --check  # 只检测有几项可领取，不点击
    python main.py renew        # 登录态保活：轻量访问一次，刷新凭据并报告剩余有效期
    python main.py probe        # 调试：导出任务面板页面文字（页面改版排查用）

日志固定输出四类关键信息：
    [登录] 成功/失败        —— 登录状态是否有效
    [检测] 发现 N 项可领取  —— 面板里有几个"领取"按钮（含任务描述）
    [结果] 本次领取 X 项，剩余可领取 Y 项
    [积分] 总积分 N，最近过期 M 积分将于 YYYY-MM-DD 过期

更多说明见 README.md。
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VERSION = "0.2.0"

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 内置参数（零配置：平台改版时直接改这里的常量）
# ---------------------------------------------------------------------------

HOME_URL = "https://start.qq.com/webgame/home?ADTAG=10000000"

# 浏览器
HEADLESS = True            # 无头后台运行；run --visible 可临时弹窗
VIEWPORT = (1440, 900)

# 任务面板入口路径：头像下拉 → 我的资产 → 积分
ENTRY_ASSETS = "我的资产"
ENTRY_POINTS = "积分"
ENTRY_FALLBACK = "查看详情"
PANEL_MARKERS = ("points_mall", "#/newWeal")  # 任务面板 iframe 的 URL 特征
POINTS_HOST = "my.start.qq.com"              # 积分商城所在域名（余额/明细都在这）
POINTS_DETAIL_ENTRY = "积分明细"              # 商城页头按钮，点开「收支明细」弹窗
BALANCE_SEL = ".integral-num"                # 商城页头的余额数字
DETAIL_BOX_SEL = "[class*='log-header-box']"  # 收支明细弹窗的页头（含余额与过期提示）
# 「120积分将于2026年12月31日过期」；「可用平台积分：552」
EXPIRE_RE = re.compile(r"(\d+)\s*积分将于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日过期")
BALANCE_RE = re.compile(r"可用平台积分\s*[:：]?\s*(\d+)")

# 可领取按钮的文案（全字匹配，逐个尝试）
CLAIM_KEYWORDS = ("一键领取", "领取", "领取奖励")
# 这些字样出现时不点击（防御性过滤）
SKIP_WORDS = ("已领取", "去完成", "前往", "去登录", "已领完", "明日再来")
# 补充匹配：按 CSS 类名找按钮（文案可能是图片字时兜底）
CSS_SELECTORS = ("[class*='receive']", "[class*='claim']", "[class*='lingqu']")
MAX_ROUNDS = 4             # 最多扫几轮（领取后面板可能刷新出新按钮）

# 运行时
# 浏览器存档（登录状态），删掉需重新扫码。
# 建议放在不会被其他程序并发改写的本地目录；可用环境变量 START_PROFILE_DIR 指定位置，
# 如 D:\start_data\profile
PROFILE_DIR = os.environ.get("START_PROFILE_DIR") or "profile"
LOGS_DIR = "logs"          # probe 调试输出目录（dom_dump.txt）
STATE_FILE = "state.json"  # 当日去重记录
LOG_FILE = ""              # 追加写入的日志文件，留空只打印
LOGIN_WAIT_SECONDS = 300   # login 命令等待扫码的最长秒数
BALANCE_WAIT_SECONDS = 10  # 轮询等待积分余额渲染的最长时间
DETAIL_WAIT_SECONDS = 5    # 轮询等待明细弹窗渲染的最长时间

# 登录态保活：把"还能用多久"的凭据单独备份一份。
# 值 = 触发提醒的剩余天数（低于这个天数就在日志里提示重新 login）。
SESSION_BACKUP = "session_backup.json"
SESSION_WARN_DAYS = 7

# 长期凭据：备份时整份存档，判定"还能用多久"则只看这几项。
# 它们带 30 天~1 年有效期，且每次访问首页都会被腾讯滚动续期（start_uid 每次顺延 30 天），
# 所以只要间隔不到 30 天跑一次 renew，登录态就不会自然过期。
SESSION_COOKIE_NAMES = (
    "RK",              # .qq.com      登录票据根（1 年）
    "qlogin_uid",      # .qq.com      登录过的 QQ 号（1 年）
    "pt2gguin",        # .ptlogin2    登录过的 QQ 号（1 年）
    "ptcz",            # .qq.com      客户端指纹（1 年）
    "pt_guid_sig",     # .ptlogin2    "记住我"签名（30 天滚动）
    "pt_recent_uins",  # .ptlogin2    最近登录账号列表（30 天滚动）
    "start_uid",       # .start.qq.com START 会话（30 天滚动）
)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

log = logging.getLogger("start_points_claimer")


def setup_logging() -> None:
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log.setLevel(logging.INFO)

    if LOG_FILE:
        path = _resolve(LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def _resolve(name: str) -> Path:
    """相对路径一律相对脚本所在目录解析，保证从任意工作目录运行都一致。"""
    p = Path(name)
    return p if p.is_absolute() else BASE_DIR / p


# ---------------------------------------------------------------------------
# 浏览器基础操作
# ---------------------------------------------------------------------------

def open_context(pw, headless: bool = HEADLESS):
    """打开带本地存档的浏览器。"""
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(_resolve(PROFILE_DIR)),
        headless=headless,
        viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
        locale="zh-CN",
        # 磁盘缓存限到 20MB：本脚本只用一个页面，缓存大无收益，还能控制 profile 目录体积
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disk-cache-size=20971520",
            "--media-cache-size=10485760",
        ],
        ignore_default_args=["--enable-automation"],
    )


def iter_frames(context):
    """遍历所有标签页的所有 frame（任务面板可能藏在 iframe 或新标签页里）。"""
    for pg in context.pages:
        for frame in pg.frames:
            yield pg, frame


def find_login_button(context) -> bool:
    """页面上是否出现未登录标志（右上角 user-info 显示'登录'）。"""
    for _, frame in iter_frames(context):
        try:
            info = frame.locator(".user-info").first
            if info.is_visible() and info.inner_text().strip() == "登录":
                return True
        except Exception:
            continue
        try:
            btn = frame.get_by_text("立即登录", exact=True).first
            if btn.is_visible():
                return True
        except Exception:
            continue
    return False


def _click_first(context, labels, extra_sel=None) -> bool:
    """在所有页面/框架里找给定文案的可见元素并点击第一个。先精确匹配，再模糊匹配。"""
    for pg, frame in list(iter_frames(context)):
        for label in labels:
            for exact in (True, False):
                try:
                    el = frame.get_by_text(label, exact=exact).first
                    if el.is_visible():
                        el.click(timeout=3000)
                        log.info("已点击：%s", label)
                        return True
                except Exception:
                    continue
        if extra_sel:
            try:
                el = frame.locator(extra_sel).first
                if el.is_visible():
                    el.click(timeout=3000)
                    log.info("已点击元素：%s", extra_sel)
                    return True
            except Exception:
                continue
    return False


def _wait_new_tab(context, baseline: int, timeout: int = 8) -> None:
    """等新标签页出现或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(context.pages) > baseline:
            return
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# 核心流程：导航 + 检测 + 领取
# ---------------------------------------------------------------------------

def try_open_task_panel(context) -> bool:
    """按真实用户路径导航：头像 → 我的资产(面板) → 积分 → 任务列表。"""
    n_tabs = len(context.pages)

    # 1) 点头像，展开下拉菜单
    if not _click_first(context, (), extra_sel=".user-info"):
        log.info("未点到头像，尝试直接点顶栏'%s'", ENTRY_ASSETS)
        _click_first(context, (ENTRY_ASSETS,))
    context.pages[0].wait_for_timeout(2000)

    # 2) 下拉菜单里点"我的资产"，弹出资产面板（含时长/积分）
    _click_first(context, (ENTRY_ASSETS,))
    context.pages[0].wait_for_timeout(3000)

    # 3) 面板里点"积分"，内嵌页跳到积分商城（含任务列表）
    if not _click_first(context, (ENTRY_POINTS,)):
        log.info("未点到'%s'，尝试'%s'", ENTRY_POINTS, ENTRY_FALLBACK)
        _click_first(context, (ENTRY_FALLBACK,))
    _wait_new_tab(context, n_tabs)
    context.pages[-1].wait_for_timeout(6000)

    # 4) 确认任务面板 iframe 已出现
    deadline = time.time() + 15
    while time.time() < deadline:
        for pg in context.pages:
            for frame in pg.frames:
                if any(marker in frame.url for marker in PANEL_MARKERS):
                    log.info("任务面板已加载：%s", frame.url[:80])
                    pg.wait_for_timeout(2000)
                    return True
        time.sleep(1)
    log.info("未确认到任务面板，仍将在现有页面上尝试扫描领取按钮。")
    return False


def _task_desc(btn, label: str) -> str:
    """取按钮所在任务条目的描述（父容器文字），失败则退回按钮文案。"""
    try:
        text = btn.locator("xpath=..").inner_text()
        text = " ".join(text.split())
        if text and len(text) > len(label):
            return text[:50]
    except Exception:
        pass
    return label


def detect_claimable(context, verbose: bool = False) -> list:
    """统计当前页面上可领取的任务（只看不点），返回去重后的描述列表。"""
    claim_re = re.compile(r"^(%s)$" % "|".join(map(re.escape, CLAIM_KEYWORDS)))
    names = []
    for pg, frame in list(iter_frames(context)):
        try:
            for btn in frame.get_by_text(claim_re).all():
                try:
                    label = (btn.inner_text() or "").strip()
                    if not label or any(w in label for w in SKIP_WORDS):
                        continue
                    if not btn.is_visible():
                        continue
                    names.append(_task_desc(btn, label))
                except Exception:
                    continue
        except Exception:
            continue
    names = list(dict.fromkeys(names))  # 去重并保持顺序
    if verbose:
        if names:
            log.info("[检测] 发现 %d 项可领取：%s", len(names), "；".join(names))
        else:
            log.info("[检测] 未发现可领取任务")
    return names


# ---------------------------------------------------------------------------
# 积分余额与过期时间
# ---------------------------------------------------------------------------

def _points_frame(context):
    """找到承载积分商城（含余额/明细）的 frame；找不到返回 None。"""
    for _, frame in iter_frames(context):
        try:
            if POINTS_HOST in frame.url or any(m in frame.url for m in PANEL_MARKERS):
                return frame
        except Exception:
            continue
    return None


def _parse_expire(text: str):
    """从「120积分将于2026年12月31日过期」里解析出 (积分数, 'YYYY-MM-DD')。"""
    m = EXPIRE_RE.search(text or "")
    if not m:
        return None
    amount = int(m.group(1))
    y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return amount, f"{y:04d}-{mo:02d}-{d:02d}"


def read_points_info(context, open_detail: bool = True) -> dict:
    """读取当前账号的积分余额与最近的过期信息。

    余额直接取商城页头的数字；过期信息藏在「积分明细（收支明细）」弹窗里，
    需要先点一下入口按钮。返回形如：
        {"balance": 552, "expire_amount": 120, "expire_date": "2026-12-31"}
    取不到的字段为 None，绝不抛异常影响主流程。

    积分商城是 SPA 首屏渲染时快时慢，余额和弹窗都可能晚一拍才出现，
    所以每一步都是轮询等待，不一次性判断。
    """
    info = {"balance": None, "expire_amount": None, "expire_date": None}

    frame = _points_frame(context)
    if frame is None:
        return info

    # 1) 余额：轮询商城页头的 .integral-num（首屏渲染慢时最多等 10 秒）
    deadline = time.time() + BALANCE_WAIT_SECONDS
    while time.time() < deadline:
        try:
            el = frame.locator(BALANCE_SEL).first
            if el.is_visible():
                text = (el.inner_text() or "").strip()
                if text:
                    info["balance"] = int(re.sub(r"\D", "", text) or 0)
                    break
        except Exception:
            pass
        time.sleep(1)

    # 2) 过期信息：点开「积分明细」弹窗，弹窗页头里既有余额也有过期提示
    if open_detail:
        try:
            btn = frame.get_by_text(POINTS_DETAIL_ENTRY, exact=True).first
            if btn.is_visible():
                btn.click(timeout=5000)
                frame.page.wait_for_timeout(1000)
        except Exception:
            pass

        # 弹窗渲染也许有滞后，轮询页头框，余额/过期轮都读出来再收工
        deadline = time.time() + DETAIL_WAIT_SECONDS
        while time.time() < deadline:
            try:
                box = frame.locator(DETAIL_BOX_SEL).first
                if box.is_visible():
                    text = box.inner_text() or ""
                    if info["balance"] is None:
                        m = BALANCE_RE.search(text)
                        if m:
                            info["balance"] = int(m.group(1))
                    parsed = _parse_expire(text)
                    if parsed:
                        info["expire_amount"], info["expire_date"] = parsed
                    if info["balance"] is not None and info["expire_date"]:
                        break
            except Exception:
                pass
            time.sleep(1)

    # 兜底：整页文本里再搜余额与过期提示（含弹窗打开的可见文字）
    if info["balance"] is None or info["expire_date"] is None:
        try:
            text = frame.evaluate(JS_TEXT)
            if info["balance"] is None:
                m = BALANCE_RE.search(text or "")
                if m:
                    info["balance"] = int(m.group(1))
            if info["expire_date"] is None:
                parsed = _parse_expire(text or "")
                if parsed:
                    info["expire_amount"], info["expire_date"] = parsed
        except Exception:
            pass

    return info


def log_points_info(info: dict) -> None:
    """把积分余额与最近过期时间打到日志（取不到也要给一行提示）。"""
    if not info or info.get("balance") is None:
        log.info("[积分] 未能读取积分余额（页面结构可能已变，可跑 probe 排查）。")
        return
    if info.get("expire_date"):
        log.info(
            "[积分] 总积分 %d，最近过期：%d 积分将于 %s 过期。",
            info["balance"], info.get("expire_amount") or 0, info["expire_date"],
        )
    else:
        log.info("[积分] 总积分 %d，未读到积分过期信息。", info["balance"])


def close_popups(context) -> None:
    """领取后常弹提示窗，尝试按 ESC / 点关闭图标把它关掉。"""
    for pg, frame in list(iter_frames(context)):
        try:
            pg.keyboard.press("Escape")
        except Exception:
            pass
        for selector in ("[class*='close']", ".close", "text=我知道了"):
            try:
                el = frame.locator(selector).first
                if el.is_visible():
                    el.click(timeout=2000)
                    pg.wait_for_timeout(500)
                    break
            except Exception:
                continue


def claim_round(context) -> int:
    """扫一轮所有可领取按钮并点击，返回本轮成功点击的次数。"""
    claim_re = re.compile(r"^(%s)$" % "|".join(map(re.escape, CLAIM_KEYWORDS)))
    claimed = 0

    # 第一轮：按按钮文案全字匹配
    for pg, frame in list(iter_frames(context)):
        try:
            buttons = frame.get_by_text(claim_re)
            for btn in buttons.all():
                try:
                    label = (btn.inner_text() or "").strip()
                    if not label or any(w in label for w in SKIP_WORDS):
                        continue
                    if not btn.is_visible():
                        continue
                    btn.click(timeout=3000)
                    claimed += 1
                    log.info("已点击「%s」", label)
                    pg.wait_for_timeout(1500)
                    close_popups(context)
                except PWTimeout:
                    continue
                except Exception:
                    continue
        except Exception:
            continue

    # 第二轮：按 CSS 类名补充（文案为图片字、文本匹配不到时兜底）
    for pg, frame in list(iter_frames(context)):
        for selector in CSS_SELECTORS:
            try:
                for el in frame.locator(selector).all():
                    try:
                        # 类名匹配到的按钮若带文字，须与领取文案一致才点，防误触"未完成/已领取"
                        text = (el.inner_text() or "").strip()
                        if text and not claim_re.match(text):
                            continue
                        if not el.is_visible():
                            continue
                        el.click(timeout=3000)
                        claimed += 1
                        log.info("已点击元素按钮(%s)", selector)
                        pg.wait_for_timeout(1500)
                        close_popups(context)
                    except Exception:
                        continue
            except Exception:
                continue
    return claimed


def claim_all(context) -> int:
    """反复扫，直到没有新的可领取按钮。"""
    total = 0
    for round_no in range(1, MAX_ROUNDS + 1):
        n = claim_round(context)
        log.info("第 %d 轮扫描：领取 %d 次", round_no, n)
        total += n
        if n == 0:
            break
    return total


# ---------------------------------------------------------------------------
# 登录态保活：备份 / 恢复 / 复查
# ---------------------------------------------------------------------------

# localStorage 读写：设备指纹与 start_token 都存在这里
JS_LOCAL_STORAGE = """() => {
  const out = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    out[k] = localStorage.getItem(k);
  }
  return out;
}"""
JS_SET_LOCAL_STORAGE = """(data) => {
  for (const k in data) {
    try { localStorage.setItem(k, data[k]); } catch (e) {}
  }
}"""


def _cookie_expiry(cookie) -> float:
    """取 cookie 的过期时间戳；会话级 cookie 返回 0。"""
    return cookie.get("expires") or 0


def _fmt_expiry(expires: float) -> str:
    if not expires or expires <= 0:
        return "会话级"
    return time.strftime("%Y-%m-%d", time.localtime(expires))


def _days_left(expires: float) -> int:
    """剩余天数；会话级 cookie 返回 -1（不参与判断）。"""
    if not expires or expires <= 0:
        return -1
    return int((expires - time.time()) // 86400)


def snapshot_session(context, page) -> dict:
    """把"能免扫码重建登录"的东西整份抓下来：

    1. cookie —— 全量（.qq.com 的登录票据 + .game.qq.com 的 AMS 会话 + .start.qq.com 的业务会话）
    2. localStorage —— 设备指纹与 start_token

    两者缺一不可：只还原 cookie 认不出这台机器，只还原 localStorage 又没有登录票据；
    而且 .game.qq.com 那组 ams_* 凭据缺失时，任务面板会打开但读不到积分。
    """
    picked = []
    for c in context.cookies():
        picked.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": _cookie_expiry(c),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": c.get("sameSite", "Lax"),
        })

    storage = {}
    try:
        storage = page.evaluate(JS_LOCAL_STORAGE) or {}
    except Exception:
        pass

    return {"saved_at": int(time.time()), "cookies": picked, "local_storage": storage}


def save_session_backup(context, page) -> dict:
    """把凭据备份到 session_backup.json（权限 600，仅本人可读）。

    cookie 部分只增不减：新抓到的若比备份里少（例如刚自愈、只发了 start_uid），
    就以旧的为底做合并，避免把一份完整备份越写越小。
    """
    snap = snapshot_session(context, page)
    if not snap["cookies"] and not snap["local_storage"]:
        return snap

    old = _load_session_backup()
    merged = {(c["domain"], c["name"]): c for c in old.get("cookies", [])}
    for c in snap["cookies"]:
        merged[(c["domain"], c["name"])] = c
    now = time.time()
    snap["cookies"] = [
        c for c in merged.values()
        if not c.get("expires") or c["expires"] <= 0 or c["expires"] > now
    ]
    if not snap["local_storage"]:
        snap["local_storage"] = old.get("local_storage", {})

    path = _resolve(SESSION_BACKUP)
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return snap


def _load_session_backup() -> dict:
    path = _resolve(SESSION_BACKUP)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def restore_session_backup(context, page) -> int:
    """把备份灌回浏览器（cookie + localStorage），返回恢复的凭据条数。

    灌完必须再访问一次首页，让腾讯拿这些凭据去换发当次会话；
    单靠 cookie 不够——设备指纹在 localStorage 里，两者齐了才认得出这台机器。
    """
    snap = _load_session_backup()
    cookies = []
    now = time.time()
    for c in snap.get("cookies", []):
        exp = c.get("expires") or -1
        if exp > 0 and exp < now:
            continue  # 已过期的别灌，免得帮倒忙
        cookies.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "expires": exp,
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": c.get("sameSite", "Lax"),
        })
    if not cookies:
        return 0
    context.add_cookies(cookies)

    storage = snap.get("local_storage") or {}
    if storage:
        try:
            page.evaluate(JS_SET_LOCAL_STORAGE, storage)
        except Exception:
            pass
    return len(cookies)


def session_report(context) -> dict:
    """汇总当前登录态"还能撑多久"：返回 (剩余天数, 最早到期的凭据名, 过期时间)。"""
    soonest_name, soonest_exp = None, 0.0
    for c in context.cookies():
        if c["name"] not in SESSION_COOKIE_NAMES:
            continue
        exp = _cookie_expiry(c)
        if exp <= 0:
            continue
        if soonest_exp == 0 or exp < soonest_exp:
            soonest_name, soonest_exp = c["name"], exp
    if not soonest_name:
        return {"days": -1, "name": None, "expires": 0}
    return {"days": _days_left(soonest_exp), "name": soonest_name, "expires": soonest_exp}


def log_session_report(report: dict) -> None:
    """把"登录态还能用多久"打到日志，快到期时给出明确提醒。"""
    if report["days"] < 0:
        log.info("[保活] 未取到长期凭据的有效期。")
        return
    if report["days"] <= SESSION_WARN_DAYS:
        log.info(
            "[保活] 提醒：最早的凭据 %s 将于 %s 到期（还剩 %d 天），"
            "建议尽快跑一次 python main.py login 续上。",
            report["name"], _fmt_expiry(report["expires"]), report["days"],
        )
    else:
        log.info(
            "[保活] 登录态健康：最早的凭据 %s 到期于 %s，还剩 %d 天。",
            report["name"], _fmt_expiry(report["expires"]), report["days"],
        )


def touch_session(context, page) -> bool:
    """轻量"续期"：访问一次首页，让腾讯把 30 天期的凭据滚动顺延。

    实测 start_uid / pt_guid_sig 每次访问都会重新计时，
    所以只要间隔小于 30 天跑一次，登录态就不会自然过期。
    """
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        return not find_login_button(context)
    except Exception as exc:
        log.info("[保活] 访问首页失败：%s", exc)
        return False


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_login(wait_secs: int) -> int:
    """首次登录：弹出可见浏览器窗口，自动点登录框，轮询等待人工扫码。"""
    with sync_playwright() as pw:
        ctx = open_context(pw, headless=False)  # 扫码必须有可见窗口
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if find_login_button(ctx):
            try:
                page.locator(".user-info", has_text="登录").first.click(timeout=5000)
                log.info("已自动点击登录按钮，弹出二维码后请用手机 QQ/微信扫码。")
            except Exception as exc:
                log.info("自动点登录失败(%s)，请在页面上手动点右上角'登录'。", exc)

        log.info("等待扫码中，最长等 %d 秒……扫码成功后脚本会自动确认并关闭浏览器。", wait_secs)
        deadline = time.time() + wait_secs
        ok = False
        while time.time() < deadline:
            if not find_login_button(ctx):
                ok = True
                break
            page.wait_for_timeout(2000)

        snap = save_session_backup(ctx, page) if ok else {"cookies": []}
        report = session_report(ctx)
        ctx.close()
    if ok:
        log.info("[登录] 成功：状态已保存到 %s，以后无需再扫码。", PROFILE_DIR)
        log.info("[保活] 已备份 %d 条凭据到 %s，登录态丢失时可自动恢复。",
                 len(snap["cookies"]), SESSION_BACKUP)
        log_session_report(report)
        return 0
    log.info("[登录] 失败：超时未检测到登录，请重跑本命令再试一次。")
    return 1


def _load_state() -> dict:
    path = _resolve(STATE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    path = _resolve(STATE_FILE)
    state["history"] = state.get("history", [])[-29:]  # 最多保留 30 条
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_run(force: bool, keep_open: bool, visible: bool, check_only: bool) -> int:
    # --check 模式忽略当日去重（随时可查），也不写 state.json
    if not check_only:
        state = _load_state()
        today = date.today().isoformat()
        if state.get("last_success") == today and not force:
            log.info("今天(%s)已成功运行过，跳过。加 --force 可强制重跑。", today)
            return 0

    with sync_playwright() as pw:
        ctx = open_context(pw, headless=not visible)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)  # 等页面 JS 渲染

            # 登录状态是第一件要确认的事
            if find_login_button(ctx):
                # 掉登录了先别急着重扫码：用备份原地恢复一次
                log.info("[登录] 未登录，尝试用 %s 里的凭据自动恢复……", SESSION_BACKUP)
                n = restore_session_backup(ctx, page)
                if n and touch_session(ctx, page):
                    log.info("[登录] 成功：已用备份的 %d 条凭据免扫码恢复。", n)
                else:
                    log.info("[登录] 失败：备份凭据也无法恢复，请先运行 python main.py login")
                    return 2
            else:
                log.info("[登录] 成功：登录状态有效")

            # 顺手把凭据再续一次并刷新备份（每次访问都会滚动顺延 30 天）
            save_session_backup(ctx, page)

            try_open_task_panel(ctx)
            page.wait_for_timeout(3000)

            # 先检测（只看不点），把"有几项可领取"写进日志
            found = detect_claimable(ctx, verbose=True)

            if check_only:
                log_points_info(read_points_info(ctx))
                log_session_report(session_report(ctx))
                log.info("[结果] 登录正常；发现 %d 项可领取（--check 只检测不领取）。", len(found))
                return 0

            # 再执行领取
            total = claim_all(ctx)
            remaining = detect_claimable(ctx) if (found or total) else []
            log.info("[结果] 本次领取 %d 项，剩余可领取 %d 项。", total, len(remaining))

            # 领取完再读一次积分（余额是领取后的最新值）
            points = read_points_info(ctx)
            log_points_info(points)

            state["last_success"] = today
            state["last_claim_count"] = total
            state["last_claimable"] = len(found)
            if points.get("balance") is not None:
                state["last_points"] = points["balance"]
            if points.get("expire_date"):
                state["points_expire_amount"] = points.get("expire_amount")
                state["points_expire_date"] = points["expire_date"]
            state.setdefault("history", []).append(
                {"date": today, "claimed": total, "claimable": len(found),
                 "points": points.get("balance")}
            )
            _save_state(state)

            if keep_open:
                input(">>> 已保持浏览器打开，检查完毕后回车关闭...")
        finally:
            ctx.close()
    return 0


JS_TEXT = "() => (document.body.innerText || '').slice(0, 6000)"


def cmd_renew(visible: bool) -> int:
    """登录态保活：轻量访问一次首页，滚动续期凭据并报告剩余有效期。

    比 run 轻得多（不点菜单、不领奖、不读积分），适合单独挂在更频繁的计划任务上，
    例如每天跑 renew、月底跑 run，让 30 天期的凭据永远处在"刚续过"的状态。
    """
    with sync_playwright() as pw:
        ctx = open_context(pw, headless=not visible)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ok = touch_session(ctx, page)
            if not ok:
                # 掉登录了：先用备份原地恢复，恢复不了才让人去扫码
                log.info("[登录] 未登录，尝试用 %s 里的凭据自动恢复……", SESSION_BACKUP)
                n = restore_session_backup(ctx, page)
                if n and touch_session(ctx, page):
                    log.info("[登录] 成功：已用备份的 %d 条凭据免扫码恢复。", n)
                    ok = True
                else:
                    log.info("[登录] 失败：备份凭据也无法恢复，请运行 python main.py login 重新扫码。")
                    return 2

            snap = save_session_backup(ctx, page)
            log.info("[结果] 登录态保活完成，凭据备份共 %d 条。", len(snap["cookies"]))
            log_session_report(session_report(ctx))
        finally:
            ctx.close()
    return 0


def cmd_probe() -> int:
    """调试：走到任务面板，把每个 frame 的可见文字导出到 logs/dom_dump.txt。"""
    out_lines = []
    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            if find_login_button(ctx):
                log.info("提示：当前未登录，导出的将是未登录页面。建议先 python main.py login")

            try_open_task_panel(ctx)
            page.wait_for_timeout(5000)

            out_lines.append(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            out_lines.append(f"标签页数: {len(ctx.pages)}")
            for p_idx, pg in enumerate(ctx.pages):
                out_lines.append(f"\n{'=' * 30} 标签页 {p_idx}: {pg.url} {'=' * 30}")
                for i, frame in enumerate(pg.frames):
                    try:
                        text = frame.evaluate(JS_TEXT)
                    except Exception:
                        continue
                    if text and text.strip() and "wxopensdk" not in text[:200]:
                        out_lines.append(f"\n--- frame {i}: {frame.url} ---")
                        out_lines.append(text)
        finally:
            ctx.close()

    out = _resolve(LOGS_DIR) / "dom_dump.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(out_lines), encoding="utf-8")
    log.info("页面文字已导出到 %s，可对照修改 CLAIM_KEYWORDS。", out)
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="start_points_claimer",
        description="腾讯 START 云游戏积分任务自动领取助手（真实浏览器模拟，无需逆向接口）",
    )
    parser.add_argument("--version", action="version", version=f"start_points_claimer {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="首次登录（弹窗扫码）")
    p_login.add_argument("--wait", type=int, default=LOGIN_WAIT_SECONDS, help="等待扫码秒数")

    p_run = sub.add_parser("run", help="日常自动领取")
    p_run.add_argument("--visible", action="store_true", help="弹出浏览器窗口运行")
    p_run.add_argument("--keep-open", action="store_true", help="结束后不自动关浏览器")
    p_run.add_argument("--force", action="store_true", help="忽略当天已运行记录")
    p_run.add_argument("--check", action="store_true", help="只检测有几项可领取，不点击（忽略当日去重）")

    p_renew = sub.add_parser("renew", help="登录态保活：轻量访问一次，续期凭据并报告剩余有效期")
    p_renew.add_argument("--visible", action="store_true", help="弹出浏览器窗口运行")

    sub.add_parser("probe", help="调试：导出任务面板页面文字")

    args = parser.parse_args()
    setup_logging()

    if args.cmd == "login":
        return cmd_login(args.wait)
    if args.cmd == "renew":
        return cmd_renew(args.visible)
    if args.cmd == "probe":
        return cmd_probe()
    return cmd_run(force=args.force, keep_open=args.keep_open,
                   visible=args.visible, check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
