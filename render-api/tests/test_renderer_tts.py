import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import renderer


def _setup_project(tmpdir: Path, api_value: str, voice: str = "custom-voice") -> Path:
    storage_root = tmpdir
    pid = "pid123"
    project_root = storage_root / "projects" / pid
    input_dir = project_root / "input"
    images_dir = input_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "img001.png").write_bytes(b"fake")

    scenes = {
        "vid": {
            "voice": voice,
            "lang": "en",
            "api": api_value,
        },
        "scenes": [
            {
                "title": "",
                "images": ["img001.png"],
                "VO": "Hello world",
            }
        ],
    }
    scenes_path = input_dir / "scenes.json"
    scenes_path.write_text(json.dumps(scenes), encoding="utf-8")
    return storage_root


class RendererTTSTest(TestCase):
    def test_bottom_scripture_caption_wraps_reference_and_verse(self):
        commands = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            title_file = root / "caption.txt"
            with mock.patch.object(renderer, "run", side_effect=lambda cmd, log=None: commands.append(cmd)):
                renderer._overlay_title(
                    source,
                    root / "captioned.mp4",
                    "Genesis 1:2\nAnd the earth was without form, and void; and darkness was upon the face of the deep.",
                    title_file,
                    root / "font.ttf",
                    {"position": "bottom-center", "fontSize": 48},
                    "medium",
                    "18",
                    None,
                )
            caption = title_file.read_text(encoding="utf-8")
        self.assertTrue(caption.startswith("Genesis 1:2\n"))
        self.assertGreater(caption.count("\n"), 1)
        self.assertIn("y=h-text_h-h*0.08", commands[0][commands[0].index("-vf") + 1])

    def test_final_concat_normalizes_mixed_intro_and_scene_time_bases(self):
        graph = renderer._normalized_concat_filter(2, 30)

        self.assertIn("[0:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS[v0]", graph)
        self.assertIn("[1:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS[v1]", graph)
        self.assertIn("aresample=48000", graph)
        self.assertIn("channel_layouts=stereo", graph)
        self.assertTrue(graph.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"))

    def test_vibevoice_preserves_named_english_presets(self):
        self.assertEqual(renderer._vibevoice_speaker_name("en-Emma_woman"), "en-Emma_woman")
        self.assertEqual(
            renderer._vibevoice_speaker_name("en-US-AdamMultilingualNeural"),
            renderer.VIBEVOICE_DEFAULT_SPEAKER,
        )

    def _common_patches(self):
        fake_run_outputs = []

        def fake_run(cmd, log=None):
            if cmd:
                target = Path(cmd[-1])
                if target.suffix:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"")
                    fake_run_outputs.append(target)

        patches = [
            mock.patch.object(renderer, "run", side_effect=fake_run),
            mock.patch.object(renderer, "ffprobe_duration", return_value=5.0),
            mock.patch.object(renderer, "DEFAULT_TTS_VOICE", "fallback-voice"),
            mock.patch.object(renderer, "DEFAULT_TTS_LANGUAGE", "en"),
            mock.patch.object(renderer, "DEFAULT_TTS_API", "xtts"),
        ]
        return patches

    def test_xtts_is_used_when_api_xtts(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp)
            storage_root = _setup_project(storage_root, "xtts")
            xtts_calls = []
            azure_calls = []

            def fake_xtts(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"xtts")
                xtts_calls.append((text, destination, voice, language))
                return destination

            def fake_azure(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"azure")
                azure_calls.append((text, destination, voice, language))
                return destination

            patches = self._common_patches() + [
                mock.patch.object(renderer, "synthesize_xtts", side_effect=fake_xtts),
                mock.patch.object(renderer, "synthesize_azure", side_effect=fake_azure),
            ]

            for patch in patches:
                patch.start()
            try:
                result = renderer.render_project(
                    "pid123",
                    storage_root,
                    {},
                    "output.mp4",
                )
            finally:
                for patch in reversed(patches):
                    patch.stop()

            self.assertTrue(result.exists())
            self.assertEqual(result.name, "output.mp4")
            self.assertEqual(len(xtts_calls), 1)
            self.assertEqual(len(azure_calls), 0)

            text, destination, voice, language = xtts_calls[0]
            self.assertEqual(text, "Hello world")
            self.assertEqual(voice, "custom-voice")
            self.assertEqual(language, "en")
            self.assertTrue(destination.exists())

    def test_azure_is_used_when_api_azure(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp)
            storage_root = _setup_project(
                storage_root,
                "azure",
                voice="en-US-AdamMultilingualNeural",
            )
            xtts_calls = []
            azure_calls = []

            def fake_xtts(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"xtts")
                xtts_calls.append((text, destination, voice, language))
                return destination

            def fake_azure(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"azure")
                azure_calls.append((text, destination, voice, language))
                return destination

            patches = self._common_patches() + [
                mock.patch.object(renderer, "synthesize_xtts", side_effect=fake_xtts),
                mock.patch.object(renderer, "synthesize_azure", side_effect=fake_azure),
                mock.patch.dict(
                    os.environ,
                    {"AZURE_TTS_VOICE": "en-US-AriaNeural"},
                    clear=False,
                ),
            ]

            for patch in patches:
                patch.start()
            try:
                result = renderer.render_project(
                    "pid123",
                    storage_root,
                    {},
                    "output.mp4",
                )
            finally:
                for patch in reversed(patches):
                    patch.stop()

            self.assertTrue(result.exists())
            self.assertEqual(result.name, "output.mp4")
            self.assertEqual(len(azure_calls), 1)
            self.assertEqual(len(xtts_calls), 0)

            text, destination, voice, language = azure_calls[0]
            self.assertEqual(text, "Hello world")
            self.assertEqual(voice, "en-US-AdamMultilingualNeural")
            self.assertEqual(language, "en")
            self.assertTrue(destination.exists())

    def test_voice_gateway_owns_narration_provider_selection(self):
        create_response = mock.Mock()
        create_response.json.return_value = {
            "job_id": "voice-1",
            "status": "completed",
            "audio_url": "/files/project/audio/voice.wav",
        }
        audio_response = mock.Mock(content=b"gateway-audio")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            renderer.requests, "post", return_value=create_response
        ) as post, mock.patch.object(renderer.requests, "get", return_value=audio_response) as get:
            destination = Path(tmp) / "voice.wav"
            renderer._synthesize_voice_gateway(
                "Hello world",
                destination,
                project_id="pid123",
                voice="Carter",
                language="en-US",
                log=None,
            )
            audio_bytes = destination.read_bytes()

        self.assertEqual(audio_bytes, b"gateway-audio")
        self.assertTrue(post.call_args.args[0].endswith("/v1/tts/jobs"))
        self.assertEqual(post.call_args.kwargs["json"]["speaker_name"], "Carter")
        self.assertNotIn("provider", post.call_args.kwargs["json"])
        self.assertTrue(get.call_args.args[0].endswith("/files/project/audio/voice.wav"))

    def test_missing_tts_configuration_falls_back_to_local_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp)
            storage_root = _setup_project(
                storage_root,
                "azure",
                voice="en-US-AdamMultilingualNeural",
            )
            flite_outputs = []

            def fake_run(cmd, log=None):
                if any(str(part).startswith("flite=textfile=") for part in cmd):
                    target = Path(cmd[-1])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"flite")
                    flite_outputs.append(target)
                    return
                if cmd:
                    target = Path(cmd[-1])
                    if target.suffix:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"")

            patches = [
                mock.patch.object(renderer, "run", side_effect=fake_run),
                mock.patch.object(renderer, "ffprobe_duration", return_value=5.0),
                mock.patch.object(renderer, "synthesize_azure", side_effect=renderer.TTSConfigurationError("missing credentials")),
            ]

            for patch in patches:
                patch.start()
            try:
                result = renderer.render_project(
                    "pid123",
                    storage_root,
                    {},
                    "output.mp4",
                )
            finally:
                for patch in reversed(patches):
                    patch.stop()

            self.assertTrue(result.exists())
            self.assertEqual(len(flite_outputs), 1)

    def test_intro_title_preserves_three_explicit_lines(self):
        wrapped = renderer._wrap_intro_title("Line One\nLine Two\nLine Three\nLine Four")
        self.assertEqual(wrapped, "Line One\nLine Two\nLine Three")

    def test_intro_title_draws_each_line_centered(self):
        with tempfile.TemporaryDirectory() as tmp:
            title_dir = Path(tmp)
            filter_graph = renderer._intro_title_drawtext_filter(
                title_lines=["Line One", "Line Two", "Line Three"],
                title_dir=title_dir,
                title_font_path=Path("/tmp/font.ttf"),
                input_label="card",
                output_label="v",
            )

        self.assertEqual(filter_graph.count("drawtext="), 3)
        self.assertEqual(filter_graph.count("x=(w-text_w)/2"), 3)
        self.assertIn("intro_title_1.txt", filter_graph)
        self.assertIn("[v]", filter_graph)

    def test_subject_title_card_does_not_add_brand_template_input(self):
        commands = []

        def fake_run(command, log=None):
            commands.append(command)
            output = Path(command[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"generated")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(renderer, "run", side_effect=fake_run):
            root = Path(tmp)
            background = root / "bible-title-card.png"
            background.write_bytes(b"image")
            renderer._create_intro_card_assets(
                background_image=background,
                leader_template=None,
                title="Genesis 1",
                work_dir=root / "work",
                output_dir=root / "output",
                fps=30,
                duration=1.0,
                title_font_path=root / "font.ttf",
                preset="medium",
                crf="18",
                log=None,
                thumbnail_enabled=False,
            )

        intro_command = commands[0]
        self.assertEqual(intro_command.count("-loop"), 1)
        self.assertIn("1:a", intro_command)
        self.assertNotIn("leader.png", " ".join(intro_command))
        self.assertNotIn("[leader]", intro_command[intro_command.index("-filter_complex") + 1])

    def test_default_image_durations_match_narration_length(self):
        durations = renderer._default_image_durations(
            scene={},
            image_count=2,
            min_shot=2.5,
            audio_duration=18.0,
        )

        self.assertEqual(len(durations), 2)
        self.assertGreaterEqual(sum(durations), 18.0)
        self.assertGreater(durations[0], 8.0)

    def test_fit_image_filter_preserves_source_aspect_ratio(self):
        filter_graph = renderer._fit_image_frame_filter("0:v", "v")

        self.assertIn("force_original_aspect_ratio=decrease", filter_graph)
        self.assertIn("pad=1920:1080:(ow-iw)/2:(oh-ih)/2", filter_graph)
        self.assertNotIn("crop=1920:1080", filter_graph)
        self.assertNotIn("zoompan", filter_graph)

    def test_motion_clip_plays_once_stretches_to_scene_and_pads_short_audio(self):
        commands = []

        def fake_run(cmd, log=None):
            commands.append(cmd)
            target = Path(cmd[-1])
            if target.suffix:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"output")

        with tempfile.TemporaryDirectory() as tmp:
            storage_root = _setup_project(Path(tmp), "none")
            project_root = storage_root / "projects" / "pid123"
            scenes_path = project_root / "input" / "scenes.json"
            spec = json.loads(scenes_path.read_text(encoding="utf-8"))
            spec["scenes"][0].update(
                {
                    "duration": 12.0,
                    "timeline": [{"image": "img001.png", "video": "scene_001.mp4"}],
                }
            )
            scenes_path.write_text(json.dumps(spec), encoding="utf-8")
            motion_path = project_root / "input" / "motion" / "scene_001.mp4"
            motion_path.parent.mkdir(parents=True, exist_ok=True)
            motion_path.write_bytes(b"motion")

            with mock.patch.object(renderer, "run", side_effect=fake_run), mock.patch.object(
                renderer, "ffprobe_duration", return_value=5.0
            ):
                renderer.render_project("pid123", storage_root, {}, "output.mp4")

        motion_command = next(command for command in commands if str(motion_path) in command)
        self.assertNotIn("-stream_loop", motion_command)
        self.assertIn("setpts=2.40000000*PTS", motion_command[motion_command.index("-filter_complex") + 1])
        audio_mux = next(command for command in commands if "-c:a" in command and "-shortest" in command)
        self.assertEqual(audio_mux[audio_mux.index("-af") + 1], "apad")
