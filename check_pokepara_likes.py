import os
import re
import sys
import uuid
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# 環境変数（GitHub Secretsから安全に読み込み）
POKEPARA_ID = os.environ.get("POKEPARA_ID", "").strip()
POKEPARA_PASSWORD = os.environ.get("POKEPARA_PASSWORD", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_TO_ID = os.environ.get("LINE_TO_ID", "").strip()

LOGIN_URL = "https://www.pokepara.jp/kyushu/login/login_shopc.aspx"
DEFAULT_HOKKAIDO_BLOG_URL = "https://www.pokepara.jp/_hokkaido/m801/a1801/shop6032/gal/783239/blog/diary_45193448.html"

TARGET_SHOPS = [
    "エンジェル女学園 3年2組",
    "天使と悪魔",
    "乙女坂",
]

JST = timezone(timedelta(hours=9))


def send_line_message(text: str) -> None:
    """LINE Messaging API (Push Message) で結果を通知する"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TO_ID:
        print("LINE認証情報が未設定のため標準出力のみ行います:\n", text, flush=True)
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "X-Line-Retry-Key": str(uuid.uuid4()),
    }
    payload = {
        "to": LINE_TO_ID,
        "messages": [{"type": "text", "text": text[:4999]}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"LINE送信エラー ({resp.status_code}): {resp.text}", file=sys.stderr, flush=True)
    else:
        print("LINE通知を正常に送信しました。", flush=True)


def login_to_pokepara(page) -> dict:
    """ポケパラ九州版の店舗ログイン画面でログインする"""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    filled = page.evaluate(
        """({ loginId, password }) => {
        const passInput = document.querySelector("input[type='password']");
        if (!passInput) {
            return {
                ok: false,
                reason: "password_input_not_found",
                title: document.title,
                bodySnippet: (document.body ? document.body.innerText : "").slice(0, 200)
            };
        }

        const form = passInput.closest("form") || document.body;
        const inputs = Array.from(form.querySelectorAll("input"));
        const passIdx = inputs.indexOf(passInput);

        let idInput = null;
        for (let i = passIdx - 1; i >= 0; i--) {
            const inp = inputs[i];
            const t = (inp.type || "text").toLowerCase();
            if (["text", "tel", "email", "number"].includes(t)) {
                idInput = inp;
                break;
            }
        }
        if (!idInput) return { ok: false, reason: "id_input_not_found" };

        idInput.focus();
        idInput.value = loginId;
        idInput.dispatchEvent(new Event("input", { bubbles: true }));
        idInput.dispatchEvent(new Event("change", { bubbles: true }));

        passInput.focus();
        passInput.value = password;
        passInput.dispatchEvent(new Event("input", { bubbles: true }));
        passInput.dispatchEvent(new Event("change", { bubbles: true }));

        let submitBtn = null;
        for (let i = passIdx + 1; i < inputs.length; i++) {
            const inp = inputs[i];
            const t = (inp.type || "").toLowerCase();
            if (["submit", "image", "button"].includes(t)) {
                submitBtn = inp;
                break;
            }
        }
        if (!submitBtn) {
            submitBtn = form.querySelector("button[type='submit'], input[type='submit'], input[type='image'], button");
        }
        if (submitBtn) {
            submitBtn.click();
            return { ok: true, idName: idInput.name || idInput.id, passName: passInput.name || passInput.id };
        } else {
            form.submit();
            return { ok: true, submittedForm: true };
        }
    }""",
        {"loginId": POKEPARA_ID, "password": POKEPARA_PASSWORD},
    )
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    filled["afterUrl"] = page.url
    filled["afterTitle"] = page.title()
    print(f"ログイン結果: {filled}", flush=True)
    return filled


def resolve_hokkaido_blog_url(page) -> str:
    page.goto(DEFAULT_HOKKAIDO_BLOG_URL, wait_until="domcontentloaded", timeout=30000)
    body_text = page.locator("body").inner_text()
    if "本日の残り" in body_text:
        return DEFAULT_HOKKAIDO_BLOG_URL

    page.goto("https://www.pokepara.jp/_hokkaido/", wait_until="domcontentloaded", timeout=30000)
    blog_links = page.locator("a[href*='/blog/diary_']")
    if blog_links.count() > 0:
        href = blog_links.first.get_attribute("href")
        if href:
            return href if href.startswith("http") else f"https://www.pokepara.jp{href}"
    return DEFAULT_HOKKAIDO_BLOG_URL


def extract_remaining_likes(page, blog_url: str) -> int:
    page.goto(blog_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    match = re.search(r"本日の残り(?:イイネ|いいね)\s*[:：]?\s*(\d+)", text)
    if match:
        return int(match.group(1))
    return -1


def switch_shop_if_needed(page, target_shop_keyword: str) -> None:
    switched_via_select = page.evaluate(
        """(keyword) => {
        const selects = Array.from(document.querySelectorAll("select"));
        for (const sel of selects) {
            const options = Array.from(sel.options);
            const targetOpt = options.find(o => o.textContent.includes(keyword));
            if (targetOpt) {
                if (sel.value === targetOpt.value) {
                    return { status: "already_selected" };
                }
                sel.value = targetOpt.value;
                sel.dispatchEvent(new Event("change", { bubbles: true }));
                return { status: "changed_select" };
            }
        }
        return { status: "no_select_found" };
    }""",
        target_shop_keyword,
    )
    if switched_via_select.get("status") == "changed_select":
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        return
    if switched_via_select.get("status") == "already_selected":
        return

    other_shop_btn = page.get_by_text("他のお店", exact=False)
    if other_shop_btn.count() > 0:
        other_shop_btn.first.click()
        page.wait_for_timeout(800)

    shop_link = page.locator("a", has_text=target_shop_keyword)
    if shop_link.count() > 0:
        shop_link.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)


def navigate_to_cast_list(page) -> str:
    for label in ["女の子一覧", "女の子管理"]:
        links = page.locator("a", has_text=label)
        if links.count() > 0:
            links.first.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

    gal_list = page.locator("a", has_text="女の子一覧")
    if gal_list.count() > 0:
        gal_list.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

    return page.url


def get_active_casts_and_proxy_urls(page) -> tuple[list[dict], str, dict]:
    casts_list_url = navigate_to_cast_list(page)

    page.evaluate(
        """() => {
        const triggers = Array.from(document.querySelectorAll("a, span, div, button")).filter(
            el => el.textContent && el.textContent.includes("その他の項目")
        );
        triggers.forEach(t => { try { t.click(); } catch (e) {} });
    }"""
    )
    page.wait_for_timeout(500)

    info = page.evaluate(
        """() => {
        const allProxyLinks = Array.from(document.querySelectorAll("a")).filter(
            a => a.textContent && a.textContent.includes("代理ログイン")
        );

        const activeResults = [];
        const skippedGray = [];

        for (const link of allProxyLinks) {
            let card = link.parentElement;
            while (card && card.parentElement && card.parentElement !== document.body) {
                const parentProxyCount = Array.from(card.parentElement.querySelectorAll("a")).filter(
                    a => a.textContent && a.textContent.includes("代理ログイン")
                ).length;
                if (parentProxyCount > 1) {
                    break;
                }
                card = card.parentElement;
            }

            let curr = link;
            let isGray = false;
            while (curr) {
                const style = window.getComputedStyle(curr);
                const bg = style.backgroundColor || "";
                const cls = (curr.className || "").toString().toLowerCase();
                const inlineStyle = (curr.getAttribute("style") || "").toLowerCase();

                if (
                    cls.includes("gray") ||
                    cls.includes("grey") ||
                    cls.includes("disable") ||
                    cls.includes("retire") ||
                    cls.includes("hide") ||
                    cls.includes("off") ||
                    inlineStyle.includes("#ccc") ||
                    inlineStyle.includes("#ddd") ||
                    inlineStyle.includes("#eee") ||
                    inlineStyle.includes("gray") ||
                    inlineStyle.includes("grey")
                ) {
                    isGray = true;
                }

                const rgbMatch = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                if (rgbMatch) {
                    const [_, r, g, b] = rgbMatch.map(Number);
                    if (Math.abs(r - g) <= 5 && Math.abs(g - b) <= 5 && r >= 150 && r <= 245) {
                        isGray = true;
                    }
                }
                if (curr === card) break;
                curr = curr.parentElement;
            }

            let castName = "";
            if (card) {
                const ignoreWords = [
                    "その他の項目", "代理ログイン", "ブログ", "写メ", "スケジュール",
                    "出勤", "プロフィール", "編集", "削除", "公開", "非公開", "順番",
                    "移動", "コピー", "確認", "設定", "管理", "一覧"
                ];
                const candidates = Array.from(card.querySelectorAll("strong, b, h3, h4, .name, .gal_name, a, span, div, p, td"));
                for (const el of candidates) {
                    if (el.children.length > 2) continue;
                    const txt = (el.textContent || "").trim().replace(/\\s+/g, " ");
                    if (!txt || txt.length > 25) continue;
                    if (ignoreWords.some(w => txt.includes(w))) continue;
                    if (/^[0-9/:\\s\\-\\.]+$/.test(txt)) continue;
                    castName = txt;
                    break;
                }
            }

            const item = {
                name: castName || "キャスト",
                href: link.href
            };
            if (isGray) {
                skippedGray.push(item.name);
            } else {
                activeResults.push(item);
            }
        }

        const sampleLinks = Array.from(document.querySelectorAll("a"))
            .map(a => (a.textContent || "").trim())
            .filter(Boolean)
            .slice(0, 15);

        return {
            url: location.href,
            title: document.title,
            totalProxyLinks: allProxyLinks.length,
            activeCount: activeResults.length,
            skippedGray: skippedGray,
            activeResults: activeResults,
            sampleLinks: sampleLinks,
            bodySnippet: (document.body ? document.body.innerText : "").replace(/\\s+/g, " ").slice(0, 150)
        };
    }"""
    )
    return info["activeResults"], casts_list_url, info


def main():
    if not POKEPARA_ID or not POKEPARA_PASSWORD:
        print("エラー: POKEPARA_ID または POKEPARA_PASSWORD が設定されていません。", file=sys.stderr, flush=True)
        sys.exit(1)

    now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    report_lines = [f"【ポケパラ 100いいね未完了チェック v2】\n確認日時: {now_str}"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="ja-JP",
        )
        page = context.new_page()

        login_info = login_to_pokepara(page)
        admin_home_url = page.url
        blog_url = None

        for shop_name in TARGET_SHOPS:
            page.goto(admin_home_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)
            switch_shop_if_needed(page, shop_name)
            admin_home_url = page.url

            active_casts, casts_list_url, debug_info = get_active_casts_and_proxy_urls(page)
            if not active_casts:
                report_lines.append(
                    f"\n■ {shop_name}\n⚠️ 在籍キャスト取得0名\n"
                    f"・URL: {debug_info.get('url')}\n"
                    f"・Title: {debug_info.get('title')}\n"
                    f"・代理ログイン検出数: {debug_info.get('totalProxyLinks')} (グレースキップ: {len(debug_info.get('skippedGray', []))})\n"
                    f"・画面抜粋: {debug_info.get('bodySnippet')}"
                )
                continue

            remaining_casts = []
            completed_count = 0

            for cast in active_casts:
                page.goto(cast["href"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)

                if cast["name"] == "キャスト":
                    mypage_text = page.locator("body").inner_text()
                    m_name = re.search(r"([^\s\n]+)\s*さん", mypage_text)
                    if m_name:
                        cast["name"] = m_name.group(1)

                if blog_url is None:
                    blog_url = resolve_hokkaido_blog_url(page)

                remaining = extract_remaining_likes(page, blog_url)
                if remaining == 0:
                    completed_count += 1
                elif remaining > 0:
                    remaining_casts.append(f"・{cast['name']}：残り {remaining}")
                else:
                    remaining_casts.append(f"・{cast['name']}：取得失敗（要確認）")

                page.goto(casts_list_url, wait_until="domcontentloaded", timeout=30000)

            report_lines.append(f"\n■ {shop_name}（在籍{len(active_casts)}名 / 完了{completed_count}名）")
            if remaining_casts:
                report_lines.extend(remaining_casts)
            else:
                report_lines.append("🎉 全員100いいね完了（残り0）！")

        browser.close()

    final_message = "\n".join(report_lines)
    print("\n--- 最終レポート ---\n" + final_message, flush=True)
    send_line_message(final_message)


if __name__ == "__main__":
    main()
