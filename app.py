#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""katabump 自动登录续期 —— 已迁移到 renew-kit。

公共部分（结果分类 / Telegram / 报告排版 / 时间格式化 / 环境变量）交给 renewkit，
本文件保留 selenium 浏览器自动化：Turnstile 处理、登录、续期提交流程。

迁移带来的行为变化：
    · 登录页加载不出表单、Cloudflare 拦截、Turnstile 连续失败 -> TRANSIENT，
      exit 0 不标红（属上游或风控问题，次日排程自动重试）。
    · 提交后 redirect 返 `?error=captcha`（Turnstile token 被服务端拒收）
      -> TRANSIENT，且同一 run 内即刻重试最多 3 次（换 IP 通常即过）。
      实证 2026-10-07：同一份 code，68.154.54.106 中招、20.40.223.126 一次过。
    · 浏览器/驱动层异常 -> TRANSIENT（环境问题，非业务失败）。
    · 只有「登录被拒」「找不到服务器条目」这类确定性失败才 FAILED；
      其中 `?error=credentials` 才真的是帐密不对，文案会写明。
    · 续期提交后读不到明确提示 -> UNKNOWN（❓ 结果未确认，提醒留意，
      但不算失败；原实现用 ℹ️ 含糊带过，看不出成没成）。
    · 通知失败不再影响退出码。
"""
import subprocess
import time

from seleniumbase import SB

from renewkit import Outcome, RenewReport
from renewkit import env
from renewkit.report import shorten
from renewkit.timeutil import now_local

# 从环境变量获取账号密码（TG 由 renewkit.notify 自行读取）
EMAIL = env.get("KATABUMP_EMAIL")
PASSWORD = env.get("KATABUMP_PASSWORD")

BASE_URL = "https://dashboard.katabump.com"
SERVICE = "katabump"

# 登入層風控重試：Turnstile token 被服務端拒收（redirect 返
# /auth/login?error=captcha）係**機房 IP 聲譽波動**，唔係帳密錯。
# 實測 2026-10-07：同一份 code、同一組 secret ——
#   09:13Z 出口 IP 68.154.54.106 → ?error=captcha（run 37592961199 紅）
#   人手重跑換 IP 20.40.223.126 → 一次過 302 /dashboard（run 37594138457 綠）
# 即係「換一轉即刻好」，所以同一 run 內重試係有效嘅，唔應該等第二日。
LOGIN_MAX_ATTEMPTS = 3
LOGIN_RETRY_WAIT = 5
CAPTCHA_ERR_MARK = "error=captcha"


def masked_account() -> str:
    """脱敏后的账号名，用于报告标题。"""
    if not EMAIL:
        return SERVICE
    if "@" in EMAIL:
        name, domain = EMAIL.split("@", 1)
        return f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else EMAIL
    return EMAIL[:2] + "****"


#  页面注入脚本
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_EXISTS_JS = """
(function(){
    return document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_WININFO_JS = """
(function(){
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight
    };
})()
"""

# ===== 自动续期相关 =====

# 在模态框内查找 iframe 并展开，返回点击坐标
_ALTCHA_EXPAND_JS = """
(function() {
    var modal = document.querySelector('div.modal.show') || document;
    var iframes = modal.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var r = iframes[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            iframes[i].style.width  = '300px';
            iframes[i].style.height = '150px';
            iframes[i].style.minWidth  = '300px';
            iframes[i].style.minHeight = '150px';
            iframes[i].style.visibility = 'visible';
            iframes[i].style.opacity = '1';
            var el = iframes[i];
            for (var j = 0; j < 10; j++) {
                el = el.parentElement;
                if (!el) break;
                el.style.overflow = 'visible';
            }
            var r2 = iframes[i].getBoundingClientRect();
            return { cx: Math.round(r2.x + 30), cy: Math.round(r2.y + r2.height / 2) };
        }
    }
    return null;
})()
"""

# 检测 ALTCHA 是否已验证通过
_ALTCHA_SOLVED_JS = """
(function(){
    var modal = document.querySelector('div.modal.show') || document;
    // hidden input 有值
    var inputs = modal.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < inputs.length; i++) {
        var n = (inputs[i].name || '').toLowerCase();
        if ((n.includes('altcha') || n.includes('captcha')) &&
            inputs[i].value && inputs[i].value.length > 20) return true;
    }
    // checkbox 变为 disabled
    var cbs = modal.querySelectorAll('input[type="checkbox"]');
    for (var j = 0; j < cbs.length; j++) {
        if (cbs[j].disabled) return true;
    }
    // widget data-state 属性
    var w = modal.querySelector('[data-state="verified"],.altcha--verified,.altcha-verified');
    if (w) return true;
    return false;
})()
"""

#  底层输入工具
def js_fill_input(sb, selector: str, text: str):
    sb.execute_script("""
    var el = document.querySelector(arguments[0]);
    if (!el) return;
    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(el, arguments[1]);
    } else {
        el.value = arguments[1];
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    """, selector, text)

def _activate_window():
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# def _xdotool_click(x: int, y: int):
#     _activate_window()
#     try:
#         subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
#         time.sleep(0.15)
#         subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
#     except Exception:
#         os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

#  人机验证处理（使用 SeleniumBase 内置 uc_gui_click_captcha）
def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    # 检查是否已静默通过
    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    # 尝试展开 Turnstile（防止被父容器 overflow:hidden 裁剪）
    for _ in range(3):
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        time.sleep(0.5)

    # 使用 SeleniumBase 内置 uc_gui_click_captcha 处理 Turnstile
    # 该方法自动完成：检测验证码类型 → 定位 iframe → 计算坐标 → PyAutoGUI 平滑点击
    for attempt in range(6):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过（第 {attempt} 次尝试）")
            return True

        print(f"🖱️ 第 {attempt + 1} 次调用 uc_gui_click_captcha...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")

        # 等待验证结果（最多 8 秒）
        for _ in range(16):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
                return True

        print(f"⚠️ 第 {attempt + 1} 次未通过，重试...")

    print("  ❌ Turnstile 6 次均失败")
    return False

#  账户登录（單次嘗試）
def _login_once(sb) -> tuple[bool, Outcome, str]:
    """單次登入嘗試。返回 (是否成功, 失败分类, 说明)。"""
    print(f"🌐 打开登录页面: {BASE_URL}/auth/login")
    sb.uc_open_with_reconnect(BASE_URL + "/auth/login", reconnect_time=8)
    time.sleep(8)

    # 先等待 Cloudflare 验证通过（最多等 30 秒）
    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过（{i+1}s）")
            break
        time.sleep(1)
    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[type="email"]', timeout=15)
    except Exception:
        # 尝试大写选择器作为后备
        try:
            sb.wait_for_element('input[type="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            print(f"  当前 URL: {sb.get_current_url()}")
            print(f"  当前标题: {sb.get_title() or ''}")
            sb.save_screenshot("login_load_fail.png")
            # 登录页根本打不开 = 站点/CF 问题，不是脚本错
            return False, Outcome.TRANSIENT, "登录页未加载出表单（站点不可达或被 Cloudflare 拦截）"

    print("🍪 关闭可能的 Cookie 弹窗...")
    try:
        for btn in sb.find_elements("button"):
            if "Accept" in (btn.text or ""):
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    print("📧 填写邮箱...")
    js_fill_input(sb, 'input[type="email"]', EMAIL)
    time.sleep(1)

    print("🔑 填写密码...")
    js_fill_input(sb, 'input[type="password"]', PASSWORD)
    time.sleep(3)

    # 等待 Turnstile 验证框出现（最多 10 秒）
    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    for i in range(10):
        if sb.execute_script(_EXISTS_JS):
            ts_found = True
            print(f"✅ 检测到 Turnstile（{i+1}s）")
            break
        time.sleep(1)

    if ts_found:
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            sb.save_screenshot("login_turnstile_fail.png")
            return False, Outcome.TRANSIENT, "Turnstile 连续 6 次未通过（风控，次日自动重试）"
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 敲击回车提交表单...")
    sb.press_keys('input[name="password"]', '\n')

    print("⏳ 等待登录跳转...")
    for _ in range(12):
        time.sleep(1)
        cur_url = sb.get_current_url().split('?')[0].lower()
        page_title = sb.get_title() or ""
        if cur_url.startswith(f"{BASE_URL}/dashboard") or "Dashboard | KataBump" in page_title.lower():
            break

    cur_url = sb.get_current_url().split('?')[0].lower()
    page_title = sb.get_title() or ""
    if cur_url.startswith(f"{BASE_URL}/dashboard") or "Dashboard | KataBump" in page_title.lower():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True, Outcome.SKIPPED, ""

    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    sb.save_screenshot("login_failed.png")
    return False, *_classify_login_failure(sb.get_current_url() or "")


def _classify_login_failure(url: str) -> tuple[Outcome, str]:
    """按登入後 URL 上嘅錯誤碼分類，唔好一律當「帳密錯」。

    katabump 係 Laravel：驗證失敗會 redirect 返 /auth/login?error=xxx。
    實測見到嘅碼：
      · error=captcha     → Turnstile token 被服務端拒收（風控，機房 IP 波動）
      · error=credentials → 服務端明確話帳密唔啱（先至係真 FAILED）
    """
    u = (url or "").lower()
    if CAPTCHA_ERR_MARK in u:
        return (Outcome.TRANSIENT,
                "Turnstile 被服務端拒收（風控，非帳密問題）")
    if "error=credentials" in u or "error=password" in u or "error=email" in u:
        return Outcome.FAILED, "帳號或密碼錯誤（服務端明確拒絕）"
    return Outcome.FAILED, "登录被拒或未跳转（请检查账号密码）"


#  账户登录（含風控自動重試）
def login(sb) -> tuple[bool, Outcome, str]:
    """登入；TRANSIENT 類失敗（風控／站點未載入）喺同一 run 內自動重試。"""
    last = (False, Outcome.FAILED, "未知錯誤")
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        if attempt > 1:
            print(f"\n🔁 登入重試 {attempt}/{LOGIN_MAX_ATTEMPTS}"
                  f"（上一轉：{last[2]}）")
            time.sleep(LOGIN_RETRY_WAIT)
        ok, outcome, reason = _login_once(sb)
        if ok:
            if attempt > 1:
                print(f"✅ 第 {attempt} 次嘗試成功（風控重試生效）")
            return True, Outcome.SKIPPED, ""
        last = (ok, outcome, reason)
        if outcome is not Outcome.TRANSIENT:
            # 確定性失敗（例如服務端講明帳密唔啱），重試無意義
            return last
    print(f"\n⚠️ 已試 {LOGIN_MAX_ATTEMPTS} 次，最後一次：{last[2]}")
    return last


# ===== 自动续期流程 =====

def _read_alert(sb):
    """读取页面第一个 Bootstrap alert 的文本，找不到返回空串"""
    try:
        el = sb.find_element("div.alert", timeout=4)
        return (el.text or "").strip()
    except Exception:
        return ""


def _goto_server_detail(sb) -> tuple[bool, Outcome, str]:
    """在 Dashboard 首页查找并点击 See 进入服务器详情页。返回 (是否成功, 分类, 说明)。"""
    print("\n🖥️  正在进入服务器续期页...")
    time.sleep(5)

    # 页面顶部已有「还无法续期」全局提示 -> 未到窗口，不是错误
    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️  页面顶部提示: {alert_text}")
        return False, Outcome.SKIPPED, shorten(alert_text)

    # 多种选择器尝试查找 See 链接
    selectors = [
        'a[href*="/servers/edit?id="]',
        'td a[href*="/servers/edit"]',
        'table a[href*="/servers/edit"]',
        'table td a',
    ]

    see_link = None
    for sel in selectors:
        try:
            see_link = sb.find_element(sel, timeout=8)
            print(f"✅ 通过选择器找到链接: {sel}")
            break
        except Exception:
            continue

    # 选择器全部失败，尝试通过文本内容查找
    if see_link is None:
        print("⚠️ 选择器未命中，尝试文本匹配...")
        try:
            for a in sb.find_elements("a"):
                if (a.text or "").strip().lower() == "see":
                    see_link = a
                    print("✅ 通过文本 'See' 找到链接")
                    break
        except Exception:
            pass

    if see_link is None:
        # 打印调试信息帮助排查
        cur_url = sb.get_current_url()
        title = sb.get_title() or ""
        print(f"❌ 未找到 'See' 链接")
        print(f"当前 URL: {cur_url}")
        print(f"页面标题: {title}")
        try:
            links = sb.find_elements("a")
            print(f"     页面共 {len(links)} 个链接:")
            for a in links[:20]:
                href = a.get_attribute("href") or ""
                txt  = (a.text or "").strip()[:30]
                if href:
                    print(f"       - [{txt}] -> {href}")
        except Exception:
            pass
        sb.save_screenshot("servers_page_fail.png")
        return False, Outcome.FAILED, "未找到服务器条目或 See 链接"

    print("🖱️  点击 'See' 进入服务器详情页...")
    see_link.click()
    time.sleep(5)
    print(f"📄 当前页面: {sb.get_current_url()}")
    return True, Outcome.SKIPPED, ""


def _open_renew_modal(sb) -> bool:
    """滚动到 Renew 按钮并点击，打开模态框"""
    print("\n🔄 查找 Renew 按钮...")
    try:
        renew_btn = sb.find_element('button[data-bs-target="#renew-modal"]', timeout=10)
    except Exception:
        try:
            renew_btn = sb.find_element('button.btn.btn-outline-primary', timeout=5)
        except Exception:
            print("  ❌ 未找到 Renew 按钮")
            return False

    sb.execute_script("""
        (function(){
            var btn = document.querySelector('button[data-bs-target="#renew-modal"]')
                     || document.querySelector('button.btn.btn-outline-primary');
            if (btn) btn.scrollIntoView({behavior:'smooth',block:'center'});
        })()
    """)
    time.sleep(0.8)
    renew_btn.click()
    print("🖱️ 已点击 Renew 按钮，等待确认框...")
    time.sleep(3)

    try:
        sb.find_element('div.modal.show', timeout=5)
        print("✅ Renew 模态框已弹出")
        return True
    except Exception:
        print("⚠️ 模态框未弹出")
        return False


# def _solve_altcha(sb) -> bool:
#     """处理 ALTCHA 人机验证"""
#     print("\n🔐 处理 ALTCHA 人机验证...")
#     time.sleep(2)
#
#     # 先检查是否已自动通过
#     if sb.execute_script(_ALTCHA_SOLVED_JS):
#         print("✅ ALTCHA 已自动通过")
#         return True
#
#     # 展开模态框内 iframe 并获取坐标
#     coords = None
#     try:
#         coords = sb.execute_script(_ALTCHA_EXPAND_JS)
#     except Exception:
#         pass
#
#     if coords:
#         print(f"  📍 找到模态框内 iframe 坐标: ({coords['cx']}, {coords['cy']})")
#
#     # 最多尝试 3 轮
#     for attempt in range(3):
#         if sb.execute_script(_ALTCHA_SOLVED_JS):
#             print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
#             return True
#
#         # 策略 1: xdotool 物理点击 iframe 坐标
#         if coords:
#             try:
#                 wi = sb.execute_script(_WININFO_JS)
#             except Exception:
#                 wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}
#             bar = wi["oh"] - wi["ih"]
#             ax  = coords["cx"] + wi["sx"]
#             ay  = coords["cy"] + wi["sy"] + bar
#             print(f"🖱️  ALTCHA点击复选框  ({ax}, {ay})")
#             _xdotool_click(ax, ay)
#
#         # 策略 2: SeleniumBase 原生点击模态框内 iframe 元素
#         try:
#             iframes = sb.find_elements('div.modal.show iframe')
#             for iframe in iframes:
#                 try:
#                     iframe.click()
#                     print("🖱️  SeleniumBase 点击模态框 iframe")
#                 except Exception:
#                     pass
#         except Exception:
#             pass
#
#         # 策略 3: JS 遍历模态框内所有可点击元素
#         sb.execute_script("""
#             (function(){
#                 var modal = document.querySelector('div.modal.show');
#                 if (!modal) return;
#                 // 点击 iframe
#                 var iframes = modal.querySelectorAll('iframe');
#                 for (var i = 0; i < iframes.length; i++) {
#                     iframes[i].click();
#                     iframes[i].dispatchEvent(new MouseEvent('click', {bubbles:true}));
#                 }
#                 // 点击含 checkbox 的 label
#                 var labels = modal.querySelectorAll('label');
#                 for (var j = 0; j < labels.length; j++) {
#                     var txt = (labels[j].textContent || '').toLowerCase();
#                     if (txt.includes('robot') || txt.includes('captcha') || txt.includes('verify'))
#                         labels[j].click();
#                 }
#                 // 点击 checkbox
#                 var cbs = modal.querySelectorAll('input[type="checkbox"]');
#                 for (var k = 0; k < cbs.length; k++) {
#                     if (!cbs[k].disabled) {
#                         cbs[k].click();
#                         cbs[k].dispatchEvent(new MouseEvent('click', {bubbles:true}));
#                     }
#                 }
#             })()
#         """)
#
#         # 等待验证结果
#         for _ in range(6):
#             time.sleep(1)
#             if sb.execute_script(_ALTCHA_SOLVED_JS):
#                 print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮）")
#                 return True
#
#         print(f"  ⚠️ 第 {attempt + 1} 轮未通过，重试...")
#         # 重新获取坐标（iframe 可能已重新渲染）
#         try:
#             new_coords = sb.execute_script(_ALTCHA_EXPAND_JS)
#             if new_coords:
#                 coords = new_coords
#         except Exception:
#             pass
#
#     print("  ❌ ALTCHA 3 轮均失败")
#     return False


def _submit_renew(sb):
    """点击模态框内的 Renew 提交按钮"""
    print("🖱️  点击模态框中的 Renew 按钮...")
    try:
        submit = sb.find_element('div.modal-footer button.btn.btn-primary', timeout=10)
        submit.click()
    except Exception:
        sb.execute_script("""
            (function(){
                var m = document.querySelector('button.btn.btn-primary');
                if (!m) return;
                var bs = m.querySelectorAll('button');
                for (var i = 0; i < bs.length; i++)
                    if (/renew/i.test(bs[i].textContent)) bs[i].click();
            })()
        """)
    time.sleep(8)


def _check_renew_result(sb) -> tuple[Outcome, str]:
    """读页面 alert 提示，判断续期结果。返回 (Outcome, 说明)。"""
    print("\n📋 检查续期结果...")
    alert_text = _read_alert(sb)
    if not alert_text:
        time.sleep(3)
        alert_text = _read_alert(sb)

    if not alert_text:
        print("ℹ️ 未检测到明确的提示框，可能续期操作未生效")
        return Outcome.UNKNOWN, "未检测到明确提示，请留意下次运行"

    print(f"📩 页面提示: {alert_text}")
    low = alert_text.lower()
    if "can't renew" in low or "unable" in low:
        return Outcome.SKIPPED, shorten(alert_text)
    if any(kw in low for kw in ("renewed", "success", "extended")):
        return Outcome.RENEWED, shorten(alert_text)
    return Outcome.UNKNOWN, shorten(alert_text)


def renew_server(sb) -> tuple[Outcome, str]:
    """登录成功后调用：自动进入详情页 -> Renew -> 提交 -> 读结果。"""
    print("\n" + "#" * 25)
    print("  开始自动续期流程")
    print("#" * 25)

    ok, outcome, detail = _goto_server_detail(sb)
    if not ok:
        return outcome, detail

    if not _open_renew_modal(sb):
        return Outcome.UNKNOWN, "Renew 按钮或确认框未出现"

    _submit_renew(sb)
    return _check_renew_result(sb)


#  脚本执行入口 (可选代理)
def main() -> int:
    print("#" * 25)
    print("   katabump 自动登录续期")
    print("#" * 25)

    report = RenewReport(SERVICE)
    account = masked_account()

    IS_PROXY = env.get("IS_PROXY", "false").lower() == "true"
    proxy_str = env.get("PROXY_SERVER", "").strip()
    sb_kwargs = {"uc": True, "headless": False}

    if IS_PROXY and proxy_str:
        print(f"🔗 挂载代理: {proxy_str}")
        sb_kwargs["proxy"] = proxy_str
    elif IS_PROXY:
        print("⚠️ IS_PROXY=true 但 PROXY_SERVER 未设置，回退直连")
    else:
        print("🌐 未使用代理，直连访问")

    print("🚀 启动浏览器...")
    try:
        with SB(**sb_kwargs) as sb:
            try:
                sb.open("https://api.ip.sb/ip")
                print(f"📍  当前出口IP: {sb.get_text('body')}")
            except Exception:
                pass

            ok, outcome, reason = login(sb)
            if not ok:
                print(f"\n❌ 登录未成功：{reason}")
                report.add(account, outcome, detail=reason)
            else:
                outcome, detail = renew_server(sb)
                report.add(account, outcome, detail=detail)
    except Exception as exc:
        # 浏览器 / 驱动层面的异常：多为环境或上游问题，按上游故障处理
        report.add(account, Outcome.TRANSIENT, detail=shorten(str(exc), 90))

    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
