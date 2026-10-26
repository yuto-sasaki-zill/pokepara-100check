import os
import re
import sys
import uuid
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# 環境変数（GitHub Secretsから安全に読み込み）
POKEPARA_ID = os.environ.get("POKEPARA_ID", "")
POKEPARA_PASSWORD = os.environ.get("POKEPARA_PASSWORD", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO_ID = os.environ.get("LINE_TO_ID", "")  # 個人User ID (U...) または グループID (C...)

LOGIN_URL = "https://www.pokepara.jp/kyushu/login/login_shopc.aspx"
DEFAULT_HOKKAIDO_BLOG_URL = "https://www.pokepara.jp/_hokkaido/m801/a1801/shop6032/gal/783239/blog/diary_45193448.html"

# 対象店舗リスト（管理画面の店舗切替ドロップダウン表示名に部分一致）
TARGET_SHOPS = [
    "エンジェル女学園 3年2組",
    "天使と悪魔",
    "乙女坂",
]

JST = timezone(timedelta(hours=9))


def send_line_message(text: str) -> None:
    """LINE Messaging API (Push Message) で結果を通知する"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TO_ID:
        print("LINE認証情報が未設定のため標準出力のみ行います:\n", text)
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
        print(f"LINE送信エラー ({resp.status_code}): {resp.text}", file=sys.stderr)
    else:
        print("LINE通知を正常に送信しました。")


def resolve_hokkaido_blog_url(page) -> str:
    """確認用の北海道ブログ記事URLを取得する（既存URLが有効ならそのまま使用、消えていれば新規取得）"""
    page.goto(DEFAULT_HOKKAIDO_BLOG_URL, wait_until="domcontentloaded", timeout=30000)
    body_text = page.locator("body").inner_text()
    if "本日の残り" in body_text:
        return DEFAULT_HOKKAIDO_BLOG_URL

    # 万が一該当ブログ記事が削除されていた場合は北海道トップから適当なブログを1件取得
    page.goto("https://www.pokepara.jp/_hokkaido/", wait_until="domcontentloaded", timeout=30000)
    blog_links = page.locator("a[href*='/blog/diary_']")
    if blog_links.count() > 0:
        href = blog_links.first.get_attribute("href")
        if href:
            return href if href.startswith("http") else f"https://www.pokepara.jp{href}"
    return DEFAULT_HOKKAIDO_BLOG_URL


def extract_remaining_likes(page, blog_url: str) -> int:
    """ブログ記事ページから『本日の残りイイネ』の数値を抽出する"""
    page.goto(blog_url, wait_until="domcontentloaded", timeout=30000)
    text = page.locator("body").inner_text()
    match = re.search(r"本日の残り(?:イイネ|いいね)\s*[:：]?\s*(\d+)", text)
    if match:
        return int(match.group(1))
    return -1


def switch_shop_if_needed(page, target_shop_keyword: str) -> None:
    """管理画面左上の『他のお店でログインする』から対象店舗へ切り替える"""
    selects = page.locator("select")
    for i in range(selects.count()):
        sel = selects.nth(i)
        options_text = sel.inner_text()
        if "天使と悪魔" in options_text or "乙女坂" in options_text or "エンジェル女学園" in options_text:
            options = sel.locator("option")
            for j in range(options.count()):
                opt_label = options.nth(j).inner_text().strip()
                if target_shop_keyword in opt_label:
                    val = options.nth(j).get_attribute("value")
                    if val:
                        sel.select_option(value=val)
                        page.wait_for_load_state("domcontentloaded")
                    return

    other_shop_btn = page.get_by_text("他のお店", exact=False)
    if other_shop_btn.count() > 0:
        other_shop_btn.first.click()
        page.wait_for_timeout(500)
        shop_link = page.locator("a", has_text=target_shop_keyword)
        if shop_link.count() > 0:
            shop_link.first.click()
            page.wait_for_load_state("domcontentloaded")


def get_active_casts_and_proxy_urls(page) -> tuple[list[dict], str]:
    """『女の子一覧』ページから、背景がグレーでない在籍キャスト名と代理ログインURLを取得する"""
    gal_manage = page.locator("a", has_text="女の子管理")
    if gal_manage.count() > 0:
        gal_manage.first.click()
        page.wait_for_load_state("domcontentloaded")

    gal_list = page.locator("a", has_text="女の子一覧")
    if gal_list.count() > 0:
        gal_list.first.click()
        page.wait_for_load_state("domcontentloaded")

    casts_list_url = page.url

    active_casts = page.evaluate(
        """() => {
        const results = [];
        const proxyLinks = Array.from(document.querySelectorAll('a')).filter(a => a.textContent.includes('代理ログイン'));
        for (const link of proxyLinks) {
            let container = link.parentElement;
            let isGray = false;
            let castName = '';
            for (let depth = 0; depth < 8 && container; depth++) {
                const bg = window.getComputedStyle(container).backgroundColor;
                const cls = (container.className || '').toLowerCase();
                if (cls.includes('gray') || cls.includes('disable') || cls.includes('off') || cls.includes('hide')) {
                    isGray = true;
                }
                const rgbMatch = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                if (rgbMatch) {
                    const [_, r, g, b] = rgbMatch.map(Number);
                    if (r === g && g === b && r >= 180 && r <= 242) {
                        isGray = true;
                    }
                }
                container = container.parentElement;
            }
            let card = link.closest('li, tr, .gal_box, .cast_box, .box, div');
            if (card) {
                const nameEl = card.querySelector('.name, .gal_name, strong, b, h3, h4, a');
                if (nameEl) {
                    castName = nameEl.textContent.trim();
                }
            }
            if (!isGray) {
                results.push({
                    name: castName || '名称未取得',
                    href: link.href
                });
            }
        }
        return results;
    }"""
    )
    return active_casts, casts_list_url


def main():
    if not POKEPARA_ID or not POKEPARA_PASSWORD:
        print("エラー: POKEPARA_ID または POKEPARA_PASSWORD が設定されていません。", file=sys.stderr)
        sys.exit(1)

    now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    report_lines = [f"【ポケパラ 100いいね未完了チェック】\n確認日時: {now_str}"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # 1. ポケパラ九州版 店舗ログイン
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.locator("input[type='text']").first.fill(POKEPARA_ID)
        page.locator("input[type='password']").first.fill(POKEPARA_PASSWORD)
        submit_btn = page.locator("input[type='submit'], button[type='submit'], input[type='image']").first
        submit_btn.click()
        page.wait_for_load_state("domcontentloaded")

        admin_home_url = page.url
        blog_url = None

        # 2. 各店舗を順番に確認
        for shop_name in TARGET_SHOPS:
            page.goto(admin_home_url, wait_until="domcontentloaded", timeout=30000)
            switch_shop_if_needed(page, shop_name)
            admin_home_url = page.url

            active_casts, casts_list_url = get_active_casts_and_proxy_urls(page)
            remaining_casts = []
            completed_count = 0

            for cast in active_casts:
                page.goto(cast["href"], wait_until="domcontentloaded", timeout=30000)

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
    print(final_message)
    send_line_message(final_message)


if __name__ == "__main__":
    main()
