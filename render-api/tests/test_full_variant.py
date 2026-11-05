import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import full_variant as variant
from app import renderer
from app.caption_variants import derivative_content_windows


class FullVariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.file("output/master.mp4", b"original saved master")
        self.destination = self.file("output/variant.mp4", b"previous variant")
        self.raw = self.file("input/footage/phone.mov", b"raw HDR camera data")
        self.proxy = self.file("work/footage/clip_00.mp4", b"approved prepared proxy")
        self.work, self.input = self.root / "work", self.root / "input"
        self.state = {"title": "Office for Lease", "renderOptions": {"logoEnabled": False}}
        self.metadata = {"format": {"duration": "8"}, "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac"},
        ]}
        self.commands = []
        for module, name, value in ((variant, "p_work", self.work), (variant, "p_input", self.input),
                                     (variant, "read_project_state", self.state)):
            mock = patch.object(module, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def file(self, name, data=b"media"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def probe(self, path):
        if Path(path) == self.source:
            return self.metadata
        return {"format": {"duration": "8"}, "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920},
            {"codec_type": "audio", "codec_name": "aac"},
        ]}

    def fake_run(self, command, log=None):
        self.commands.append(command)
        target = Path(command[-1])
        if target.suffix.lower() in {".mp4", ".jpg"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"complete staged output")

    def mapping(self):
        return [{"sourceStart": 2.0, "sourceEnd": 8.0, "kind": "video", "assetPath": self.raw,
                 "assetStart": 0.0, "assetTimeScale": 0.98, "proxyPath": self.proxy,
                 "proxyTimeScale": 1.0, "sceneIndex": 0, "sceneTitle": "Office",
                 "fingerprint": {"size": self.raw.stat().st_size, "mtimeNs": str(self.raw.stat().st_mtime_ns)}}]

    def render(self, *, run=None, mode="fill"):
        with patch.object(variant, "_probe", side_effect=self.probe), patch.object(variant, "native_source_map", return_value=self.mapping()), patch.object(
            variant, "_known_card", return_value=None
        ), patch.object(variant, "analyze_framing", side_effect=lambda _path, _start, length: [{"start": 0, "duration": length, "crop": None}]), patch(
            "app.vertical_framing.analyze_fill_focus", return_value=[(0, 0.5), (1, 0.65)]
        ):
            return variant.render_full_variant(self.source, self.destination, project_id="project", framing_mode=mode,
                                               work_root=self.root / "qa", run_command=run or self.fake_run)

    def test_native_body_uses_approved_proxy_and_one_continuous_aac_copy(self):
        self.render()
        video_inputs = [command[command.index("-i") + 1] for command in self.commands[:-1]]
        self.assertIn(str(self.proxy), video_inputs)
        self.assertNotIn(str(self.raw), video_inputs)
        final = self.commands[-1]
        self.assertEqual(final[final.index("-c:a") + 1], "copy")
        self.assertIn("1:a?", final)
        report = json.loads(self.destination.with_suffix(".framing.json").read_text())
        self.assertEqual(report["layout"], "fill")
        self.assertEqual(report["frameCount"], 240)
        self.assertEqual(report["audioMode"], "aac-stream-copy")
        self.assertEqual(report["sections"][-1]["kind"], "verified-prepared-proxy")
        self.assertNotIn("boxblur", report["sections"][-1]["filterGraph"])
        self.assertEqual(self.source.read_bytes(), b"original saved master")

    def test_fit_remains_an_explicit_full_picture_choice(self):
        self.render(mode="fit")
        report = json.loads(self.destination.with_suffix(".framing.json").read_text())
        self.assertEqual(report["layout"], "fit")
        self.assertIn("boxblur", report["sections"][-1]["filterGraph"])

    def test_changed_proxy_does_not_replace_previous_variant(self):
        def changed(command, log=None):
            self.fake_run(command, log)
            self.proxy.write_bytes(b"another render changed this proxy")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.render(run=changed)
        self.assertEqual(self.destination.read_bytes(), b"previous variant")
        self.assertFalse(self.destination.with_suffix(".framing.json").exists())

    def test_encode_failure_preserves_previous_video_and_framing_report(self):
        report = self.destination.with_suffix(".framing.json")
        report.write_bytes(b"old report")
        with self.assertRaisesRegex(RuntimeError, "encode failed"):
            self.render(run=lambda *_args: (_ for _ in ()).throw(RuntimeError("encode failed")))
        self.assertEqual(self.destination.read_bytes(), b"previous variant")
        self.assertEqual(report.read_bytes(), b"old report")

    def test_saved_card_copy_keeps_every_listing_fact_and_the_broker_colon(self):
        lines = ["OFFICE FOR LEASE", "12238 QUEENSTON BLVD, UNIT K", "EXISTING EYE-CARE SETUP", "$2,500/MONTH · WATER INCLUDED"]
        state = {"introLines": lines, "hyperframes": {"leadCardRight1": "Brokerage: LeCrown Properties", "leadCardRight2": "Broker: Jie Huang"}}
        copy = variant._copy(state, {}, {"kind": "opening"})
        self.assertEqual(copy["headline"], lines[0])
        self.assertEqual(copy["supporting"], " · ".join(lines[1:]))
        self.assertEqual(copy["contact"]["right2"], "Broker: Jie Huang")

    def test_unverified_gap_is_never_invented_as_a_vertical_title_card(self):
        self.file("work/hyperframes_intro.mp4")
        with patch.object(variant, "_card_matches", return_value=False):
            result = variant._known_card(self.source, {"sourceStart": 0, "sourceEnd": 2}, self.mapping(), {"useIntro": True}, self.work)
        self.assertIsNone(result)

    def caption_framing_report(self, path):
        report = {"version": 1, "fps": 30, "frameCount": 240, "outputSha256": variant._identity(path)["sha256"],
                  "sections": [
                      {"kind": "vertical-opening", "sourceStart": 0, "sourceEnd": 2, "outputStartFrame": 0, "outputEndFrame": 60},
                      {"kind": "verified-prepared-proxy", "sourceStart": 2, "sourceEnd": 4, "outputStartFrame": 60, "outputEndFrame": 120},
                      {"kind": "vertical-transition", "sourceStart": 4, "sourceEnd": 4.4, "outputStartFrame": 120, "outputEndFrame": 132},
                      {"kind": "master-fallback", "sourceStart": 4.4, "sourceEnd": 8, "outputStartFrame": 132, "outputEndFrame": 240},
                  ]}
        path.with_suffix(".framing.json").write_text(json.dumps(report))

    def test_caption_windows_exclude_declared_cards_but_preserve_unknown_master_speech(self):
        framed = self.file("work/framed.mp4", b"newly framed video")
        self.caption_framing_report(framed)
        windows = variant.framing_caption_windows(framed)
        self.assertEqual(windows, [(2.0, 4.0, 2.0), (4.4, 8.0, 4.4)])
        self.assertEqual(derivative_content_windows(SimpleNamespace(content_windows=None), windows, 8), [(2.0, 4.0), (4.4, 8.0)])
        saved = json.loads(framed.with_suffix(".framing.json").read_text())
        self.assertEqual(saved["captionBodyWindows"], [[2.0, 4.0, 2.0], [4.4, 8.0, 4.4]])

    def test_old_recorded_master_without_caption_manifest_gets_new_graphic_fences(self):
        clean_source = self.file("work/uncaptioned_master.mp4", b"clean recorded source")
        self.assertFalse(self.source.with_suffix(".captions.json").exists())
        prepared = SimpleNamespace(clean_source=clean_source, source_type="whisper", content_windows=None, cues=[])
        captured = {}
        def full(_source, target, **kwargs):
            target.write_bytes(b"new uncaptioned variant")
            self.caption_framing_report(target)
            if kwargs.get("before_publish"):
                kwargs["before_publish"]({target: variant._identity(target), target.with_suffix(".framing.json"): variant._identity(target.with_suffix(".framing.json"))})
            return target
        def captions(_prepared, framed, target, **kwargs):
            captured["windows"] = kwargs["windows"]
            captured["content"] = derivative_content_windows(_prepared, kwargs["windows"], 8)
            staged = target.with_name("captioned-staged.mp4")
            staged.write_bytes(b"captioned portrait video")
            staged.replace(target)
        with patch("app.caption_variants.prepare_derivative_captions", return_value=prepared), patch("app.caption_variants.apply_derivative_captions", side_effect=captions), patch.object(
            variant, "render_full_variant", side_effect=full
        ), patch("app.storage.p_work", return_value=self.work):
            renderer.render_tiktok_variant(self.source, self.destination, captions_enabled=True, project_id="project")
        self.assertEqual(captured["windows"], [(2.0, 4.0, 2.0), (4.4, 8.0, 4.4)])
        self.assertEqual(captured["content"], [(2.0, 4.0), (4.4, 8.0)])
        self.assertEqual(self.source.read_bytes(), b"original saved master")

    def test_transaction_restores_all_sidecars_and_retained_clean_after_failure(self):
        expected = {self.destination: b"previous variant"}
        for suffix in (".media.json", ".framing.json", ".captions.json", ".srt", ".ass"):
            path = self.destination.with_suffix(suffix)
            path.write_bytes(("old " + suffix).encode())
            expected[path] = path.read_bytes()
        clean = self.file("work/uncaptioned_variant.mp4", b"previous retained clean")
        expected[clean] = clean.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "after caption publication"):
            with variant.variant_output_transaction(self.destination, "project") as transaction:
                for path in expected:
                    replacement = path.with_name(path.name + ".replacement")
                    replacement.write_bytes(b"newly published data")
                    replacement.replace(path)
                transaction.record(list(expected))
                raise RuntimeError("after caption publication")
        self.assertEqual({path: path.read_bytes() for path in expected}, expected)

    def test_external_replacement_before_our_publication_is_never_rolled_back(self):
        with self.assertRaisesRegex(RuntimeError, "before our publication"):
            with variant.variant_output_transaction(self.destination, "project"):
                foreign = self.destination.with_name("external.mp4")
                foreign.write_bytes(b"another writer's new variant")
                foreign.replace(self.destination)
                raise RuntimeError("before our publication")
        self.assertEqual(self.destination.read_bytes(), b"another writer's new variant")

    def test_an_external_update_during_encoding_is_not_overwritten(self):
        def concurrent(command, log=None):
            self.fake_run(command, log)
            foreign = self.destination.with_name("external.mp4")
            foreign.write_bytes(b"concurrently updated output")
            foreign.replace(self.destination)
        with self.assertRaisesRegex(RuntimeError, "newer output was preserved"):
            self.render(run=concurrent)
        self.assertEqual(self.destination.read_bytes(), b"concurrently updated output")

    def test_caption_manifest_failure_rolls_back_caption_output_and_retained_clean(self):
        clean_source = self.file("work/uncaptioned_master.mp4", b"clean source")
        self.file("work/uncaptioned_variant.mp4", b"old clean variant")
        self.destination.with_suffix(".srt").write_bytes(b"old subtitles")
        prepared = SimpleNamespace(clean_source=clean_source)
        def full(_source, target, **_kwargs):
            target.write_bytes(b"new uncaptioned variant")
            self.caption_framing_report(target)
            return target
        def captions(_prepared, _framed, target, **_kwargs):
            new = target.with_name("new.mp4")
            new.write_bytes(b"new captioned variant")
            new.replace(target)
            target.with_suffix(".srt").write_bytes(b"new subtitles")
            (self.work / "uncaptioned_variant.mp4").write_bytes(b"new retained clean")
        with patch("app.caption_variants.prepare_derivative_captions", return_value=prepared), patch("app.caption_variants.apply_derivative_captions", side_effect=captions), patch.object(
            variant, "render_full_variant", side_effect=full
        ) as render, patch.object(variant, "carry_framing_manifest", side_effect=RuntimeError("manifest failed")), patch("app.storage.p_work", return_value=self.work):
            with self.assertRaisesRegex(RuntimeError, "manifest failed"):
                renderer.render_tiktok_variant(self.source, self.destination, captions_enabled=True, project_id="project", framing_mode="fill")
        self.assertEqual(self.destination.read_bytes(), b"previous variant")
        self.assertEqual(self.destination.with_suffix(".srt").read_bytes(), b"old subtitles")
        self.assertEqual((self.work / "uncaptioned_variant.mp4").read_bytes(), b"old clean variant")
        self.assertEqual(render.call_args.kwargs["project_id"], "project")
        self.assertEqual(render.call_args.kwargs["framing_mode"], "fill")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Actual full portrait encode")
class FullVariantEncodingTests(unittest.TestCase):
    def test_real_four_second_fill_keeps_master_and_aac_packets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / "master.mp4", root / "variant.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)], check=True, capture_output=True)
            before = variant._identity(source)
            variant.render_full_variant(source, target, framing_mode="fill", work_root=root / "proof")
            report = json.loads(target.with_suffix(".framing.json").read_text())
            self.assertEqual(report["audioMode"], "aac-stream-copy")
            self.assertEqual(report["layout"], "fill")
            self.assertTrue(all("boxblur" not in section["filterGraph"] for section in report["sections"]))
            self.assertEqual(variant._identity(source), before)
            metadata = variant._probe(target)
            video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            def packets(path):
                return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(packets(source), packets(target))
