import json
import math
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import video_captions as captions


PROFILE = {"ok": True, "engine": "faster-whisper", "model": "small", "device": "cpu", "compute_type": "int8"}


def response(value):
    result = Mock()
    result.json.return_value = value
    return result


def transcript(words=None, model="small"):
    words = words if words is not None else [{"start": 0.2, "end": 0.7, "word": "Welcome"}, {"start": 0.8, "end": 1.5, "word": " home."}]
    return {"engine": "faster-whisper", "model": model, "language": "en", "duration": 2, "text": "Welcome home.", "segments": [{"start": 0.2, "end": 1.5, "text": "Welcome home.", "words": words}]}


def has_ass_filter():
    if not shutil.which("ffmpeg"):
        return False
    result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return bool(re.search(r"\bass\s+V->V", result.stdout))


class VideoCaptionTests(unittest.TestCase):
    def test_script_uses_exact_source_words_and_measured_audio_offset(self):
        source = "Original narration, with its punctuation.\n\nA second sentence is preserved!"
        cues = captions.script_cues(source, 12.5, start=4.25)
        self.assertEqual(re.sub(r"\s+", "", source), re.sub(r"\s+", "", "".join(cue.text for cue in cues)))
        self.assertEqual(cues[0].start, 4.25)
        self.assertAlmostEqual(cues[-1].end, 16.75)
        self.assertTrue(all(left.end == right.start for left, right in zip(cues, cues[1:])))
        self.assertGreater(cues[0].end - cues[0].start, 0)

    def test_mandarin_script_keeps_characters_and_cjk_safe_line_widths(self):
        source = "欢迎参观这套休斯顿的住宅。宽敞的客厅与开放式厨房，适合家人朋友相聚。请联系黄洁预约看房。"
        cues = captions.script_cues(source, 9)
        self.assertEqual(source, "".join(cue.text for cue in cues))
        for cue in cues:
            lines = captions._wrapped(cue.text).splitlines()
            self.assertLessEqual(len(lines), 2)
            self.assertTrue(all(captions._width(line) <= 32 for line in lines))

    def test_script_rejects_nonfinite_duration_without_rewriting_text(self):
        for duration in [0, -1, math.inf, math.nan]:
            with self.subTest(duration=duration), self.assertRaises(captions.CaptionError):
                captions.script_cues("Original words.", duration)

    def test_whisper_word_times_leave_speech_gaps_blank(self):
        payload = transcript([
            {"start": 0.2, "end": 0.5, "word": "Welcome"},
            {"start": 0.6, "end": 1.1, "word": " home."},
            {"start": 3.0, "end": 3.5, "word": "Next"},
            {"start": 3.6, "end": 4.2, "word": " room."},
        ])
        records = captions._chunk_words(payload, 0, 0, 5, 0, 5)
        cues = captions._word_cues(records)
        self.assertEqual([cue.text for cue in cues], ["Welcome home.", "Next room."])
        self.assertEqual(cues[0].end, 1.1)
        self.assertEqual(cues[1].start, 3.0)

    def test_overlapping_chunk_boundary_deduplicates_only_shared_words(self):
        first = transcript([{"start": 27.7, "end": 28.2, "word": "boundary"}])
        second = transcript([{"start": 0.75, "end": 1.25, "word": "boundary"}, {"start": 1.4, "end": 2.2, "word": " next"}])
        records = captions._chunk_words(first, 0, 0, 28, 0, 57)
        records += captions._chunk_words(second, 27, 28, 56, 1, 57)
        cues = captions._word_cues(records)
        self.assertEqual(" ".join(cue.text for cue in cues), "boundary next")

    def test_boundary_timestamp_jitter_cannot_drop_or_duplicate_shared_speech(self):
        for previous, following in [((27.9, 28.2), (0.6, 0.9)), ((27.6, 27.9), (1.0, 1.3))]:
            with self.subTest(previous=previous, following=following):
                left = transcript([{"start": previous[0], "end": previous[1], "word": "boundary"}])
                right = transcript([{"start": following[0], "end": following[1], "word": "boundary"}])
                records = captions._chunk_words(left, 0, 0, 28, 0, 57)
                records += captions._chunk_words(right, 27, 28, 56, 1, 57)
                self.assertEqual(" ".join(cue.text for cue in captions._word_cues(records)), "boundary")

    def test_overlap_reconciliation_preserves_genuinely_repeated_words(self):
        left = transcript([{"start": 27.4, "end": 27.6, "word": "the"}, {"start": 27.8, "end": 28.0, "word": " the"}])
        right = transcript([{"start": 0.3, "end": 0.5, "word": "the"}, {"start": 0.8, "end": 1.0, "word": " the"}])
        records = captions._chunk_words(left, 0, 0, 28, 0, 57) + captions._chunk_words(right, 27, 28, 56, 1, 57)
        self.assertEqual(" ".join(cue.text for cue in captions._word_cues(records)), "the the")

    def test_wrapping_preserves_accented_word_spaces_and_fullwidth_punctuation(self):
        source = "Welcome to the café and garden."
        self.assertEqual(" ".join(captions._wrapped(source).split()), source)
        source = "这，" * 20
        self.assertTrue(all(captions._width(line) <= 32 for line in captions._wrapped(source).splitlines()))
        cues = captions.script_cues(source, 8)
        self.assertEqual(source, "".join(cue.text for cue in cues))
        self.assertTrue(all(len(captions._wrapped(cue.text).splitlines()) <= 2 for cue in cues))

    def test_known_intro_window_preserves_early_whisper_first_word(self):
        payload = transcript([{"start": 1.1, "end": 1.4, "word": "Office"}, {"start": 1.5, "end": 1.8, "word": " for"}, {"start": 1.9, "end": 2.2, "word": " lease."}])
        records = captions._chunk_words(payload, 0, 0, 5, 0, 5)
        cues = captions._word_cues(records, [(1.5, 5)])
        self.assertEqual(" ".join(cue.text for cue in cues), "Office for lease.")
        self.assertEqual(cues[0].start, 1.5)
        self.assertTrue(all(1.5 <= cue.start < cue.end <= 5 for cue in cues))

    def test_short_hyperframes_gap_always_splits_caption_group(self):
        payload = transcript([{"start": 1.7, "end": 1.95, "word": "Living"}, {"start": 1.96, "end": 2.2, "word": " room"}, {"start": 2.35, "end": 2.6, "word": "Kitchen"}, {"start": 2.7, "end": 3.1, "word": " next"}])
        records = captions._chunk_words(payload, 0, 0, 5, 0, 5)
        windows = [(1.5, 2), (2.3, 5)]
        cues = captions._word_cues(records, windows)
        self.assertEqual([cue.text for cue in cues], ["Living room", "Kitchen next"])
        self.assertEqual(cues[0].end, 2)
        self.assertGreaterEqual(cues[1].start, 2.3)
        self.assertTrue(all(any(cue.start >= lower and cue.end <= upper for lower, upper in windows) for cue in cues))

    def test_card_fence_never_duplicates_one_crossing_word(self):
        payload = transcript([{"start": 1.8, "end": 2.6, "word": "boundary"}])
        records = captions._chunk_words(payload, 0, 0, 5, 0, 5)
        cues = captions._word_cues(records, [(1, 2), (2.3, 5)])
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "boundary")
        self.assertGreaterEqual(cues[0].start, 2.3)
        self.assertLessEqual(cues[0].end, 5)

    def test_burn_validation_refuses_whole_cue_spanning_a_card(self):
        with self.assertRaisesRegex(captions.CaptionError, "title or transition"):
            captions._validated_cues([captions.CaptionCue(1.8, 2.6, "Spans a card")], 5, [(1, 2), (2.3, 5)])
        ass = captions._ass([captions.CaptionCue(1.533333, 3.066667, "Exact content.")], 1920, 1080)
        self.assertIn("0:00:01.54,0:00:03.06", ass)

    def test_segment_fallback_retains_long_transcript_text(self):
        text = "This exact segment contains several source details and must remain complete when word timestamps are unavailable."
        payload = {"text": text, "segments": [{"start": 0, "end": 8, "text": text}]}
        cues = captions._word_cues(captions._chunk_words(payload, 0, 0, 8, 0, 8))
        self.assertEqual(re.sub(r"\s+", "", text), re.sub(r"\s+", "", "".join(cue.text for cue in cues)))
        self.assertTrue(all(len(captions._wrapped(cue.text).splitlines()) <= 2 for cue in cues))

    def test_invalid_provider_times_and_untimed_speech_fail_clearly(self):
        values = [
            transcript([{"start": math.nan, "end": 1, "word": "bad"}]),
            transcript([{"start": 2, "end": 1, "word": "bad"}]),
            {"text": "Spoken words", "segments": []},
        ]
        for payload in values:
            with self.subTest(payload=payload), self.assertRaises(captions.CaptionError):
                captions._chunk_words(payload, 0, 0, 4, 0, 4)

    def test_ass_style_is_resolution_aware_and_text_cannot_inject_tags(self):
        cue = captions.CaptionCue(0.2, 1.2, r"Literal {\pos(0,0)} words\Nremain")
        landscape = captions._ass([cue], 1920, 1080)
        portrait = captions._ass([cue], 1080, 1920)
        self.assertIn("Noto Sans CJK SC", landscape)
        self.assertIn("PlayResX: 1920\nPlayResY: 1080", landscape)
        self.assertIn("PlayResX: 1080\nPlayResY: 1920", portrait)
        self.assertNotIn(r"{\pos(0,0)}", landscape)
        self.assertNotIn(r"words\Nremain", landscape)
        self.assertNotIn(",675,1", portrait)


class WhisperGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / "source.mp4"
        self.video.write_bytes(b"recorded-audio-v1")
        self.probe = patch.object(captions, "_probe", return_value={"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "2"}})
        self.mock_probe = self.probe.start(); self.addCleanup(self.probe.stop)
        self.media = patch.object(captions, "_run", side_effect=self.fake_media)
        self.mock_media = self.media.start(); self.addCleanup(self.media.stop)
        self.get = patch.object(captions.requests, "get", return_value=response({"stt": PROFILE}))
        self.mock_get = self.get.start(); self.addCleanup(self.get.stop)
        self.post = patch.object(captions.requests, "post", return_value=response(transcript()))
        self.mock_post = self.post.start(); self.addCleanup(self.post.stop)
        self.env = patch.dict("os.environ", {"VOICE_GATEWAY_URL": "http://fortress.lan:8133"})
        self.env.start(); self.addCleanup(self.env.stop)

    def fake_media(self, command, **kwargs):
        source = Path(command[command.index("-i") + 1])
        Path(command[-1]).write_bytes(source.read_bytes())
        return ""

    def test_gateway_uses_transcribe_task_and_caches_exact_audio_language_model(self):
        first = captions.transcribe_video(self.video, self.root / "work", "en-US")
        second = captions.transcribe_video(self.video, self.root / "work", "en-US")
        self.assertEqual(first, second)
        self.assertEqual(self.mock_post.call_count, 1)
        call = self.mock_post.call_args
        self.assertEqual(call.args[0], "http://fortress.lan:8133/control/api/voice/transcriptions")
        self.assertEqual(call.kwargs["data"], {"task": "transcribe", "word_timestamps": "true", "vad_filter": "true", "language": "en"})
        self.assertEqual(call.kwargs["timeout"], (5, 60))
        self.assertNotIn("8012", call.args[0])
        self.video.write_bytes(b"recorded-audio-v2")
        captions.transcribe_video(self.video, self.root / "work", "en-US")
        self.assertEqual(self.mock_post.call_count, 2)
        self.mock_get.return_value = response({"stt": {**PROFILE, "model": "medium"}})
        self.mock_post.return_value = response(transcript(model="medium"))
        captions.transcribe_video(self.video, self.root / "work", "en-US")
        self.assertEqual(self.mock_post.call_count, 3)
        captions.transcribe_video(self.video, self.root / "work", "zh-CN")
        self.assertEqual(self.mock_post.call_count, 4)
        self.assertEqual(self.mock_post.call_args.kwargs["data"]["language"], "zh")

    def test_long_audio_chunks_stay_within_gateway_limit_and_report_progress(self):
        self.mock_probe.return_value = {"streams": [{"codec_type": "audio"}], "format": {"duration": "57"}}
        progress = []
        captions.transcribe_video(self.video, self.root / "work", progress_cb=progress.append)
        self.assertEqual(self.mock_post.call_count, 3)
        for call in self.mock_media.call_args_list:
            command = call.args[0]
            if "-t" in command:
                self.assertLessEqual(float(command[command.index("-t") + 1]), 30)
        self.assertEqual(progress, [1 / 3, 2 / 3, 1.0])

    def test_metadata_model_missing_disables_cache_reuse(self):
        self.mock_get.return_value = response({"stt": {"ok": True, "engine": "faster-whisper"}})
        captions.transcribe_video(self.video, self.root / "work")
        captions.transcribe_video(self.video, self.root / "work")
        self.assertEqual(self.mock_post.call_count, 2)

    def test_known_content_windows_invalidate_old_transcript_clock_and_cache(self):
        first = captions.transcribe_video(self.video, self.root / "work", content_windows=[(0.5, 2)])
        second = captions.transcribe_video(self.video, self.root / "work", content_windows=[(0.5, 2)])
        self.assertEqual(first, second)
        self.assertEqual(self.mock_post.call_count, 1)
        captions.transcribe_video(self.video, self.root / "work", content_windows=[(0.7, 2)])
        self.assertEqual(self.mock_post.call_count, 2)
        captions.transcribe_video(self.video, self.root / "work", content_windows=[])
        self.assertEqual(self.mock_post.call_count, 3)

    def test_provider_error_fails_without_fabricated_or_template_captions(self):
        self.mock_post.side_effect = requests.ConnectionError("unreachable")
        with self.assertRaisesRegex(captions.CaptionError, "central voice gateway"):
            captions.transcribe_video(self.video, self.root / "work")
        self.assertFalse(list((self.root / "work/caption-cache").glob("*.json")))

    def test_silent_whisper_result_remains_empty_and_is_recorded(self):
        self.mock_post.return_value = response({"engine": "faster-whisper", "model": "small", "text": "", "segments": []})
        cues = captions.transcribe_video(self.video, self.root / "work")
        self.assertEqual(cues, [])
        result = next(json.loads(path.read_text()) for path in (self.root / "work/caption-cache").glob("*.json") if ".chunk-" not in path.name)
        self.assertTrue(result["noSpeech"])

    def test_non_whisper_central_engine_is_not_silently_used(self):
        self.mock_get.return_value = response({"stt": {**PROFILE, "engine": "other-stt"}})
        with self.assertRaisesRegex(captions.CaptionError, "not configured for Whisper"):
            captions.transcribe_video(self.video, self.root / "work")
        self.mock_post.assert_not_called()

    def test_cold_gateway_status_connection_retries_only_read_only_metadata(self):
        self.mock_get.side_effect = [requests.ConnectTimeout("cold network"), response({"stt": PROFILE})]
        with patch.object(captions.time, "sleep") as sleep:
            self.assertTrue(captions.transcribe_video(self.video, self.root / "work"))
        self.assertEqual(self.mock_get.call_count, 2)
        self.assertEqual(self.mock_post.call_count, 1)
        sleep.assert_called_once_with(1)
        self.assertTrue(all(call.args[0] == "http://fortress.lan:8133/control/api/voice" for call in self.mock_get.call_args_list))

    def test_unhealthy_or_rejected_gateway_does_not_retry_or_use_another_provider(self):
        unhealthy = response({"stt": {**PROFILE, "ok": False}})
        rejected = response({})
        rejected.raise_for_status.side_effect = requests.HTTPError("HTTP 503")
        for value in [unhealthy, rejected]:
            with self.subTest(value=value):
                self.mock_get.reset_mock()
                self.mock_get.return_value = value
                with patch.object(captions.time, "sleep") as sleep, self.assertRaises(captions.CaptionError):
                    captions.transcribe_video(self.video, self.root / "work")
                self.assertEqual(self.mock_get.call_count, 1)
                self.mock_post.assert_not_called()
                sleep.assert_not_called()

    def test_metadata_connection_retries_are_bounded_and_never_transcribe(self):
        self.mock_get.side_effect = requests.ConnectionError("gateway down")
        with patch.object(captions.time, "sleep") as sleep, self.assertRaises(captions.CaptionError):
            captions.transcribe_video(self.video, self.root / "work")
        self.assertEqual(self.mock_get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.mock_post.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required for encoded-caption verification")
class CaptionBurnTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="caption-engine-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1c3250:s=320x180:r=24:d=3", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source)], check=True)

    def packets(self, path):
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_packets", "-show_entries", "packet=data_hash", "-show_data_hash", "sha256", "-of", "json", str(path)], capture_output=True, text=True, check=True)
        return [packet["data_hash"] for packet in json.loads(result.stdout)["packets"]]

    @unittest.skipUnless(has_ass_filter(), "FFmpeg libass filter is required for actual burn verification")
    def test_burned_frames_sidecars_and_audio_are_verified(self):
        output = self.root / "listing.v2.mp4"
        cues = [captions.CaptionCue(0.2, 1.5, "Exact narration words."), captions.CaptionCue(1.5, 2.8, "这是中文房产介绍。")]
        report = captions.burn_captions(self.source, output, cues, 320, 180, sidecar_base=output.with_suffix(""), fps=24, preset="ultrafast", source_kind="script")
        self.assertEqual(report["source"], "narration-script")
        self.assertEqual(report["sourceKind"], "script")
        self.assertEqual(report["sourceSha256"], captions._hash(self.source))
        self.assertEqual(report["outputSha256"], captions._hash(output))
        self.assertEqual(self.packets(self.source), self.packets(output))
        self.assertTrue((self.root / "listing.v2.srt").exists())
        saved = json.loads((self.root / "listing.v2.captions.json").read_text())
        self.assertEqual(saved["cues"], [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in cues])
        result = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
        pixels = result.stdout
        white = sum(1 for index in range(0, len(pixels), 3) if min(pixels[index:index + 3]) > 210)
        self.assertGreater(white, 150, "Actual MP4 frame must contain burned white caption pixels")

    def test_failed_burn_preserves_previous_video_and_sidecars(self):
        output = self.root / "old.mp4"
        output.write_bytes(b"previous video")
        srt = self.root / "old.srt"
        srt.write_text("previous captions")
        original_run = captions._run

        def failing_run(command, **kwargs):
            if command[0] == "ffmpeg":
                raise captions.CaptionError("encoder failure")
            return original_run(command, **kwargs)

        with patch.object(captions, "_run", side_effect=failing_run), self.assertRaises(captions.CaptionError):
            captions.burn_captions(self.source, output, [captions.CaptionCue(0, 2, "Exact script.")], 320, 180)
        self.assertEqual(output.read_bytes(), b"previous video")
        self.assertEqual(srt.read_text(), "previous captions")
        self.assertFalse((self.root / "old.captions.json").exists())
