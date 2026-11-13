#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
start_points_claimer —— 腾讯 START 云游戏（start.qq.com）积分任务自动领取助手

原理：用 Playwright 驱动一个带本地存档的真实浏览器，按真实用户路径
（头像 → 我的资产 → 积分）进入积分任务面板，发现可领取的按钮就点击。
全程不伪造任何网络请求，行为与人工操作一致，不产生任何截图。

用法：
    python main.py login        # 首次登录（弹窗扫码）
    python main.py run          # 日常运行：进入任务面板，自动领取可领奖励
    python main.py run --check  # 只检测有几项可领取，不点击
    python main.py renew        # 登录态检查：轻量访问一次，失效时自动补登录
    python main.py probe        # 调试：导出任务面板页面文字（页面改版排查用）

日志固定输出四类关键信息：
    [登录] 成功/失败        —— 登录状态是否有效（以服务端探活为准）
    [检测] 发现 N 项可领取  —— 面板里有几个"领取"按钮（含任务描述）
    [结果] 本次领取 X 项，剩余可领取 Y 项
    [积分] 总积分 N，最近过期 M 积分将于 YYYY-MM-DD 过期

登录态说明：
    腾讯 START 的登录态无法靠定时访问长期维持（实测 3~7 天即被服务端作废），
    只有 start_uid 会随访问首页顺延，其余凭据不会自动续期。所以 run / renew
    都带三级自救：现有存档 → session_backup.json → QQ 快捷登录自动重登。
    前两级静默完成；第三级默认也在无头窗口里完成，不弹窗。
    三级都失败才需要人工 python main.py login 扫码。

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

VERSION = "0.3.0"

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 内置参数（零配置：平台改版时直接改这里的常量）
# ---------------------------------------------------------------------------

HOME_URL = "https://start.qq.com/webgame/home?ADTAG=10000000"

# 浏览器
HEADLESS = True            # 无头后台运行；run --visible 可临时弹窗
VIEWPORT = (1440, 900)

# QQ 快捷登录靠「https 页面 → localhost.ptlogin2.qq.com:4301」这条回环请求
# 读本机 QQ 号，读到了头像才渲染出来。Chrome 138+ 默认开启本地网络访问检查
# （Local Network Access），会以 "Permission was denied for this request to
# access the `loopback` address space" 把这条请求挡掉，结果是登录框里永远
# 只剩扫码、头像不渲染——无头和可见窗口都会中招。
# 关掉这项检查即可恢复；它只管本地回环请求的权限判定，不影响页面其他行为。
# 同时写上旧名 PrivateNetworkAccessChecks，兼容尚未改名的 Chromium 版本。
LOCAL_NETWORK_FLAGS = [
    "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks",
]

# 补登录（自动重登）默认用无头窗口完成，全程不闪窗。
# 无头几轮都失败时是否回退可见窗口再试：默认关闭——实测卡住的地方是本机 QQ
# 客户端的回环握手，跟窗口是否可见无关，弹窗基本救不回来，反而破坏"全程无感"。
# 个别环境确实只有可见窗口能成，把它改成 True 即可恢复旧的兜底行为。
REPAIR_HEADLESS = True
REPAIR_VISIBLE_FALLBACK = False

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

# 服务端探活：页面每次加载都会 POST 这个接口，拿本地凭据换发当次会话。
# 返回 22001（auth fail）说明凭据已被服务端作废（单点互斥、在别处重新登录、
# 或存档被别的浏览器动过），这时首页可能"看起来"还登录着，但任务面板一定读不到数据。
# 只观察页面自己发的那一次请求，不额外造请求。
SESSION_PROBE_MARK = "/capi/login/v1/session"
SESSION_PROBE_FAIL_CODE = 22001
SESSION_PROBE_WAIT_SECONDS = 8

# 只有这一项实测会随每次访问首页自动顺延（其余不会）。
RENEWABLE_COOKIE_NAMES = ("start_uid",)
# 业务凭据：到期不影响扫码态，但会让积分/资产接口读不到数据
BIZ_COOKIE_PREFIXES = ("ams_",)

# 长期凭据：备份时整份存档，判定"还能用多久"则只看这几项。
# 注意：只有 start_uid 会被滚动顺延（见 RENEWABLE_COOKIE_NAMES）；
# RK / pt_guid_sig / pt_recent_uins 等只在真正走一次登录握手时才刷新，
# 光访问首页不会。所以这份报告只能说明"本地凭据最晚哪天到期"，
# 不能保证登录态一定能用到那天。
SESSION_COOKIE_NAMES = (
    "RK",              # .qq.com      登录票据根（1 年）
    "qlogin_uid",      # .qq.com      登录过的 QQ 号（1 年）
    "pt2gguin",        # .ptlogin2    登录过的 QQ 号（1 年）
    "ptcz",            # .qq.com      客户端指纹（1 年）
    "pt_guid_sig",     # .ptlogin2    "记住我"签名（30 天，不自动续）
    "pt_recent_uins",  # .ptlogin2    最近登录账号列表（30 天，不自动续）
    "start_uid",       # .start.qq.com START 会话（30 天滚动，唯一会自动顺延的）
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
        ] + LOCAL_NETWORK_FLAGS,
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
    """汇总本地凭据的到期情况。

    返回：
        days / name / expires   —— 长期凭据里最早到期的那一条
        biz_days / biz_name / biz_expires —— 业务凭据（ams_*）里最早到期的那一条

    注意这只是"本地这份凭据最晚还能撑到哪天"，**不代表登录态一定能用到那天**：
    实测只有 start_uid 会随访问首页顺延，其余凭据要真正走一次登录握手才刷新，
    而服务端也可能随时把整份凭据作废（单点互斥）。
    """
    def _soonest(names=None, prefixes=()):
        name, exp = None, 0.0
        for c in context.cookies():
            n = c["name"]
            if names is not None and n not in names:
                continue
            if prefixes and not n.startswith(prefixes):
                continue
            e = _cookie_expiry(c)
            if e <= 0:
                continue
            if exp == 0 or e < exp:
                name, exp = n, e
        return name, exp

    name, exp = _soonest(names=SESSION_COOKIE_NAMES)
    biz_name, biz_exp = _soonest(prefixes=BIZ_COOKIE_PREFIXES)

    report = {
        "days": _days_left(exp) if name else -1,
        "name": name, "expires": exp,
        "renewable": name in RENEWABLE_COOKIE_NAMES,
        "biz_days": _days_left(biz_exp) if biz_name else None,
        "biz_name": biz_name, "biz_expires": biz_exp,
    }
    return report


def log_session_report(report: dict, alive=None) -> None:
    """播报本地凭据到期情况。

    alive：服务端探活的结论（True 还认 / False 已作废 / None 未知）。
    这里只说"本地凭据哪天到期"，不再宣称"登录态健康"——那句话会误导人。
    """
    if alive is False:
        log.info("[保活] 登录态已失效：本地凭据被服务端作废，只能重新扫码。")
        return

    if report["days"] < 0:
        log.info("[保活] 未取到长期凭据的有效期。")
    elif report["days"] <= SESSION_WARN_DAYS:
        log.info(
            "[保活] 提醒：本地凭据 %s 将于 %s 到期（还剩 %d 天），"
            "到期前请跑一次 python main.py login 续上。",
            report["name"], _fmt_expiry(report["expires"]), report["days"],
        )
    else:
        renew_note = "会随访问首页顺延" if report.get("renewable") else "不会自动续期"
        log.info(
            "[保活] 本地凭据里最先到期的是 %s（%s，还剩 %d 天，%s）；"
            "登录态能否活到那天取决于服务端，不取决于这个日期。",
            report["name"], _fmt_expiry(report["expires"]), report["days"], renew_note,
        )

    # 业务凭据通常比登录凭据先到期，到期后任务面板读不到积分，但页面仍显示已登录
    if report.get("biz_days") is not None:
        if report["biz_days"] <= SESSION_WARN_DAYS:
            log.info(
                "[保活] 提醒：业务凭据 %s 将于 %s 到期（还剩 %d 天），"
                "它一到期任务面板就读不到积分，需重新 login。",
                report["biz_name"], _fmt_expiry(report["biz_expires"]), report["biz_days"],
            )
        else:
            log.info(
                "[保活] 业务凭据 %s 到期于 %s（还剩 %d 天）。",
                report["biz_name"], _fmt_expiry(report["biz_expires"]), report["biz_days"],
            )


def watch_session_probe(page) -> dict:
    """旁听页面自己发出的登录态探活请求，不额外造请求。

    页面每次加载首页都会 POST {my.start.qq.com}/capi/login/v1/session，
    用本地凭据换发当次会话：code=0 说明服务端还认这份凭据，
    code=22001（auth fail）说明已被作废——此时页面可能仍显示已登录，
    但任务面板注定读不到数据。返回一个可变 dict，供后续读取结论。
    """
    state = {"code": None, "url": ""}

    def on_response(resp):
        try:
            if SESSION_PROBE_MARK not in resp.url:
                return
            state["url"] = resp.url
            body = resp.json() or {}
            state["code"] = body.get("code")
        except Exception:
            pass

    page.on("response", on_response)
    return state


def probe_verdict(state: dict):
    """把探活结果翻译成 True/False/None（未知）。"""
    if not state or state.get("code") is None:
        return None
    return state["code"] == 0


def touch_session(context, page, probe=None) -> bool:
    """访问一次首页：让可滚动的凭据顺延，并判断服务端是否还认这份登录态。

    实测只有 start_uid 会随访问首页顺延（30 天滚动）；pt_guid_sig、
    pt_recent_uins 等要在真正走一次登录握手时才刷新。所以本函数
    **不是**"无限续期"，只是"顺手把 start_uid 往后推"。

    判定以服务端探活为准（probe_verdict），页面上的"登录"按钮只作兜底。
    """
    if probe is not None:
        probe.update({"code": None, "url": ""})
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded")
        deadline = time.time() + SESSION_PROBE_WAIT_SECONDS
        while probe is not None and time.time() < deadline:
            if probe.get("code") is not None:
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.info("[保活] 访问首页失败：%s", exc)
        return False

    verdict = probe_verdict(probe)
    if verdict is True:
        return True
    if verdict is False:
        code = probe.get("code")
        hint = "（auth fail，凭据已被服务端作废）" if code == SESSION_PROBE_FAIL_CODE else ""
        log.info("[登录] 服务端探活失败：code=%s%s", code, hint)
        return False
    return not find_login_button(context)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

# QQ 快捷登录：电脑 QQ 客户端已登录时，登录框默认就有"快捷登录"面板，
# 点一下头像就能登录，无需扫码。头像本体是带 uin 属性的链接（实测于 xui.ptlogin2 登录框）。
# 只保留精确选择器：泛化的 .face 会命中登录框里的其他头像元素，误点会把整个
# 登录弹窗关掉，反而更慢，所以不用。
QUICK_LOGIN_SELECTORS = (
    "a[uin]",              # 头像链接（uin=QQ号，最可靠）
    "#qlogin_list .face",  # 快捷登录列表里的头像
)
QUICK_LOGIN_WAIT_SECONDS = 30  # 等本机 QQ 客户端握手、快捷登录面板出现的最长时间
# 登录框出现后，正常 4~5 秒就该渲染出快捷登录头像；超过这个时间还没有，
# 基本是本机 QQ 的回环握手这一轮失败了（日志里是"尝试完所有端口都失败了"），
# 与其干等，不如早点回去重新握手
QUICK_LOGIN_GIVEUP_AFTER = 15
# 本地端口握手偶尔整体失败，重新加载一次首页通常就能重新握手成功，所以给两次机会。
# （能否拿到头像取决于本机 QQ 每次握手成没成，属抽签性质，重载比关掉重开更有效）
QUICK_LOGIN_ROUNDS = 3
# 点"登录"到 ptlogin2 登录框真正加载出来实测要 10~15 秒（要先经过
# graph.qq.com/oauth2.0/authorize 跳一次），等得短了会误判成失败
LOGIN_DIALOG_WAIT_SECONDS = 20
LOGIN_CLICK_RETRIES = 3
LOGIN_ENTRY_SELECTOR = ".user-info"
LOGIN_ENTRY_WAIT_SECONDS = 8  # 首页刚打开时顶栏头像区还没渲染，点之前先等它出现

# 登录框是往首页里插 iframe。frame URL 要等加载完才有值，而加载期间
# 页面已经被遮罩盖住（此时再点"登录"必定超时）。所以判定"登录框是否已开出"
# 不能只看 frame URL，还要看 DOM 里有没有对应的 iframe 元素。
JS_LOGIN_IFRAME = """() => Array.from(document.querySelectorAll('iframe')).some(f => {
  const s = f.getAttribute('src') || '';
  return s.indexOf('ptlogin2') >= 0 || s.indexOf('graph.qq.com/oauth2.0') >= 0;
})"""
# 头像点完后 OAuth 授权页可能停在确认页，只做精确匹配点击（避免误点
# "授权即同意服务协议"这类静态文字），且仅在快捷登录流程中使用
AUTHORIZE_LABELS = ("授权并登录", "授权并进入")


def wait_login_entry(context, page, timeout: float = LOGIN_ENTRY_WAIT_SECONDS) -> bool:
    """等顶栏的登录/头像区（.user-info）渲染出来。

    首页是 SPA，顶栏异步挂载；无头下虽然快，但偶尔还是没等它出来就点，
    点击会直接超时。先等元素出现再点，能明显减少"点登录没反应"的假失败。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.locator(LOGIN_ENTRY_SELECTOR).first.is_visible():
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def open_login_dialog(context, page) -> bool:
    """点右上角"登录"，把 QQ 登录框拉起来；拉不起来才重试。

    登录框要走一段 graph.qq.com/oauth2.0/authorize 跳转，再插入 ptlogin2
    iframe，实测 10~15 秒才真正可用。两个坑：
    1. 只等 frame URL 会误判——iframe 加载期间 URL 还是空的，但页面已被遮罩
       盖住，此时"重试点击"必定超时，于是把已经打开的登录框当成没打开。
    2. 因此这里先额外判定 DOM 里是否已插入登录 iframe；一旦插入就只等它加载完，
       不再重复点击。
    """
    def iframe_inserted() -> bool:
        try:
            return bool(page.evaluate(JS_LOGIN_IFRAME))
        except Exception:
            return False

    def dialog_ready() -> bool:
        for pg, frame in list(iter_frames(context)):
            u = frame.url or ""
            if "ptlogin2" in u or "graph.qq.com/oauth2.0" in u:
                return True
        return False

    deadline = time.time() + LOGIN_DIALOG_WAIT_SECONDS
    for attempt in range(1, LOGIN_CLICK_RETRIES + 1):
        if not iframe_inserted():
            try:
                # 点登录会拉起跨域 iframe，用 no_wait_after 免得干等加载完成
                page.locator(LOGIN_ENTRY_SELECTOR).first.click(
                    timeout=5000, no_wait_after=True)
            except Exception as exc:
                # 点击超时也可能只是被遮罩挡着，说明登录框已经开了，继续等它加载
                if iframe_inserted():
                    break
                log.info("第 %d 次点登录未成功(%s)，请在页面上手动点右上角'登录'。", attempt, exc)
                return False

        while time.time() < deadline:
            if dialog_ready():
                if attempt > 1:
                    log.info("第 %d 次点登录成功，登录框已弹出。", attempt)
                else:
                    log.info("已自动点击登录按钮，登录框已弹出。")
                return True
            page.wait_for_timeout(500)
        if attempt < LOGIN_CLICK_RETRIES:
            log.info("第 %d 次点登录后没等到登录框，重试……", attempt)
            deadline = time.time() + LOGIN_DIALOG_WAIT_SECONDS
    log.info("多次点击都没拉起登录框，请手动点右上角'登录'。")
    return False


def try_quick_login(context) -> bool:
    """检测登录框里的 QQ 快捷登录并点头像，点到了返回 True，没有返回 False。

    点登录后登录框先显示密码页签，等本机 QQ 客户端握手完成（约 4 秒），
    面板自动切成"快捷登录"并显示出头像（带 uin 属性的链接），届时点击。

    点头像会触发跨域跳转，点击动作本身经常以"frame 已销毁"报错收场——
    这恰恰说明点生效了，所以这里对异常宽容：只要点出去了就算成功，
    真正的登录结果由调用方按登录态判断。
    """
    deadline = time.time() + QUICK_LOGIN_WAIT_SECONDS
    # 只在"登录框已加载完、但头像迟迟不来"时才提前放弃；
    # 登录框本身要 10~15 秒才加载出来，这段时间不能算进放弃计时
    giveup_at = None
    while time.time() < deadline:
        for pg, frame in list(iter_frames(context)):
            if "ptlogin2" not in (frame.url or ""):
                continue
            if giveup_at is None:
                giveup_at = time.time() + QUICK_LOGIN_GIVEUP_AFTER
            for selector in QUICK_LOGIN_SELECTORS:
                try:
                    el = frame.locator(selector).first
                    if not el.is_visible():
                        continue
                except Exception:
                    continue
                # 依次尝试：普通点击 → 强制点击 → 直接派发事件（动画遮挡时的兜底）
                for attempt in ("click", "force", "event"):
                    try:
                        if attempt == "click":
                            el.click(timeout=3000, no_wait_after=True)
                        elif attempt == "force":
                            el.click(timeout=3000, force=True, no_wait_after=True)
                        else:
                            el.dispatch_event("click")
                        log.info("检测到 QQ 快捷登录，已点击头像自动登录。")
                        return True
                    except Exception as exc:
                        # 跨域跳转把 frame 干掉了：点击其实已经生效
                        if any(k in str(exc) for k in ("detached", "navigat", "Target closed")):
                            log.info("检测到 QQ 快捷登录，已点击头像自动登录。")
                            return True
                        continue
        # 登录框已加载但头像迟迟不来：这一轮本地握手多半失败了，早点回去重握手
        if giveup_at and time.time() > giveup_at:
            log.info("登录框里没出现快捷登录头像（本机 QQ 握手未成功），稍后重试。")
            return False
        time.sleep(1)
    return False


def cmd_login(wait_secs: int) -> int:
    """首次登录：弹出可见浏览器窗口，自动点登录框。

    电脑上 QQ 客户端已登录时优先走"QQ 快速登录"（点头像即进，无需人工介入）；
    没有快速登录才轮询等待人工扫码。
    """
    with sync_playwright() as pw:
        ctx = open_context(pw, headless=False)  # 快速登录/扫码都要可见窗口
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        wait_login_entry(ctx, page)

        quick_login = False
        if find_login_button(ctx):
            # 登录框拉起来之后再判断有没有快捷登录
            if open_login_dialog(ctx, page) and try_quick_login(ctx):
                quick_login = True
            else:
                log.info("没有可用的 QQ 快捷登录，等待扫码中，最长等 %d 秒……", wait_secs)

        deadline = time.time() + (wait_secs if not quick_login else 60)
        ok = False
        while time.time() < deadline:
            if not find_login_button(ctx):
                ok = True
                break
            # 快捷登录点头像后可能停在 OAuth 授权确认页，精确匹配点掉
            if quick_login:
                _click_first(ctx, AUTHORIZE_LABELS, extra_sel="#approve")
            page.wait_for_timeout(2000)

        alive = None
        if ok:
            # 回首页做一次服务端探活：页面显示已登录不等于凭据真能被服务端认下
            probe = watch_session_probe(page)
            alive = touch_session(ctx, page, probe)
            if not alive:
                log.info("[登录] 页面上看似已登录，但服务端探活未通过，凭据可能未生效。")

        if ok and quick_login:
            log.info("[登录] QQ 快捷登录成功，无需扫码。")
        elif not ok:
            log.info("超时未检测到登录。")

        snap = save_session_backup(ctx, page) if ok else {"cookies": []}
        report = session_report(ctx)
        ctx.close()
    if ok:
        log.info("[登录] 成功：状态已保存到 %s，以后无需再扫码。", PROFILE_DIR)
        log.info("[保活] 已备份 %d 条凭据到 %s，登录态丢失时可自动恢复。",
                 len(snap["cookies"]), SESSION_BACKUP)
        log_session_report(report, alive=alive)
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


def _quick_login_once(pw, headless: bool, wait_secs: int) -> bool:
    """开一个窗口，在里面反复尝试 QQ 快捷登录，成功把凭据存进 profile 并返回 True。

    重试放在**同一个窗口内重载首页**，而不是关掉重开：
    QQ 快捷登录能不能成，取决于本机 QQ 客户端那次回环握手成功没有
    （日志里"尝试完所有端口都失败了"就是没成功），而这件事每次页面加载
    都是重新抽一次签。重载比关掉重开快得多，实测命中率也更高。
    """
    ctx = open_context(pw, headless=headless)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        for round_no in range(1, QUICK_LOGIN_ROUNDS + 1):
            log.info("[续期] 打开%s窗口，尝试用 QQ 快捷登录自动恢复（第 %d 轮，无需你操作）……",
                     "无头" if headless else "可见", round_no)
            if _quick_login_round(ctx, page, wait_secs):
                return True
            if round_no < QUICK_LOGIN_ROUNDS:
                log.info("[续期] 本轮未成功，重新加载页面再试……")
                page.wait_for_timeout(2000)
        return False
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _quick_login_round(ctx, page, wait_secs: int) -> bool:
    """一轮完整尝试：加载首页 → 拉登录框 → 点头像 → 等登录完成 → 服务端确认。"""
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    if not find_login_button(ctx):
        # 顶栏还没渲染完时 find_login_button 也会返回 False，先等一会儿再判一次，
        # 免得把"页面还没加载好"误判成"已经是登录状态"
        wait_login_entry(ctx, page)
        if not find_login_button(ctx):
            log.info("[续期] 页面打开后已经是登录状态，无需操作。")
            save_session_backup(ctx, page)
            return True

    if not open_login_dialog(ctx, page):
        return False
    if not try_quick_login(ctx):
        return False

    # 点头像后可能停在 OAuth 授权确认页，精确匹配点掉并等登录完成
    deadline = time.time() + wait_secs
    ok = False
    while time.time() < deadline:
        if not find_login_button(ctx):
            ok = True
            break
        _click_first(ctx, AUTHORIZE_LABELS, extra_sel="#approve")
        page.wait_for_timeout(2000)
    if not ok:
        log.info("[续期] 等待登录完成超时。")
        return False

    # 回首页做服务端确认，通过才存备份（避免把坏凭据覆盖上去）
    probe = watch_session_probe(page)
    if not touch_session(ctx, page, probe):
        log.info("[续期] 页面显示已登录，但服务端探活未通过。")
        return False

    save_session_backup(ctx, page)
    log.info("[续期] QQ 快捷登录成功，登录态已恢复并保存。")
    return True


def quick_login_into_profile(pw, wait_secs: int = QUICK_LOGIN_WAIT_SECONDS + 30,
                             headless: bool = REPAIR_HEADLESS) -> bool:
    """走 QQ 快捷登录把凭据写回 profile，返回是否成功。

    默认在**无头窗口**里完成，全程不弹窗：
    1. 无头能成，就完全不弹窗——这是默认路径。
       （历史上无头拿不到头像，根因是 Chrome 138+ 的本地网络访问检查挡住了
        localhost.ptlogin2 回环请求，已在 open_context 里关掉，见
        LOCAL_NETWORK_FLAGS；关掉后无头同样能渲染快捷登录头像。）
    2. 同一个窗口里最多重载试 QUICK_LOGIN_ROUNDS 轮——能否拿到头像取决于
       本机 QQ 客户端每次握手成没成，属抽签性质，重载比关掉重开更管用。
    3. REPAIR_VISIBLE_FALLBACK 打开时，无头全败后才回退可见窗口兜底；
       默认关闭，因为实测失败原因是本机 QQ 侧握手，弹窗救不回来。
    4. 同一个 profile 目录不能被两个浏览器同时占用，调用前必须先关掉原 context。
    """
    if not headless:
        return _quick_login_once(pw, False, wait_secs)

    if _quick_login_once(pw, True, wait_secs):
        return True
    if not REPAIR_VISIBLE_FALLBACK:
        log.info("[续期] 登录框里没有可用的 QQ 快捷登录，自动恢复失败。")
        return False
    log.info("[续期] 无头模式没能完成快捷登录，回退可见窗口再试一次……")
    if _quick_login_once(pw, False, wait_secs):
        return True
    log.info("[续期] 登录框里没有可用的 QQ 快捷登录，自动恢复失败。")
    return False


def acquire_logged_in_context(pw, visible: bool, allow_repair: bool = True):
    """打开浏览器并保证登录可用，返回 (context, page, alive)。

    三级阶梯，越往后越慢：
        1. 存档里现有凭据还认 → 直接用（绝大多数情况，一次都不用重登）
        2. 不认 → 灌回 session_backup.json 再试（profile 被清空的场景）
        3. 还不认 → 关掉当前实例，走 QQ 快捷登录（服务端作废的场景）

    第 3 步默认在无头窗口里完成，全程不弹窗；只有无头拿不到快捷登录头像
    （极少数环境）才回退可见窗口闪一下。allow_repair=False 时跳过第 3 步，
    交给调用方决定（`login` 命令自己就走可见窗口）。
    """
    ctx = open_context(pw, headless=not visible)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    probe = watch_session_probe(page)
    if touch_session(ctx, page, probe):
        log.info("[登录] 成功：登录状态有效")
        return ctx, page, True

    # 第 2 级：本地恢复
    log.info("[登录] 未登录，尝试用 %s 里的凭据自动恢复……", SESSION_BACKUP)
    n = restore_session_backup(ctx, page)
    if n and touch_session(ctx, page, probe):
        log.info("[登录] 成功：已用备份的 %d 条凭据免扫码恢复。", n)
        return ctx, page, True

    if not allow_repair:
        return ctx, page, False

    # 第 3 级：自动补登录（必须独占 profile，先关掉当前实例）
    log.info("[登录] 本地凭据已失效，尝试自动重新登录……")
    try:
        ctx.close()
    except Exception:
        pass
    # 补登录与 run 的可见性无关：默认走无头，避免计划任务里闪窗
    if not quick_login_into_profile(pw, headless=True):
        return None, None, False

    # 重新用正常模式（可能是无头）打开，确认恢复成功
    ctx = open_context(pw, headless=not visible)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    probe = watch_session_probe(page)
    if touch_session(ctx, page, probe):
        log.info("[登录] 成功：已用 QQ 快捷登录自动恢复，本次无需人工扫码。")
        return ctx, page, True
    return ctx, page, False


def cmd_run(force: bool, keep_open: bool, visible: bool, check_only: bool) -> int:
    # --check 模式忽略当日去重（随时可查），也不写 state.json
    if not check_only:
        state = _load_state()
        today = date.today().isoformat()
        if state.get("last_success") == today and not force:
            log.info("今天(%s)已成功运行过，跳过。加 --force 可强制重跑。", today)
            return 0

    with sync_playwright() as pw:
        ctx, page, alive = acquire_logged_in_context(pw, visible=visible)
        if not alive:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
            log.info("[登录] 失败：本地凭据与 QQ 快捷登录都没能恢复，请运行 python main.py login 手动扫码")
            return 2
        try:
            # 顺手刷新一次备份（start_uid 会随本次访问顺延）
            save_session_backup(ctx, page)

            try_open_task_panel(ctx)
            page.wait_for_timeout(3000)

            # 先检测（只看不点），把"有几项可领取"写进日志
            found = detect_claimable(ctx, verbose=True)

            if check_only:
                log_points_info(read_points_info(ctx))
                log_session_report(session_report(ctx), alive=alive)
                log.info("[结果] 登录正常；发现 %d 项可领取（--check 只检测不领取）。", len(found))
                return 0

            # 再执行领取
            total = claim_all(ctx)
            remaining = detect_claimable(ctx) if (found or total) else []
            log.info("[结果] 本次领取 %d 项，剩余可领取 %d 项。", total, len(remaining))

            # 领取完再读一次积分（余额是领取后的最新值）
            points = read_points_info(ctx)
            log_points_info(points)

            # 领取完报一下本地凭据的到期情况，顺便让用户知道"还能撑多久"
            log_session_report(session_report(ctx), alive=True)

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


def cmd_renew(visible: bool, repair: bool = True) -> int:
    """登录态检查（并默认自动补登录）：轻量访问一次，判断凭据是否仍有效。

    比 run 轻得多（不点菜单、不领奖、不读积分）。要清楚它不能做什么：
    访问首页只会顺延 start_uid（30 天滚动），pt_guid_sig / ams_* 等不会因此刷新，
    服务端也可能随时把整份凭据作废（单点互斥）。

    所以本命令的定位是"尽早发现问题 + 尽力自愈"：
    - 凭据失效时，先用备份恢复，再尝试 QQ 快捷登录自动重新登录（会短暂弹窗）
    - 都失败才返回 2 要求人工扫码
    `--no-repair` 可关掉自动补登录，只做检查。
    """
    with sync_playwright() as pw:
        ctx, page, ok = acquire_logged_in_context(pw, visible=visible, allow_repair=repair)
        try:
            if not ok:
                log.info("[登录] 失败：本地凭据与 QQ 快捷登录都没能恢复，请运行 python main.py login 手动扫码。")
                return 2

            snap = save_session_backup(ctx, page)
            log.info("[结果] 登录态检查完成，凭据备份共 %d 条。", len(snap["cookies"]))
            log_session_report(session_report(ctx), alive=True)
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
    return 0


def cmd_probe() -> int:
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

    p_renew = sub.add_parser("renew", help="登录态检查：轻量访问一次，失效时自动补登录")
    p_renew.add_argument("--visible", action="store_true", help="全程弹出浏览器窗口运行")
    p_renew.add_argument("--no-repair", action="store_true",
                         help="只检查不自动补登录（不弹窗，失效直接返回 2）")

    sub.add_parser("probe", help="调试：导出任务面板页面文字")

    args = parser.parse_args()
    setup_logging()

    if args.cmd == "login":
        return cmd_login(args.wait)
    if args.cmd == "renew":
        return cmd_renew(args.visible, repair=not args.no_repair)
    if args.cmd == "probe":
        return cmd_probe()
    return cmd_run(force=args.force, keep_open=args.keep_open,
                   visible=args.visible, check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
