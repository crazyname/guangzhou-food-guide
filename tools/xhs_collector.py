"""Free, local, user-initiated Xiaohongshu gallery/comment evidence collection.

Automates the visible browser UI. Does not bypass login, CAPTCHAs or rate limits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import Error as PWError, sync_playwright

HOSTS = ('xiaohongshu.com', 'rednote.com', 'xhslink.cn', 'xhslink.com')
NOTE_RE = re.compile(r'/(?:discovery/item|explore)/([0-9a-f]{24})', re.I)
SLIDES = ('.swiper-slide-active', '.swiper-slide[class*="active"]', '.carousel-item.active', '.note-container .swiper', '.media-container', '.slider-container')
NEXT = ('.swiper-button-next', '.slide-next', '.next-btn', '.carousel-next', '.arrow-controller .right', '.arrow-controller .next', '[aria-label="下一张"]', '[aria-label="Next"]')
COMMENTS = ('.comment-item', '.sub-comment-item', '.parent-comment', '.comment-item-wrap', '.comment-list .item', '.comment-container .item')
ROOTS = ('.comments-container', '.comment-container', '.comment-list', '#comments')
PICTURES = ('.comment-picture img', '.comment-picture', '.comment-img img', '.comment-image img', '.comment-pic img', '.comment-img', '.comment-image', '[class*="comment-image"] img')
EXPAND = re.compile(r'展开.{0,12}(回复|评论)|更多.{0,8}回复|查看.{0,8}回复|共\d+条回复|展开更多')


def source_url(text: str) -> str:
    match = re.search(r'https?://[^\s<>\u3000]+', text.strip())
    if not match:
        raise ValueError('请提供小红书 https:// 分享链接')
    url = match.group().rstrip('。，,.;)）')
    host = (urlparse(url).hostname or '').lower()
    if not any(host == h or host.endswith('.' + h) for h in HOSTS):
        raise ValueError('不允许非小红书来源链接')
    return url


def public_url(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, '', '', ''))


def note_id(url: str) -> str:
    m = NOTE_RE.search(urlparse(url).path)
    return m.group(1).lower() if m else hashlib.sha256((public_url(url) or url).encode()).hexdigest()[:24]


def fingerprint(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible(timeout=400)
    except PWError:
        return False


def slide_screenshot(page, path: Path) -> tuple[str, str | None]:
    for selector in SLIDES:
        try:
            loc = page.locator(selector).first
            if not visible(loc):
                continue
            box = loc.bounding_box(timeout=800)
            if box and box['width'] > 120 and box['height'] > 120:
                raw = loc.screenshot(animations='disabled', timeout=5000)
                path.write_bytes(raw)
                img = loc.locator('img')
                src = img.first.get_attribute('src', timeout=500) if img.count() else None
                return hashlib.sha256(raw).hexdigest(), public_url(src)
        except PWError:
            continue
    raw = page.screenshot(full_page=False, animations='disabled')
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), None


def advance_slide(page) -> bool:
    for selector in NEXT:
        try:
            loc = page.locator(selector).first
            if visible(loc) and loc.is_enabled(timeout=400):
                loc.click(timeout=1700)
                return True
        except PWError:
            continue
    return False


def collect_slides(page, directory: Path, maximum: int) -> tuple[list[dict], str]:
    directory.mkdir(parents=True, exist_ok=True)
    seen = set()
    items = []
    reason = '未找到下一张按钮，无法证明图片全部采集'
    for index in range(1, maximum + 1):
        dest = directory / f'{index:02}.png'
        digest, source = slide_screenshot(page, dest)
        if digest in seen:
            dest.unlink(missing_ok=True)
            reason = '出现重复画面，已停止继续翻页'
            break
        seen.add(digest)
        items.append({'index': index, 'path': str(dest.relative_to(directory.parent)), 'image_source': source})
        if not advance_slide(page):
            reason = '下一张按钮已不可见或不可用'
            break
        page.wait_for_timeout(750)
    else:
        reason = f'达到图片上限 {maximum}'
    return items, reason


COMMENT_JS = r'''(elements) => elements.map(node => {
  const txt = (selectors) => {
    for (const s of selectors) {
      const e = node.querySelector(s);
      if (e && (e.innerText || e.textContent || '').trim()) return (e.innerText || e.textContent).trim();
    }
    return '';
  };
  const author = txt(['.author','.user-name','.username','.name','[class*="author"]']);
  const text = txt(['.content','.comment-content','.comment-text','[class*="commentContent"]']) || (node.innerText || '').trim().slice(0,3000);
  const domid = node.getAttribute('data-id') || node.getAttribute('data-comment-id') || node.id || '';
  const image_sources = [...new Set([...node.querySelectorAll('img')]
    .filter(img => !img.closest('[class*="avatar"]') && !img.matches('[class*="avatar"]'))
    .map(img => {try {const u = new URL(img.currentSrc || img.getAttribute('src') || '',location.href);return u.origin+u.pathname;}catch{return '';}})
    .filter(Boolean))];
  return {domid, author, text, image_sources};
})'''


def snapshot(page) -> list[dict]:
    # Top-level comments and replies may be rendered in separate DOM structures.
    found = {}
    for selector in COMMENTS:
        try:
            loc = page.locator(selector)
            if loc.count():
                for row in loc.evaluate_all(COMMENT_JS):
                    if not row['text']:
                        continue
                    key = ('id:' + row['domid'] if row['domid']
                           else fingerprint(row['author'] + '\n' + row['text']))
                    found[key] = dict(row, key=key)
        except PWError:
            continue
    return list(found.values())


def comment_root(page):
    for selector in ROOTS:
        loc = page.locator(selector).first
        if visible(loc):
            return loc
    return page.locator('body')


def expand(page, root, maximum=14) -> int:
    clicks = 0
    for _ in range(maximum):
        try:
            candidates = root.locator('button,a,span').filter(has_text=EXPAND)
            chosen = None
            for i in range(min(80, candidates.count())):
                c = candidates.nth(i)
                if visible(c):
                    content = c.inner_text(timeout=400).strip()
                    if 2 <= len(content) <= 35 and EXPAND.search(content):
                        chosen = c
                        break
            if chosen is None:
                break
            chosen.click(timeout=1800)
            clicks += 1
            page.wait_for_timeout(500)
        except PWError:
            break
    return clicks


SCROLL_JS = r'''(root) => {
  const c = [root, ...root.querySelectorAll('*')];
  const e = c.find(x => {
    const s = getComputedStyle(x);
    return x.clientHeight > 200 && x.scrollHeight > x.clientHeight + 80 && /(auto|scroll)/.test(s.overflowY);
  }) || document.scrollingElement || document.documentElement;
  const before = e.scrollTop;
  e.scrollTop += Math.max(500,Math.floor(e.clientHeight*.8));
  return {before,after:e.scrollTop,bottom:e.scrollTop+e.clientHeight>=e.scrollHeight-12};
}'''


def open_comment_pictures(page, directory: Path, seen: set[str], maximum: int) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    output = []
    for selector in PICTURES:
        try:
            images = page.locator(selector)
            for i in range(min(images.count(), 30)):
                if len(seen) >= maximum:
                    return output
                pic = images.nth(i)
                if not visible(pic):
                    continue
                src = pic.get_attribute('src', timeout=300) or pic.get_attribute('style', timeout=300) or ''
                if not src:
                    continue
                key = fingerprint(src)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pic.click(timeout=1700)
                    page.wait_for_timeout(400)
                    file = directory / f'{len(seen):03}.png'
                    page.screenshot(path=str(file), full_page=False, animations='disabled')
                    output.append({'key': key, 'path': str(file.relative_to(directory.parent.parent)), 'source': public_url(src) if src.startswith('http') else None})
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(250)
                except PWError as e:
                    output.append({'key': key, 'error': str(e)[:150]})
        except PWError:
            continue
    return output


def collect_comments(page, directory: Path, max_scrolls: int, stall_limit: int, max_images: int) -> tuple[list[dict], list[dict], dict]:
    root = comment_root(page)
    try:
        root.scroll_into_view_if_needed(timeout=2000)
    except PWError:
        pass
    comments = {}
    seen_images: set[str] = set()
    photos = []
    stalled = 0
    prev_position = -1
    expanded = 0
    reason = f'达到滚动上限 {max_scrolls}'
    for step in range(1, max_scrolls+1):
        expanded += expand(page, root)
        old_count = len(comments)
        for row in snapshot(page):
            comments[row['key']] = row
        photos.extend(open_comment_pictures(page, directory / 'images', seen_images, max_images))
        try:
            scroll = root.evaluate(SCROLL_JS)
        except PWError:
            scroll = {'after':prev_position,'bottom':False}
        page.wait_for_timeout(1000)
        for row in snapshot(page):
            comments[row['key']] = row
        if len(comments) == old_count and scroll['after'] == prev_position:
            stalled += 1
        else:
            stalled = 0
        prev_position = scroll['after']
        if scroll['bottom'] and stalled >= stall_limit:
            reason = '页面到达可见底部且多次无增量（不保证全部回复可访问）'
            break
        if stalled >= stall_limit * 2:
            reason = '连续无增量：可能到底、滚动对象改变或访问受限'
            break
    else:
        step = max_scrolls
    return list(comments.values()), photos, {
        'attempts': step, 'expanded_controls': expanded, 'captured': len(comments),
        'previewed_images': len([p for p in photos if 'path' in p]),
        'stop_reason': reason, 'complete': False
    }


def note_metadata(page) -> dict:
    info = {'title':page.title(), 'author':None, 'body':''}
    for selector in ('.note-content','#detail-desc','.desc','.note-text','.content'):
        try:
            loc = page.locator(selector).first
            if loc.count():
                text = loc.inner_text(timeout=600).strip()
                if len(text)>len(info['body']):
                    info['body']=text[:12000]
        except PWError:
            pass
    for selector in ('#noteContainer .author-wrapper .name','.author .name','.user-name','.author-name'):
        try:
            loc=page.locator(selector).first
            if visible(loc):
                info['author']=loc.inner_text(timeout=500).strip()
                break
        except PWError:
            pass
    return info


def archive(folder: Path) -> Path:
    zip_path=folder.with_suffix('.zip')
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z:
        for path in folder.rglob('*'):
            if path.is_file():
                z.write(path,path.relative_to(folder.parent))
    return zip_path


def report(data: dict) -> str:
    coverage=data['coverage']
    return '\n'.join([
        '# 小红书采集证据包', '',
        '原笔记：'+data['source']['shared_url'],
        '笔记ID：'+data['source']['note_id'],
        '标题：'+data['note']['title'],
        '图片：'+str(len(data['gallery']))+' 张，'+coverage['gallery_end'],
        '评论：'+str(len(data['comments']))+' 条，'+coverage['comments']['stop_reason'],
        '评论配图预览：'+str(len(data['comment_images']))+' 条尝试', '',
        '## 正文（未核实商家事实）', data['note']['body'] or '网页正文未取得', '',
        '## 如何审阅',
        '轮播图片参见 gallery 文件夹，评论配图参见 comment_images/images 文件夹。',
        '评论原文、来源和对应证据路径在 manifest.json。',
        '先从图中找品牌/地址，再识别作者评论，最后用商家官网交叉核实。',
        '人均与价格没有证据时保持未知。当前不能保证平台全部评论已经加载。', ''
    ])


def parse_args(argv=None):
    p=argparse.ArgumentParser(description='小红书本地浏览器证据采集器（免费，非官方）')
    p.add_argument('url', nargs='?', help='单条小红书笔记链接或含链接分享文本')
    p.add_argument('--login-only', action='store_true', help='打开浏览器，手动登录后按回车退出')
    p.add_argument('--pause-login', action='store_true', help='抓取前等待用户手动登录/验证')
    p.add_argument('--profile', type=Path, default=Path.home()/'.guangzhou-food-guide'/'browser-profile')
    p.add_argument('--output', type=Path, default=Path('captures'))
    p.add_argument('--max-photos', type=int, default=30)
    p.add_argument('--max-scrolls', type=int, default=120)
    p.add_argument('--stall-rounds', type=int, default=5)
    p.add_argument('--max-comment-images', type=int, default=80)
    a=p.parse_args(argv)
    if not a.url and not a.login_only:
        p.error('缺少分享链接；或者使用 --login-only')
    if any(getattr(a,n)<1 for n in ('max_photos','max_scrolls','stall_rounds','max_comment_images')):
        p.error('采集上限参数必须大于 0')
    return a


def main(argv=None) -> int:
    a=parse_args(argv)
    url=source_url(a.url) if a.url else 'https://www.xiaohongshu.com/'
    a.profile.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        context=p.chromium.launch_persistent_context(str(a.profile),headless=False,viewport={'width':1440,'height':900},locale='zh-CN')
        page=context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url,wait_until='domcontentloaded',timeout=45000)
            page.wait_for_timeout(1500)
            if a.login_only or a.pause_login:
                input('请在浏览器里登录/验证；完成后回到终端按 Enter：')
            if a.login_only:
                print('本地登录资料已保存于',a.profile)
                return 0
            host=(urlparse(page.url).hostname or '').lower()
            if not any(host==h or host.endswith('.'+h) for h in HOSTS):
                raise ValueError('分享链接跳转到非小红书网站，已停止')
            body=page.locator('body').inner_text(timeout=3000)[:1200]
            if ('登录后' in body or '安全验证' in body) and not note_metadata(page)['body']:
                input('需要登录/验证：请在浏览器中操作后按 Enter，不能进入则 Ctrl+C：')
            dest=a.output/note_id(page.url)/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            dest.mkdir(parents=True,exist_ok=True)
            meta=note_metadata(page)
            gallery,end=collect_slides(page,dest/'gallery',a.max_photos)
            comments,images,comment_coverage=collect_comments(page,dest/'comment_images',a.max_scrolls,a.stall_rounds,a.max_comment_images)
            payload={
                'schema_version':1,'collected_at':datetime.now(timezone.utc).isoformat(),
                'source':{'shared_url':public_url(url),'final_url':public_url(page.url),'note_id':note_id(page.url)},
                'note':meta,'gallery':gallery,'comments':comments,'comment_images':images,
                'coverage':{'gallery_end':end,'comments':comment_coverage,
                            'warning':'仅记录可访问浏览器UI内容，不能证明已读取全部评论。'}
            }
            (dest/'manifest.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            (dest/'REVIEW.md').write_text(report(payload),encoding='utf-8')
            zip_file=archive(dest)
            print(f'轮播图片 {len(gallery)}，评论 {len(comments)}，评论配图预览 {len(images)}')
            print('证据包：',zip_file.resolve())
            return 0
        finally:
            context.close()


if __name__=='__main__':
    try:
        sys.exit(main())
    except (ValueError,PWError,KeyboardInterrupt) as e:
        print('采集未完成：',e,file=sys.stderr)
        sys.exit(2)
