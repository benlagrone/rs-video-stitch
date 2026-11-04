import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from app import tiktok_highlight_graphics as graphics


class TikTokHighlightGraphicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hf = self.root / "hyperframes"
        package = self.hf / "tiktok-highlight"
        package.mkdir(parents=True)
        candidates = [parent / "hyperframes" / "tiktok-highlight" for parent in Path(__file__).resolve().parents]
        candidates.append(Path("/srv/hyperframes/tiktok-highlight"))
        source = next(path for path in candidates if path.is_dir())
        shutil.copytree(source, package, dirs_exist_ok=True)
        logo = self.root / "media" / "brand" / "logo3.png"
        logo.parent.mkdir(parents=True)
        logo.write_bytes(b"property-logo")
        self.background = self.root / "room.jpg"
        self.background.write_bytes(b"property-photo")
        binary = self.hf / "node_modules" / ".bin" / "hyperframes"
        binary.parent.mkdir(parents=True)
        binary.write_text("binary")
        root_patch = patch.object(graphics, "_hyperframes_root", return_value=self.hf)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def prepare(self, **kwargs):
        return graphics.prepare_highlight_graphics(
            self.root / "work", title="2438 White Oak", hook="Explore this Heights home",
            contact={"email": "jessica@example.test"}, background_image=self.background,
            **kwargs,
        )

    def test_vertical_project_keeps_actual_background_and_offline_animation_assets(self):
        project = self.prepare()
        html = (project / "index.html").read_text() + (project / "compositions/card.html").read_text()
        self.assertEqual((project / "assets/background.jpg").read_bytes(), b"property-photo")
        self.assertIn('data-width="1080" data-height="1920"', html)
        self.assertIn('data-duration="2.000000"', html)
        self.assertIn('src="assets/gsap.min.js"', html)
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertIn("window.__timelines['tiktok-highlight-card'] = tl", html)
        self.assertIn('data-composition-src="compositions/card.html"', html)
        self.assertIn('id="headline" class="clip" data-start="0"', html)

    def test_contact_copy_uses_supplied_email_and_never_invents_an_address(self):
        project = self.prepare(kind="outro", duration=4)
        values = json.loads((project / "variables.json").read_text())
        self.assertEqual(values["phone"], "832-798-3991")
        self.assertEqual(values["email"], "jessica@example.test")
        self.assertEqual(values["brokerage"], "Brokerage: LeCrown Properties")
        self.assertEqual(values["broker"], "Broker: Jie Huang")
        self.assertEqual(graphics._contact_copy({})["email"], "")

    def test_chinese_copy_and_user_text_remain_safe_editable_variables(self):
        project = graphics.prepare_highlight_graphics(
            self.root / "work", title="休斯顿房产 <script>alert(1)</script>",
            hook="了解这套房产", language="zh-CN", kind="outro", duration=4,
            contact={"emails": ["one@example.test", "two@example.test"]},
            background_image=self.background,
        )
        html = (project / "index.html").read_text() + (project / "compositions/card.html").read_text()
        values = json.loads((project / "variables.json").read_text())
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("预约私人看房", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(values["email"], "one@example.test\ntwo@example.test")
        self.assertIn("local('Noto Sans CJK SC')", html)

    def test_render_output_is_isolated_per_card_and_has_silent_audio(self):
        commands = []

        def run(command, log=None):
            commands.append(command)
            target = Path(command[command.index("--output") + 1]) if command[0].endswith("hyperframes") else Path(command[-1])
            target.write_bytes(b"rendered-video")

        with patch.object(graphics, "_run", side_effect=run):
            first = graphics.render_highlight_graphics(self.root / "work", title="A home", hook="Living room", contact={}, kind="transition", duration=.4, background_image=self.background)
            second = graphics.render_highlight_graphics(self.root / "work", title="A home", hook="Kitchen", contact={}, kind="transition", duration=.4, background_image=self.background)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())
        self.assertIn("portrait", commands[0])
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", commands[1])
        self.assertEqual(commands[1][commands[1].index("-t") + 1], "0.4")

    def test_invalid_card_kind_duration_and_missing_background_fail_before_render(self):
        for kwargs in ({"kind": "landscape"}, {"duration": 0}, {"duration": float("nan")}, {"duration": 30}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.prepare(**kwargs)
        with self.assertRaises(FileNotFoundError):
            graphics.prepare_highlight_graphics(self.root / "work", title="A home", hook="Tour", contact={}, background_image=self.root / "missing.jpg")

    def test_render_uses_the_packaged_browser_without_installing_another(self):
        browser = self.root / "chromium"
        browser.write_text("browser")
        with patch.dict(os.environ, {"PUPPETEER_EXECUTABLE_PATH": str(browser)}, clear=True), patch.object(
            graphics.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        ) as run:
            graphics._run(["/renderer/hyperframes", "render"])
            self.assertEqual(run.call_args.kwargs["env"]["HYPERFRAMES_BROWSER_PATH"], str(browser))
            self.assertNotIn("HYPERFRAMES_BROWSER_PATH", os.environ)
