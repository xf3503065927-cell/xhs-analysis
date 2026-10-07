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
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

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


async def run(args):
    profile_url, profile_id = validate_profile_url(args.profile_url)
    rows = []
    cache = {}
    pending = set()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(args.browser_profile.resolve()), headless=False,
            viewport={'width': 1280, 'height': 900}, locale='zh-CN',
        )

        async def capture(response):
            # Read JSON already delivered to the visible browser, never call private APIs.
            host = urlparse(response.url).hostname or ''
            if not (host == 'xiaohongshu.com' or host.endswith('.xiaohongshu.com')):
                return
            if 'application/json' not in response.headers.get('content-type', ''):
                return
            try:
                data = await response.json()
                for item in objects(data):
                    ident = first(item, 'note_id', 'noteId', 'id')
                    if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{24}', ident):
                        continue
                    if not any(key in item for key in ('title', 'display_title', 'displayTitle', 'desc')):
                        continue
                    if first(item, 'type', 'note_type', 'noteType') not in ('normal', 'image', 'video'):
                        continue
                    cache[ident] = {**cache.get(ident, {}), **item}
            except (ValueError, PlaywrightTimeout):
                pass
            except Exception:
                # Some responses disappear during navigation; DOM remains a fallback.
                pass

        def on_response(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        context.on('response', on_response)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
            print('请在浏览器中手动登录，确认目标主页的笔记列表可见，并选择最新排序（如有）。')
            await asyncio.to_thread(input, '准备好后按 Enter 开始；Ctrl+C 取消：')
            if profile_id not in urlparse(page.url).path:
                await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
            await check_access(page)
            links = {}
            unchanged = 0
            for _ in range(args.max_scrolls):
                before = len(links)
                anchors = await page.locator('a[href*="/explore/"], a[href*="/discovery/item/"]').evaluate_all(
                    '''(nodes) => nodes.map(n => ({href:n.href, title:n.innerText.trim(),
                       pinned: /置顶/.test((n.closest('section') || n.closest('.note-item') || n).innerText)}))'''
                )
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
                await page.wait_for_timeout(args.delay * 1000)
                await check_access(page)
            if not links:
                raise RuntimeError('未找到笔记链接。请检查登录、主页可见性或页面结构；未覆盖已有 CSV。')
            print('按主页最新顺序读取，跳过页面明确标记的置顶笔记；请确保主页未选择热门排序。')
            detail = await context.new_page()
            for ident, anchor in list(links.items())[:args.limit]:
                url = urljoin('https://www.xiaohongshu.com', anchor['href'])
                try:
                    await detail.goto(url, wait_until='domcontentloaded', timeout=60000)
                    await detail.wait_for_timeout(args.delay * 1000)
                    await check_access(detail)
                    state = await detail.evaluate('''() => {
                        try { return JSON.parse(JSON.stringify(window.__INITIAL_STATE__ || {},
                          (key, value) => value === undefined ? null : value)); }
                        catch (_) { return {}; }
                    }''')
                    for item in objects(state):
                        candidate = first(item, 'note_id', 'noteId', 'id')
                        if candidate == ident and any(k in item for k in ('title', 'desc', 'interactInfo', 'interact_info')):
                            cache[ident] = {**cache.get(ident, {}), **item}
                    if pending:
                        await asyncio.gather(*list(pending), return_exceptions=True)
                    row = row_from(cache.get(ident, {}), url)
                    if not row['笔记标题']:
                        title = detail.locator('#detail-title, .note-detail .title').first
                        row['笔记标题'] = (await title.inner_text()).strip() if await title.count() else anchor['title']
                    if not row['话题标签']:
                        tags = await detail.locator('#detail-desc a.tag, .note-detail a.tag').all_text_contents()
                        row['话题标签'] = '|'.join(tag.strip().lstrip('#') for tag in tags)
                    rows.append(row)
                    save(rows, args.output)
                    missing = [key for key in FIELDS if row[key] == '']
                    print(f'{len(rows)}/{min(args.limit, len(links))} 已保存' + (f'；缺失字段：{", ".join(missing)}' if missing else ''))
                except PlaywrightTimeout:
                    print('笔记加载超时，停止并保留已保存结果。请检查本地网络后重试。')
                    break
            print(f'完成：{len(rows)} 条，文件：{args.output.resolve()}。缺失值为空，计数保留网站显示格式。')
        finally:
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
            await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile-url', default=DEFAULT_PROFILE, help='推荐传入最新的完整签名主页链接')
    parser.add_argument('--limit', type=int, default=50, help='最多 50 条')
    parser.add_argument('--output', type=Path, default=Path('data/notes.csv'))
    parser.add_argument('--browser-profile', type=Path, default=Path.home() / '.xhs-scraper-browser')
    parser.add_argument('--delay', type=float, default=3.0, help='页面加载和滚动间隔秒数，至少 2 秒')
    parser.add_argument('--max-scrolls', type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.limit <= 50 or args.delay < 2 or not 1 <= args.max_scrolls <= 100:
        parser.error('limit 必须为 1–50，delay 至少为 2，max-scrolls 必须为 1–100。')
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
