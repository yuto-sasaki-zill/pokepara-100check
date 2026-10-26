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


def login_to_pokepara(page) -> bool:
    """ポケパラ九州版の店舗ログイン画面でログインする（ヘッダー検索窓を誤入力しないようパスワード欄基準で特定）"""
    print(f"ログイン画面へアクセス中: {LOGIN_URL}", flush=True)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)

    # パスワード入力欄を含むフォーム、またはパスワード欄の直前のテキスト入力欄を特定
    filled = page.evaluate(
        """({ loginId, password }) => {
        const passInput = document.querySelector("input[type='password']");
        if (!passInput) return { ok: false, reason: "password input not found" };

        const form = passInput.closest("form") || document.body;
        const inputs = Array.from(form.querySelectorAll("input"));
        const passIdx = inputs.indexOf(passInput);

        // パスワード欄より前にあるテキスト系inputを逆順に探す
        let idInput = null;
        for (let i = passIdx - 1; i >= 0; i--) {
            const inp = inputs[i];
            const t = (inp.type || "text").toLowerCase();
            if (["text", "tel", "email", "number"].includes(t) && inp.offsetParent !== null) {
                idInput = inp;
                break;
            }
        }
        if (!idInput) return { ok: false, reason: "id input not found before password" };

        idInput.focus();
        idInput.value = loginId;
        idInput.dispatchEvent(new Event("input", { bubbles: true }));
        idInput.dispatchEvent(new Event("change", { bubbles: true }));

        passInput.focus();
        passInput.value = password;
        passInput.dispatchEvent(new Event("input", { bubbles: true }));
        passInput.dispatchEvent(new Event("change", { bubbles: true }));

        // パスワード欄より後にある送信ボタンを探す
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
            return { ok: true, idName: idInput.name || idInput.id, passName: passInput.name || passInput.id, submittedForm: true };
        }
    }""",
        {"loginId": POKEPARA_ID, "password": POKEPARA_PASSWORD},
    )
    print(f"ログインフォーム入力結果: {filled}", flush=True)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    print(f"ログイン後のURL: {page.url} / タイトル: {page.title()}", flush=True)
    return "login_shopc" not in page.url.lower()


def resolve_hokkaido_blog_url(page) -> str:
    """確認用の北海道ブログ記事URLを取得する"""
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
    """ブログ記事ページから『本日の残りイイネ』の数値を抽出する"""
    page.goto(blog_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    match = re.search(r"本日の残り(?:イイネ|いいね)\s*[:：]?\s*(\d+)", text)
    if match:
        return int(match.group(1))
    return -1


def switch_shop_if_needed(page, target_shop_keyword: str) -> None:
    """管理画面左上の『他のお店でログインする』から対象店舗へ切り替える"""
    print(f"店舗切り替え確認中: {target_shop_keyword}", flush=True)
    # 1. <select> ドロップダウンがある場合
    switched_via_select = page.evaluate(
        """(keyword) => {
        const selects = Array.from(document.querySelectorAll("select"));
        for (const sel of selects) {
            const options = Array.from(sel.options);
            const targetOpt = options.find(o => o.textContent.includes(keyword));
            if (targetOpt) {
                if (sel.value === targetOpt.value) {
                    return { status: "already_selected", text: targetOpt.textContent.trim() };
                }
                sel.value = targetOpt.value;
                sel.dispatchEvent(new Event("change", { bubbles: true }));
                return { status: "changed_select", text: targetOpt.textContent.trim() };
            }
        }
        return { status: "no_select_found" };
    }""",
        target_shop_keyword,
    )
    print(f"セレクトボックス切替結果: {switched_via_select}", flush=True)
    if switched_via_select.get("status") == "changed_select":
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        return
    if switched_via_select.get("status") == "already_selected":
        return

    # 2. 『他のお店でログインする』クリック展開型の場合
    other_shop_btn = page.get_by_text("他のお店", exact=False)
    if other_shop_btn.count() > 0:
        other_shop_btn.first.click()
        page.wait_for_timeout(800)

    shop_link = page.locator("a, option, li, div", has_text=target_shop_keyword)
    for i in range(shop_link.count()):
        el = shop_link.nth(i)
        tag = el.evaluate("el => el.tagName.toLowerCase()")
        if tag == "a":
            el.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            print(f"リンククリックで店舗切替完了: {page.url}", flush=True)
            return


def navigate_to_cast_list(page) -> str:
    """『女の子管理』＞『女の子一覧』ページへ遷移する"""
    for label in ["女の子一覧", "女の子管理"]:
        links = page.locator("a", has_text=label)
        if links.count() > 0:
            href = links.first.get_attribute("href")
            print(f"メニュー『{label}』を検出 (href={href})、遷移します", flush=True)
            links.first.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

    # もし遷移後にまだ『女の子一覧』リンクがあればクリック
    gal_list = page.locator("a", has_text="女の子一覧")
    if gal_list.count() > 0:
        gal_list.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

    print(f"女の子一覧ページURL: {page.url}", flush=True)
    return page.url


def get_active_casts_and_proxy_urls(page) -> tuple[list[dict], str]:
    """『女の子一覧』ページから、背景がグレーでない在籍キャスト名と代理ログインURLを取得する"""
    casts_list_url = navigate_to_cast_list(page)

    # 『その他の項目»』をすべて展開して代理ログインリンクを確実に表示させる
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
            // このリンク1つだけを含む最上位のキャストカード要素（個別枠）を特定する
            let card = link.parentElement;
            while (card && card.parentElement && card.parentElement !== document.body) {
                const parentProxyCount = Array.from(card.parentElement.querySelectorAll("a")).filter(
                    a => a.textContent && a.textContent.includes("代理ログイン")
                ).length;
                if (parentProxyCount > 1) {
                    break; // 親要素が複数キャストを含む一覧コンテナに達したらストップ
                }
                card = card.parentElement;
            }

            // link から card までの要素のみを対象にグレー背景（非在籍）かどうかを判定
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
                    // 白(255,255,255)ではなく、グレー系(R=G=Bかつ150〜245)の背景色がカード自体についている場合
                    if (Math.abs(r - g) <= 5 && Math.abs(g - b) <= 5 && r >= 150 && r <= 245) {
                        isGray = true;
                    }
                }
                if (curr === card) break;
                curr = curr.parentElement;
            }

            // キャスト名の抽出（カード内の不要なボタン文字列を除いた先頭テキストまたは見出し）
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
        return {
            totalProxyLinks: allProxyLinks.length,
            activeCount: activeResults.length,
            skippedGray: skippedGray,
            activeResults: activeResults
        };
    }"""
    )
    print(
        f"代理ログイン総数: {info['totalProxyLinks']} / 在籍: {info['activeCount']} / スキップ(グレー): {len(info['skippedGray'])} ({info['skippedGray']})",
        flush=True,
    )
    return info["activeResults"], casts_list_url


def main():
    if not POKEPARA_ID or not POKEPARA_PASSWORD:
        print("エラー: POKEPARA_ID または POKEPARA_PASSWORD が設定されていません。", file=sys.stderr, flush=True)
        sys.exit(1)

    now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    report_lines = [f"【ポケパラ 100いいね未完了チェック】\n確認日時: {now_str}"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # 1. ログイン
        login_ok = login_to_pokepara(page)
        if not login_ok:
            print("警告: ログイン画面から遷移していない可能性があります。URL:", page.url, flush=True)

        admin_home_url = page.url
        blog_url = None

        # 2. 各店舗を順番に確認
        for shop_name in TARGET_SHOPS:
            page.goto(admin_home_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)
            switch_shop_if_needed(page, shop_name)
            admin_home_url = page.url

            active_casts, casts_list_url = get_active_casts_and_proxy_urls(page)
            if not active_casts:
                report_lines.append(f"\n■ {shop_name}\n⚠️ 在籍キャスト一覧を取得できませんでした（0名）")
                continue

            remaining_casts = []
            completed_count = 0

            for cast in active_casts:
                page.goto(cast["href"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)

                # 代理ログイン後のマイページ上にキャスト名があれば補完
                if cast["name"] == "キャスト":
                    mypage_text = page.locator("body").inner_text()
                    m_name = re.search(r"([^\s\n]+)\s*さん", mypage_text)
                    if m_name:
                        cast["name"] = m_name.group(1)

                if blog_url is None:
                    blog_url = resolve_hokkaido_blog_url(page)

                remaining = extract_remaining_likes(page, blog_url)
                print(f"[{shop_name}] {cast['name']} -> 残りイイネ: {remaining}", flush=True)

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
