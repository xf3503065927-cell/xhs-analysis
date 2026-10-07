#!/usr/bin/env python3
"""Local-only Xiaohongshu public note export using a visible browser.

Install: python -m pip install -r requirements.txt
         python -m playwright install chromium
Run:     python scraper.py --profile-url 'YOUR_CURRENT_PROFILE_URL'
Log in manually, open the profile's 笔记 tab with 最新 ordering if offered,
then press Enter in the terminal. No interaction actions are performed.
Signed links expire; supply a fresh browser-copied URL when necessary.
Missing/unavailable fields remain empty, never fabricated or treated as zero.
The browser profile contains login credentials: keep it private.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

DEFAULT_PROFILE = 'https://www.xiaohongshu.com/user/profile/6823166e000000000e02d382'
FIELDS = ['笔记标题', '笔记链接', '发布时间', '内容形式', '点赞数', '收藏数', '评论数', '话题标签']


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def first(obj, *keys):
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return ''


def date_text(value):
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            stamp = float(value)
            if stamp > 100_000_000_000:
                stamp /= 1000
            # Browser data typically uses epoch milliseconds; retain local timezone.
            return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec='seconds')
        except (ValueError, OverflowError, OSError):
            return str(value)
    return str(value or '')


def note_id(url):
    match = re.search(r'/(?:explore|discovery/item)/([a-f0-9]{24})(?:[/?#]|$)', url)
    return match.group(1) if match else ''


def row_from(note, url):
    counts = first(note, 'interact_info', 'interactInfo') or {}
    tags = first(note, 'tag_list', 'tagList') or []
    names = [str(first(tag, 'name', 'tag_name')) for tag in tags if isinstance(tag, dict)]
    kind = first(note, 'type', 'note_type', 'noteType')
    return {
        '笔记标题': first(note, 'title', 'display_title', 'displayTitle'),
        '笔记链接': url,
        '发布时间': date_text(first(note, 'time', 'publish_time', 'publishTime')),
        '内容形式': {'normal': '图文', 'image': '图文', 'video': '视频'}.get(kind, ''),
        '点赞数': first(counts, 'liked_count', 'likedCount'),
        '收藏数': first(counts, 'collected_count', 'collectedCount'),
        '评论数': first(counts, 'comment_count', 'commentCount'),
        '话题标签': '|'.join(name for name in names if name),
    }


def save(rows, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda row: row['发布时间'], reverse=True)
    temporary = target.with_suffix(target.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        # Prevent spreadsheet formula execution in public user-generated text.
        for row in rows:
            writer.writerow({key: "'" + str(value) if str(value).startswith(('=', '+', '-', '@')) else value
                             for key, value in row.items()})
    temporary.replace(target)


async def check_access(page):
    text = await page.locator('body').inner_text(timeout=15000)
    if any(word in text for word in ('访问频次异常', '访问频率过高', '账号异常', '安全验证', '滑块验证')):
        raise RuntimeError('页面出现访问限制或验证。已停止；请在本地手动处理后重新运行，不绕过验证。')


def validate_profile_url(value):
    """Validate only host/path, preserving the original signed query verbatim."""
    url = value.strip()
    # Accept copied Markdown links and enclosing quotation marks/angle brackets.
    markdown = re.fullmatch(r'\[[^\]]*\]\((https://[^\s]+)\)', url)
    if markdown:
        url = markdown.group(1)
    wrappers = {'"': '"', "'": "'", '“': '”', '‘': '’', '<': '>'}
    if len(url) >= 2 and wrappers.get(url[0]) == url[-1]:
        url = url[1:-1].strip()
    if any(char.isspace() for char in url):
        raise ValueError('链接中包含空白字符。请复制完整主页 URL，并在命令行用英文双引号包住整个链接。')
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise ValueError('链接格式无效。请复制浏览器地址栏中的完整主页 URL。') from None
    if (parsed.scheme.lower() != 'https'
            or parsed.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com')
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443)):
        raise ValueError('请提供 https://www.xiaohongshu.com 的博主主页链接；支持 xsec_token 等查询参数。请勿输入“我的完整链接”占位文字。')
    # Query parameters (?xsec_token=...&xsec_source=...) are not part of path.
    match = re.fullmatch(r'/user/profile/([a-fA-F0-9]{24})/?', parsed.path)
    if not match:
        raise ValueError('链接必须是 /user/profile/ 后跟完整的 24 位用户 ID；可携带任意查询参数，请勿用省略号替代 ID。')
    return url, match.group(1)


async def random_pause(args):
    # Pacing only; no fingerprint changes or verification bypass.
    await asyncio.sleep(random.uniform(args.delay_min, args.delay_max))


def detail_note(payload, ident):
    result = {}
    for item in objects(payload):
        candidate = first(item, 'note_id', 'noteId', 'id')
        note = item if candidate == ident else item.get(ident, {})
        if isinstance(note, dict):
            note = note.get('note', note.get('noteCard', note.get('note_card', note)))
            if isinstance(note, dict) and any(key in note for key in ('title', 'desc', 'interactInfo', 'interact_info')):
                for key, value in note.items():
                    if value is not None and value != '':
                        if isinstance(value, dict) and isinstance(result.get(key), dict):
                            result[key] = {**result[key], **value}
                        else:
                            result[key] = value
    return result


async def extract_detail(page, ident, url, payloads):
    # Wait for detail rendering rather than assuming that navigation means ready.
    await page.locator('#noteContainer, .note-detail, .note-content').first.wait_for(
        state='visible', timeout=20000)
    await check_access(page)
    state = await page.evaluate('''() => {
        try { return JSON.parse(JSON.stringify(window.__INITIAL_STATE__ || {},
          (key, value) => value === undefined ? null : value)); }
        catch (_) { return {}; }
    }''')
    note = {}
    for payload in [*payloads, state]:
        for key, value in detail_note(payload, ident).items():
            if isinstance(value, dict) and isinstance(note.get(key), dict):
                note[key] = {**note[key], **value}
            else:
                note[key] = value
    row = row_from(note, url)
    dom = await page.evaluate(r'''() => {
        const root = document.querySelector('#noteContainer, .note-detail') || document;
        const text = (selectors) => {
            for (const selector of selectors) {
                const element = root.querySelector(selector);
                if (element && element.textContent.trim()) return element.textContent.trim();
            }
            return '';
        };
        const count = (selectors) => {
            const value = text(selectors);
            const match = value.match(/[0-9]+(?:[.,][0-9]+)*(?:万|亿|千|[kKwWmM])?\+?/);
            return match ? match[0] : ''; // A label without a number is unknown, not zero.
        };
        const time = root.querySelector('time[datetime]');
        return {
            '笔记标题': text(['#detail-title', '.note-content .title', '.title']),
            '发布时间': time ? time.getAttribute('datetime') : text(['.bottom-container .date', '.note-content .date', '.date']),
            '点赞数': count(['.like-wrapper .count', '.like-wrapper', '[aria-label*="点赞"]']),
            '收藏数': count(['.collect-wrapper .count', '.collect-wrapper', '[aria-label*="收藏"]']),
            '评论数': count(['.chat-wrapper .count', '.comment-wrapper .count', '.chat-wrapper', '[aria-label*="评论"]']),
            '话题标签': [...new Set([...root.querySelectorAll('#detail-desc a.tag, .note-content a.tag, a[href*="/search_result?keyword=%23"]')]
                .map(n => n.textContent.trim().replace(/^#/, '')).filter(Boolean))].join('|'),
            '内容形式': root.querySelector('video') ? '视频' : (root.querySelector('.swiper img, .note-slider img') ? '图文' : '')
        };
    }''')
    for key, value in dom.items():
        if row[key] == '' and value:
            row[key] = value
    if not row['笔记标题'] and not note:
        raise RuntimeError('详情未加载或页面结构已变化')
    return row


async def scrape_details(page, links, args, profile_url):
    """Navigate the existing profile tab; never click anchors or open detail tabs."""
    rows = []
    failures = []
    attempted = 0
    profile = urlparse(profile_url)

    def at_home():
        current = urlparse(page.url)
        return (current.hostname == profile.hostname
                and current.path.rstrip('/') == profile.path.rstrip('/'))

    targets = list(links.items())[:args.limit]
    for index, (ident, anchor) in enumerate(targets, 1):
        if page.is_closed():
            print('当前标签页或浏览器已关闭，无法在同一标签页继续；已保存结果保留。')
            break
        attempted += 1
        pending = set()
        payloads = []

        async def capture(response):
            host = urlparse(response.url).hostname or ''
            if not (host == 'xiaohongshu.com' or host.endswith('.xiaohongshu.com')):
                return
            if 'application/json' not in response.headers.get('content-type', ''):
                return
            try:
                payloads.append(await response.json())
            except Exception:
                pass

        def on_response(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        try:
            if not at_home():
                await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
                await random_pause(args)
            await random_pause(args)
            page.on('response', on_response)
            url = urljoin('https://www.xiaohongshu.com', anchor['href'])
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            await random_pause(args)
            await check_access(page)
            if pending:
                await asyncio.wait(list(pending), timeout=5)
            row = await extract_detail(page, ident, url, payloads)
            # Write only after detail extraction. Persist successes incrementally.
            save([*rows, row], args.output)
            rows.append(row)
            missing = [key for key in FIELDS if row[key] == '']
            print(f'{index}/{len(targets)} 详情已保存' + (f'；缺失字段：{", ".join(missing)}' if missing else ''))
        except Exception as error:
            failures.append(ident)
            # Never print exception text: browser diagnostics may include signed URLs.
            print(f'{index}/{len(targets)} 笔记 {ident} 失败（{type(error).__name__}），跳过并继续。')
        finally:
            page.remove_listener('response', on_response)
            tasks = list(pending)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if not page.is_closed() and not at_home():
                try:
                    await random_pause(args)
                    await page.go_back(wait_until='domcontentloaded', timeout=60000)
                    await random_pause(args)
                    # A failed navigation may not have added a history entry.
                    if not at_home():
                        await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
                        await random_pause(args)
                except Exception as error:
                    print(f'返回主页失败（{type(error).__name__}）；下一条开始前会尝试在同一标签页恢复主页。')
    if failures:
        print('失败笔记 ID：' + ', '.join(failures))
    print(f'处理完毕：尝试 {attempted}/{len(targets)} 条，保存 {len(rows)} 条，失败 {len(failures)} 条。')
    if rows:
        print(f'CSV：{args.output.resolve()}；无法读取的字段留空。')
    else:
        print('没有成功提取详情；已有 CSV 未覆盖。')


async def run(args):
    profile_url, profile_id = validate_profile_url(args.profile_url)
    async with async_playwright() as playwright:
        async def launch_context():
            return await playwright.chromium.launch_persistent_context(
                str(args.browser_profile.resolve()), headless=False,
                viewport={'width': 1280, 'height': 900}, locale='zh-CN')

        context = await launch_context()
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
            print('请手动登录，确认主页笔记列表可见，并选择最新排序（如有）。')
            await asyncio.to_thread(input, '准备好后按 Enter 开始；Ctrl+C 取消：')
            if profile_id not in urlparse(page.url).path:
                await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
            await check_access(page)
            links = {}
            unchanged = 0
            for _ in range(args.max_scrolls):
                before = len(links)
                anchors = await page.locator('a[href*="/explore/"], a[href*="/discovery/item/"]').evaluate_all(
                    '''(nodes) => nodes.map(n => ({href:n.getAttribute('href'), title:n.innerText.trim(),
                       pinned: /置顶/.test((n.closest('section') || n.closest('.note-item') || n).innerText)}))''')
                for anchor in anchors:
                    ident = note_id(anchor['href'])
                    if ident and ident not in links and not anchor['pinned']:
                        links[ident] = anchor
                print(f'已发现 {len(links)} 条不同笔记链接。')
                if len(links) >= args.limit:
                    break
                unchanged = unchanged + 1 if len(links) == before else 0
                if unchanged >= 5:
                    break
                await page.mouse.wheel(0, 900)
                await random_pause(args)
                await check_access(page)
            if not links:
                raise RuntimeError('未找到笔记链接。请检查登录和主页可见性；已有 CSV 未覆盖。')
            print('主页链接收集完成；开始逐篇打开详情页。')
            await scrape_details(page, links, args, profile_url)
        finally:
            try:
                await context.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile-url', default=DEFAULT_PROFILE, help='推荐传入最新的完整签名主页链接')
    parser.add_argument('--limit', type=int, default=50, help='最多 50 条')
    parser.add_argument('--output', type=Path, default=Path('data/notes.csv'))
    parser.add_argument('--browser-profile', type=Path, default=Path.home() / '.xhs-scraper-browser')
    parser.add_argument('--delay-min', type=float, default=2.0, help='随机等待下限秒数，默认 2')
    parser.add_argument('--delay-max', type=float, default=4.0, help='随机等待上限秒数，默认 4')
    parser.add_argument('--max-scrolls', type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.limit <= 50 or not 2 <= args.delay_min < args.delay_max <= 60 or not 1 <= args.max_scrolls <= 100:
        parser.error('limit 必须为 1–50，随机等待须满足 2 <= delay-min < delay-max <= 60，max-scrolls 必须为 1–100。')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('\n已取消；此前已写入的 CSV 保留。')
    except Exception as error:
        # Do not log signed URLs, cookie values, or raw Playwright diagnostics.
        print(f'停止：{error}' if isinstance(error, (ValueError, RuntimeError)) else f'停止：{type(error).__name__}。请检查浏览器页面和本地安装。')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
