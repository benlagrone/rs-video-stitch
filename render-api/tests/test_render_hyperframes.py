import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import _hydrate_saved_hyperframes


class RenderHyperframesTest(TestCase):
    def test_uses_saved_project_hyperframes_when_request_omits_them(self):
        options = {"tts": "zh-CN-XiaoxiaoNeural"}
        saved = {"hyperframes": {"leadCardMode": "hyperframes", "leadCard": "lecrown-lead-card"}}

        _hydrate_saved_hyperframes(options, saved)

        self.assertEqual(options["hyperframes"], saved["hyperframes"])

    def test_keeps_explicit_render_hyperframes(self):
        options = {"hyperframes": {"leadCardMode": "traditional"}}
        saved = {"hyperframes": {"leadCardMode": "hyperframes"}}

        _hydrate_saved_hyperframes(options, saved)

        self.assertEqual(options["hyperframes"]["leadCardMode"], "traditional")
