"""Offline regression tests. No Xiaohongshu account or network required."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.xhs_collector import (
    source_url, public_url, note_id, fingerprint,
    collect_slides, collect_comments, snapshot, report, parse_args
)


class ParsingTests(unittest.TestCase):
    def test_extract_share_link(self):
        self.assertEqual(
            source_url('复制： https://xhslink.cn/o/6fC6qdVSi8A 打开小红书'),
            'https://xhslink.cn/o/6fC6qdVSi8A'
        )

    def test_reject_non_xhs(self):
        with self.assertRaises(ValueError):
            source_url('https://fake-xhslink.cn/o/foo')
        with self.assertRaises(ValueError):
            source_url('没有链接')

    def test_strip_tokens(self):
        self.assertEqual(
            public_url('https://www.rednote.com/discovery/item/ABC?xsec_token=SENSITIVE#x'),
            'https://www.rednote.com/discovery/item/ABC'
        )

    def test_note_id(self):
        self.assertEqual(
            note_id('https://www.rednote.com/discovery/item/6af68ea9000000002b03d01b?token=1'),
            '6af68ea9000000002b03d01b'
        )
        self.assertEqual(len(fingerprint('same')), 16)

    def test_parser_limits(self):
        parsed = parse_args(['https://xhslink.cn/o/6fC6qdVSi8A', '--max-photos', '3'])
        self.assertEqual(parsed.max_photos, 3)
        with self.assertRaises(SystemExit):
            parse_args(['https://xhslink.cn/o/foo', '--max-photos', '0'])

    def test_report_marks_incompleteness(self):
        data = {
            'source': {'shared_url':'https://xhslink.cn/o/sample', 'note_id':'abc'},
            'note':{'title':'豆奶','body':'黑芝麻'},
            'gallery':[], 'comments':[], 'comment_images':[],
            'coverage':{'gallery_end':'按钮终止','comments':{'stop_reason':'滚动结束'}}
        }
        result = report(data)
        self.assertIn('黑芝麻', result)
        self.assertIn('不能保证', result)


class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._engine = sync_playwright().start()
            cls._browser = cls._engine.chromium.launch(
                headless=True, executable_path=shutil.which('chromium') or None,
                args=['--no-sandbox']
            )
        except Exception as exc:
            if getattr(cls, '_engine', None):
                cls._engine.stop()
            raise unittest.SkipTest('Chromium not installed: ' + str(exc))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, '_browser'):
            cls._browser.close()
        if hasattr(cls, '_engine'):
            cls._engine.stop()

    def setUp(self):
        self.page = self._browser.new_page(viewport={'width': 1000, 'height': 800})

    def tearDown(self):
        self.page.close()

    def test_carousel_advances_and_stops(self):
        self.page.set_content('''<style>
          .swiper-slide-active{width:320px;height:200px;background:#ddd}
          </style>
          <div class="swiper-slide-active"><b id="panel">第一页</b></div>
          <button class="swiper-button-next" onclick="next()">下一张</button>
          <script>let n=1;function next(){n++;document.querySelector('#panel').textContent='第'+n+'页';if(n===3)document.querySelector('button').disabled=true;}</script>
        ''')
        with tempfile.TemporaryDirectory() as folder:
            images, reason = collect_slides(self.page, Path(folder)/'gallery', 10)
            self.assertEqual(len(images), 3)
            self.assertIn('按钮', reason)
            self.assertTrue((Path(folder)/'gallery'/'03.png').exists())

    def test_top_level_and_nested_replies(self):
        self.page.set_content('''<div class="comments-container" style="height:230px;overflow-y:auto">
          <div class="comment-item" data-id="a"><span class="author">甲</span><p class="content">在天环</p></div>
          <div class="sub-comment-item" data-id="b"><span class="author">作者</span><p class="content">两家都有</p></div>
          <div style="height:950px">其余内容</div></div>''')
        self.assertEqual(len(snapshot(self.page)), 2)
        with tempfile.TemporaryDirectory() as folder:
            comments, photos, coverage = collect_comments(
                self.page, Path(folder)/'comment_images', 4, 1, 2
            )
            self.assertEqual(len(comments), 2)
            self.assertEqual(photos, [])
            self.assertFalse(coverage['complete'])


if __name__ == '__main__':
    unittest.main()
