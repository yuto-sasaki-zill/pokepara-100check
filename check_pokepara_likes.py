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


def clean_cast_name(name: str) -> str:
    """キャスト名から絵文字・記号・異体字セレクタ等をすべて除去する"""
    cleaned = re.sub(
        r"[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBFー々a-zA-Z0-9ａ-ｚＡ-Ｚ０-９]",
        "",
        name,
    )
    return cleaned.strip() or name.strip()


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
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"LINE送信エラー ({resp.status_code}): {resp.text}", file=sys.stderr, flush=True)
        else:
            print("LINE通知を正常に送信しました。", flush=True)
    except Exception as e:
        print(f"LINE送信例外エラー: {e}", file=sys.stderr, flush=True)


def safe_goto(page, url: str, timeout: int = 25000) -> bool:
    """通信遅延でも止まらない安全なページ遷移（リトライ付き）"""
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as e:
            print(f"ページ遷移リトライ ({attempt + 1}/2) URL={url}: {e}", flush=True)
            page.wait_for_timeout(1000)
    return False


def login_to_pokepara(page) -> dict:
    """ポケパラ九州版の店舗ログイン画面でログインする"""
    safe_goto(page, LOGIN_URL, timeout=30000)
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
    try:
        page.wait_for_load_state("domcontentloaded", timeout=25000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    filled["afterUrl"] = page.url
    filled["afterTitle"] = page.title()
    print(f"ログイン結果: {filled}", flush=True)
    return filled


def resolve_hokkaido_blog_url(page) -> str:
    if safe_goto(page, DEFAULT_HOKKAIDO_BLOG_URL, timeout=20000):
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
            if "本日の残り" in body_text or "上限超過" in body_text:
                return DEFAULT_HOKKAIDO_BLOG_URL
        except Exception:
            pass

    if safe_goto(page, "https://www.pokepara.jp/_hokkaido/", timeout=20000):
        try:
            href = page.evaluate(
                """() => {
                const a = document.querySelector("a[href*='/blog/diary_']");
                return a ? a.href : "";
            }"""
            )
            if href:
                return href
        except Exception:
            pass
    return DEFAULT_HOKKAIDO_BLOG_URL


def extract_remaining_likes(page, blog_url: str) -> int:
    """ブログ記事ページから『本日の残りイイネ』の数値を抽出する（『イイネ上限超過中。』は残り0＝完了として扱う）"""
    if not safe_goto(page, blog_url, timeout=20000):
        return -1
    page.wait_for_timeout(300)
    try:
        text = page.locator("body").inner_text(timeout=5000)
        if "上限超過" in text:
            return 0
        match = re.search(r"本日の残り(?:イイネ|いいね)\s*[:：]?\s*(\d+)", text)
        if match:
            return int(match.group(1))
    except Exception as e:
        print(f"残りイイネ抽出エラー: {e}", flush=True)
    return -1


def switch_shop_if_needed(page, target_shop_keyword: str) -> None:
    """非表示要素のクリック待機エラーを防ぐため、すべてJavaScriptまたはURL直接遷移で店舗を切り替える"""
    try:
        res = page.evaluate(
            """(keyword) => {
            const selects = Array.from(document.querySelectorAll("select"));
            for (const sel of selects) {
                const options = Array.from(sel.options);
                const targetOpt = options.find(o => o.textContent && o.textContent.includes(keyword));
                if (targetOpt) {
                    if (sel.value === targetOpt.value) {
                        return { status: "already_selected", text: targetOpt.textContent.trim() };
                    }
                    sel.value = targetOpt.value;
                    sel.dispatchEvent(new Event("change", { bubbles: true }));
                    if (sel.onchange) {
                        try { sel.onchange(); } catch (e) {}
                    }
                    return { status: "changed_select", text: targetOpt.textContent.trim() };
                }
            }

            const links = Array.from(document.querySelectorAll("a"));
            const shopLink = links.find(a => a.textContent && a.textContent.includes(keyword));
            if (shopLink) {
                if (shopLink.href && !shopLink.href.toLowerCase().startsWith("javascript:")) {
                    return { status: "goto_url", url: shopLink.href, text: shopLink.textContent.trim() };
                }
                shopLink.click();
                return { status: "js_clicked", text: shopLink.textContent.trim() };
            }
            return { status: "not_found" };
        }""",
            target_shop_keyword,
        )
        print(f"店舗切替 ({target_shop_keyword}): {res}", flush=True)
        if res.get("status") == "goto_url" and res.get("url"):
            safe_goto(page, res["url"], timeout=20000)
            page.wait_for_timeout(1200)
        elif res.get("status") in ("changed_select", "js_clicked"):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
    except Exception as e:
        print(f"店舗切替警告 ({target_shop_keyword}): {e}", flush=True)


def navigate_to_cast_list(page) -> str:
    """href を直接取得して『女の子一覧』へ遷移する"""
    for label in ["女の子一覧", "女の子管理"]:
        try:
            res = page.evaluate(
                """(targetLabel) => {
                const links = Array.from(document.querySelectorAll("a")).filter(
                    a => a.textContent && a.textContent.includes(targetLabel)
                );
                if (links.length === 0) return { found: false };
                const urlLink = links.find(a => a.href && !a.href.toLowerCase().startsWith("javascript:") && !a.getAttribute("href")?.startsWith("#"));
                if (urlLink) {
                    return { found: true, type: "url", href: urlLink.href };
                }
                links[0].click();
                return { found: true, type: "js_click" };
            }""",
                label,
            )
            print(f"メニュー検索 ({label}): {res}", flush=True)
            if res.get("found"):
                if res.get("type") == "url" and res.get("href"):
                    safe_goto(page, res["href"], timeout=20000)
                    page.wait_for_timeout(1000)
                else:
                    page.wait_for_timeout(1500)
                has_proxy = page.evaluate(
                    """() => Array.from(document.querySelectorAll("a")).some(a => a.textContent && a.textContent.includes("代理ログイン"))"""
                )
                if has_proxy:
                    break
        except Exception as e:
            print(f"メニュー遷移警告 ({label}): {e}", flush=True)

    return page.url


def get_active_casts_and_proxy_urls(page) -> tuple[list[dict], str, dict]:
    casts_list_url = navigate_to_cast_list(page)

    try:
        page.evaluate(
            """() => {
            const triggers = Array.from(document.querySelectorAll("a, span, div, button")).filter(
                el => el.textContent && el.textContent.includes("その他の項目")
            );
            triggers.forEach(t => { try { t.click(); } catch (e) {} });
        }"""
        )
        page.wait_for_timeout(500)
    except Exception:
        pass

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

        return {
            url: location.href,
            title: document.title,
            totalProxyLinks: allProxyLinks.length,
            activeCount: activeResults.length,
            skippedGray: skippedGray,
            activeResults: activeResults,
            bodySnippet: (document.body ? document.body.innerText : "").replace(/\\s+/g, " ").slice(0, 150)
        };
    }"""
    )
    print(
        f"一覧取得: URL={info['url']} / 総数={info['totalProxyLinks']} / 在籍={info['activeCount']} / スキップ={len(info['skippedGray'])}",
        flush=True,
    )
    return info["activeResults"], casts_list_url, info


def main():
    if not POKEPARA_ID or not POKEPARA_PASSWORD:
        print("エラー: POKEPARA_ID または POKEPARA_PASSWORD が設定されていません。", file=sys.stderr, flush=True)
        sys.exit(1)

    now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    report_lines = [f"【ポケパラ 100いいね未完了チェック】\n確認日時: {now_str}"]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
            )
            page = context.new_page()

            login_to_pokepara(page)
            admin_home_url = page.url
            blog_url = None

            for shop_name in TARGET_SHOPS:
                try:
                    safe_goto(page, admin_home_url, timeout=25000)
                    page.wait_for_timeout(800)
                    switch_shop_if_needed(page, shop_name)
                    admin_home_url = page.url

                    active_casts, casts_list_url, debug_info = get_active_casts_and_proxy_urls(page)
                    if not active_casts:
                        report_lines.append(
                            f"\n■ {shop_name}\n⚠️ 在籍キャスト取得0名\n"
                            f"・URL: {debug_info.get('url')}\n"
                            f"・Title: {debug_info.get('title')}\n"
                            f"・代理ログイン検出数: {debug_info.get('totalProxyLinks')}\n"
                            f"・画面抜粋: {debug_info.get('bodySnippet')}"
                        )
                        continue

                    # (残りいいね数, 表示文字列) のリストとして保持し、最後に降順ソートする
                    remaining_casts = []
                    completed_count = 0

                    for cast in active_casts:
                        try:
                            if not safe_goto(page, cast["href"], timeout=20000):
                                clean_name = clean_cast_name(cast["name"])
                                remaining_casts.append((-1, f"・{clean_name}：通信タイムアウト"))
                                continue

                            if cast["name"] == "キャスト":
                                try:
                                    mypage_text = page.locator("body").inner_text(timeout=5000)
                                    m_name = re.search(r"([^\s\n]+)\s*さん", mypage_text)
                                    if m_name:
                                        cast["name"] = m_name.group(1)
                                except Exception:
                                    pass

                            # 名前から絵文字を除去
                            clean_name = clean_cast_name(cast["name"])

                            if blog_url is None:
                                blog_url = resolve_hokkaido_blog_url(page)

                            remaining = extract_remaining_likes(page, blog_url)
                            print(f"[{shop_name}] {clean_name} -> 残りイイネ: {remaining}", flush=True)

                            if remaining == 0:
                                # 100いいね完了したキャストは表示せず、完了数のみカウント
                                completed_count += 1
                            elif remaining > 0:
                                remaining_casts.append((remaining, f"・{clean_name}：残り {remaining}"))
                            else:
                                remaining_casts.append((-1, f"・{clean_name}：取得失敗（要確認）"))
                        except Exception as cast_err:
                            clean_name = clean_cast_name(cast.get("name", "不明"))
                            print(f"キャスト確認エラー ({clean_name}): {cast_err}", flush=True)
                            remaining_casts.append((-1, f"・{clean_name}：確認中エラー"))

                    # 残りいいね数が多い順（降順）に並び替え
                    remaining_casts.sort(key=lambda x: x[0], reverse=True)

                    report_lines.append(f"\n■ {shop_name}（在籍{len(active_casts)}名 / 完了{completed_count}名）")
                    if remaining_casts:
                        report_lines.extend([line for _, line in remaining_casts])
                    else:
                        report_lines.append("🎉 全員100いいね完了（残り0）！")
                except Exception as shop_err:
                    print(f"店舗処理エラー ({shop_name}): {shop_err}", flush=True)
                    report_lines.append(f"\n■ {shop_name}\n⚠️ 店舗処理中エラー: {shop_err}")

            browser.close()
    except Exception as fatal_err:
        print(f"全体エラー: {fatal_err}", flush=True)
        report_lines.append(f"\n⚠️ 実行中エラー: {fatal_err}")

    final_message = "\n".join(report_lines)
    print("\n--- 最終レポート ---\n" + final_message, flush=True)
    send_line_message(final_message)


if __name__ == "__main__":
    main()
