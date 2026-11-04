"""Timed speech captions shared by footage and narrated-photo renders.

Recorded audio is transcribed only through the workspace voice gateway. Photo
narration keeps its original script; it is never rewritten or transcribed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from typing import Callable

import requests


CAPTION_VERSION = 3
CHUNK_SECONDS = 28.0
CHUNK_OVERLAP_SECONDS = 1.0


class CaptionError(RuntimeError):
    """Requested speech captions could not be produced safely."""


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    text: str


def _run(command: list[str], *, cwd: Path | None = None, log=None) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip()[-3000:]
        if log:
            log(detail)
        raise CaptionError(f"Caption media processing failed: {detail}")
    return result.stdout


def _probe(path: Path) -> dict:
    try:
        return json.loads(_run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
    except (ValueError, OSError) as exc:
        raise CaptionError("Unable to inspect caption media") from exc


def _duration(probe: dict) -> float:
    try:
        value = float(probe.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        value = 0
    if not math.isfinite(value) or value <= 0:
        raise CaptionError("Caption media has no valid duration")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _width(value: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def _script_parts(text: str) -> list[str]:
    """Preserve source characters and punctuation, splitting on safe boundaries."""
    spans = []
    for match in re.finditer(r"[\u3400-\u9fff]|[^\s\u3400-\u9fff]+", text):
        begin = match.start()
        while _width(text[begin:match.end()]) > 64:
            end = begin + 1
            while end < match.end() and _width(text[begin:end + 1]) <= 64:
                end += 1
            spans.append((begin, end))
            begin = end
        spans.append((begin, match.end()))
    groups: list[str] = []
    start = 0
    for index, (_, end) in enumerate(spans):
        candidate = text[spans[start][0]:end]
        if index > start and (_width(_text(candidate)) > 64 or len(_wrapped(_text(candidate)).splitlines()) > 2):
            groups.append(_text(text[spans[start][0]:spans[index - 1][1]]))
            start = index
        current = _text(text[spans[start][0]:end])
        if re.search(r"[.!?。！？]$", current) and _width(current) >= 12:
            groups.append(current)
            start = index + 1
    if start < len(spans):
        groups.append(_text(text[spans[start][0]:spans[-1][1]]))
    return [part for part in groups if part]


def script_cues(text: str, duration: float, start: float = 0.0) -> list[CaptionCue]:
    """Allocate the exact narration script over its measured spoken duration.

    This is proportional script timing, not claimed word-level forced alignment.
    The renderer supplies each narration clip's actual duration and timeline offset.
    """
    duration, start = float(duration), float(start)
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(start) or start < 0:
        raise CaptionError("Narration captions require a finite positive duration and nonnegative start")
    parts = _script_parts(str(text or ""))
    if not parts:
        return []
    weights = [max(1, len(re.findall(r"[\u3400-\u9fff]|\b[\w'-]+\b", part))) for part in parts]
    total = sum(weights)
    elapsed, result = 0.0, []
    for index, (part, weight) in enumerate(zip(parts, weights)):
        end = duration if index == len(parts) - 1 else elapsed + duration * weight / total
        result.append(CaptionCue(start + elapsed, start + end, part))
        elapsed = end
    return result


def _language(value: str | None) -> str | None:
    value = str(value or "").strip().lower().replace("_", "-")
    if not value or value in {"auto", "und"}:
        return None
    if value.startswith(("zh", "cmn")):
        return "zh"
    return value.split("-", 1)[0]


def _gateway() -> tuple[str, str, dict[str, str]]:
    base = os.getenv("VOICE_GATEWAY_URL", "http://fortress.lan:8133").rstrip("/")
    if not base:
        raise CaptionError("Configure the central voice gateway before transcribing video captions")
    if base.endswith("/transcriptions"):
        base = base[:-len("/transcriptions")]
    elif not base.endswith("/control/api/voice"):
        base += "/control/api/voice"
    token = os.getenv("VOICE_GATEWAY_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return base, base + "/transcriptions", headers


def _provider_profile(base: str, headers: dict[str, str]) -> dict:
    for attempt in range(3):
        try:
            response = requests.get(base, headers=headers, timeout=(5, 15))
            response.raise_for_status()
            stt = response.json().get("stt", {})
            break
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise CaptionError("Whisper status is unavailable through the central voice gateway") from exc
            # Only warm up the read-only, configured gateway connection. A
            # transcription POST is never replayed and no alternate route is used.
            time.sleep(1)
        except (requests.RequestException, ValueError, AttributeError) as exc:
            raise CaptionError("Whisper status is unavailable through the central voice gateway") from exc
    if not stt.get("ok"):
        raise CaptionError("Whisper is unavailable through the central voice gateway")
    if stt.get("engine") and "whisper" not in str(stt["engine"]).lower():
        raise CaptionError("The central voice gateway is not configured for Whisper transcription")
    return {name: str(stt.get(name) or "") for name in ("engine", "model", "device", "compute_type")}


def _finite(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CaptionError(f"Whisper returned invalid {name}") from exc
    if not math.isfinite(number):
        raise CaptionError(f"Whisper returned nonfinite {name}")
    return number


def _join_words(words: list[dict]) -> str:
    text = ""
    for record in words:
        word = str(record["text"])
        if text and not word[:1].isspace():
            cjk_edge = any("\u3400" <= char <= "\u9fff" for char in (text[-1], word[0]))
            if not cjk_edge and text[-1:].isalnum() and word[:1].isalnum():
                text += " "
            elif text[-1:] in ".,;:!?" and word[:1].isalpha() and not cjk_edge:
                text += " "
        text += word
    return _text(text)


def _chunk_words(payload: dict, offset: float, core_start: float, core_end: float, chunk: int, duration: float) -> list[dict]:
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments and str(payload.get("text") or "").strip():
        raise CaptionError("Whisper did not return timed transcript segments")
    records = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise CaptionError("Whisper returned an invalid transcript segment")
        words = segment.get("words")
        if isinstance(words, list) and words:
            entries = words
        else:
            segment_start = _finite(segment.get("start"), "segment start")
            segment_end = _finite(segment.get("end"), "segment end")
            if segment_end <= segment_start:
                if str(segment.get("text") or "").strip():
                    raise CaptionError("Whisper returned a segment without usable timestamps")
                continue
            entries = [{"start": cue.start, "end": cue.end, "word": cue.text} for cue in script_cues(str(segment.get("text") or ""), segment_end - segment_start, start=segment_start)]
        for word in entries:
            if not isinstance(word, dict):
                raise CaptionError("Whisper returned an invalid word record")
            text = str(word.get("word", word.get("text", "")))
            if not text.strip():
                continue
            start = offset + _finite(word.get("start"), "word start")
            end = offset + _finite(word.get("end"), "word end")
            if end < start:
                raise CaptionError("Whisper returned reversed word timestamps")
            start, end = max(0.0, start), min(duration, end)
            if start >= duration or end <= 0:
                continue
            end = min(duration, max(start + 0.02, end))
            records.append({"start": start, "end": end, "text": text, "chunk": chunk, "coreStart": core_start})
    return records


def _word_key(word: dict) -> str:
    return re.sub(r"\W+", "", word["text"]).casefold()


def _content_windows(windows, duration: float) -> list[tuple[float, float]] | None:
    if windows is None:
        return None
    result = []
    for window in windows:
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            raise CaptionError("Caption content windows must contain start and end timestamps")
        start, end = _finite(window[0], "content start"), _finite(window[1], "content end")
        if start < 0 or end <= start or end > duration + 0.25:
            raise CaptionError("Caption content windows are outside the video timeline")
        result.append((start, min(end, duration)))
    result.sort()
    if any(start < previous_end - 0.001 for (_, previous_end), (start, _) in zip(result, result[1:])):
        raise CaptionError("Caption content windows overlap")
    return result


def _fence_words(records: list[dict], windows: list[tuple[float, float]] | None) -> list[dict]:
    if windows is None:
        return records
    result = []
    for word in records:
        choices = []
        for index, (start, end) in enumerate(windows):
            overlap = max(0.0, min(word["end"], end) - max(word["start"], start))
            distance = max(start - word["end"], word["start"] - end, 0.0)
            if overlap > 0 or distance <= 0.6 + 1e-9:
                midpoint = (word["start"] + word["end"]) / 2
                choices.append((overlap, -distance, -abs(midpoint - (start + end) / 2), -index))
        if not choices:
            # Words well inside a known silent card are not spoken-content cues.
            continue
        index = -max(choices)[3]
        lower, upper = windows[index]
        start, end = word["start"], word["end"]
        if end <= lower:
            start, end = lower, min(upper, lower + max(0.02, end - start))
        elif start >= upper:
            start, end = max(lower, upper - max(0.02, end - start)), upper
        else:
            start, end = max(start, lower), min(end, upper)
        if end > start:
            result.append({**word, "start": start, "end": end, "window": index})
    return result


def _reconcile_words(records: list[dict]) -> list[dict]:
    """Join padded chunk sequences before selecting a boundary.

    Independent Whisper windows can assign the same word to opposite sides of
    a core boundary. Midpoint filtering before reconciliation loses that word.
    Match overlap text first, then switch windows once at a shared word.
    """
    chunks: dict[int, list[dict]] = {}
    for word in records:
        chunks.setdefault(word["chunk"], []).append(word)
    merged: list[dict] = []
    for index in sorted(chunks):
        incoming = chunks[index]
        if not merged:
            merged = list(incoming)
            continue
        boundary = float(incoming[0].get("coreStart", index * CHUNK_SECONDS))
        lower, upper = boundary - CHUNK_OVERLAP_SECONDS - 0.25, boundary + CHUNK_OVERLAP_SECONDS + 0.25
        left = [(position, _word_key(word)) for position, word in enumerate(merged) if word["end"] >= lower and word["start"] <= upper and _word_key(word)]
        right = [(position, _word_key(word)) for position, word in enumerate(incoming) if word["end"] >= lower and word["start"] <= upper and _word_key(word)]
        choices = []
        matcher = SequenceMatcher(None, [word for _, word in left], [word for _, word in right], autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                left_index, right_index = left[block.a + offset][0], right[block.b + offset][0]
                previous, following = merged[left_index], incoming[right_index]
                left_mid = (previous["start"] + previous["end"]) / 2
                right_mid = (following["start"] + following["end"]) / 2
                if abs(left_mid - right_mid) <= 2 * CHUNK_OVERLAP_SECONDS + 0.25:
                    choices.append((block.size, -abs((left_mid + right_mid) / 2 - boundary), -abs(left_mid - right_mid), left_index, right_index))
        if choices:
            _, _, _, left_index, right_index = max(choices)
            merged = merged[:left_index + 1] + incoming[right_index + 1:]
        else:
            # Disjoint speech on either side of a pause needs no text anchor.
            # Keep crossing words rather than discarding both by midpoint.
            merged = [word for word in merged if word["start"] < boundary] + [word for word in incoming if word["end"] > boundary]
    return merged


def _word_cues(records: list[dict], content_windows: list[tuple[float, float]] | None = None) -> list[CaptionCue]:
    records = _fence_words(_reconcile_words(records), content_windows)
    expanded = []
    for word in records:
        if len(_wrapped(_text(word["text"])).splitlines()) > 2:
            expanded.extend({**word, "start": cue.start, "end": cue.end, "text": cue.text} for cue in script_cues(word["text"], word["end"] - word["start"], start=word["start"]))
        else:
            expanded.append(word)
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in expanded:
        if current and (word.get("window") != current[-1].get("window") or word["start"] - current[-1]["end"] > 0.65 or word["end"] - current[0]["start"] > 3.5 or _width(_join_words(current + [word])) > 64 or len(_wrapped(_join_words(current + [word])).splitlines()) > 2):
            groups.append(current)
            current = []
        current.append(word)
        if re.search(r"[.!?。！？]$", word["text"].strip()) and _width(_join_words(current)) >= 12:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    cues = []
    for group in groups:
        start, end = group[0]["start"], max(word["end"] for word in group)
        if cues and start < cues[-1].end:
            start = cues[-1].end
        if end <= start:
            raise CaptionError("Whisper returned overlapping transcript cues without usable timing")
        cues.append(CaptionCue(start, end, _join_words(group)))
    return cues


def transcribe_video(video_path: Path, work_dir: Path, language: str | None = None, progress_cb: Callable[[float], None] | None = None, *, content_windows: list[tuple[float, float]] | None = None) -> list[CaptionCue]:
    """Transcribe the assembled recorded audio with private, timestamped Whisper."""
    video_path, work_dir = Path(video_path), Path(work_dir)
    cache_dir = work_dir / "caption-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    probe = _probe(video_path)
    duration = _duration(probe)
    content_windows = _content_windows(content_windows, duration)
    if not any(stream.get("codec_type") == "audio" for stream in probe.get("streams", [])):
        _atomic_json(cache_dir / "no-audio.json", {"version": CAPTION_VERSION, "source": "whisper", "noSpeech": True, "reason": "source-has-no-audio"})
        if progress_cb:
            progress_cb(1.0)
        return []
    language = _language(language)
    base, endpoint, headers = _gateway()
    profile = _provider_profile(base, headers)
    with tempfile.TemporaryDirectory(prefix="caption-audio-", dir=work_dir) as temporary:
        scratch = Path(temporary)
        audio = scratch / "source.wav"
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
        audio_hash = _hash(audio)
        identity = {"version": CAPTION_VERSION, "audioSha256": audio_hash, "language": language or "auto", "provider": profile, "contentWindows": None if content_windows is None else [list(window) for window in content_windows]}
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = cache_dir / (signature + ".json")
        # Missing model metadata disables cache reuse rather than inventing a
        # provider identity. The transcription can still be requested normally.
        cache_enabled = bool(profile["engine"] and profile["model"])
        if cache_enabled and cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if saved.get("identity") == identity:
                    cues = _validated_cues([CaptionCue(**item) for item in saved["cues"]], duration, content_windows)
                    if progress_cb:
                        progress_cb(1.0)
                    return cues
            except (ValueError, KeyError, TypeError, CaptionError):
                pass
        count = max(1, math.ceil(duration / CHUNK_SECONDS))
        records: list[dict] = []
        responses = []
        for index in range(count):
            core_start, core_end = index * CHUNK_SECONDS, min(duration, (index + 1) * CHUNK_SECONDS)
            padded_start = max(0, core_start - CHUNK_OVERLAP_SECONDS)
            padded_end = min(duration, core_end + CHUNK_OVERLAP_SECONDS)
            chunk_cache = cache_dir / f"{signature}.chunk-{index}.json"
            payload = None
            if cache_enabled and chunk_cache.exists():
                try:
                    candidate = json.loads(chunk_cache.read_text(encoding="utf-8"))
                    if candidate.get("identity") == identity:
                        cached_response = candidate.get("response")
                        if isinstance(cached_response, dict):
                            _chunk_words(cached_response, padded_start, core_start, core_end, index, duration)
                            payload = cached_response
                except (ValueError, CaptionError):
                    pass
            if not isinstance(payload, dict):
                chunk = scratch / f"chunk-{index}.wav"
                _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{padded_start:.6f}", "-i", str(audio), "-t", f"{padded_end - padded_start:.6f}", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(chunk)])
                fields = {"task": "transcribe", "word_timestamps": "true", "vad_filter": "true"}
                if language:
                    fields["language"] = language
                try:
                    with chunk.open("rb") as handle:
                        response = requests.post(endpoint, data=fields, files={"file": (chunk.name, handle, "audio/wav")}, headers=headers, timeout=(5, 60))
                    response.raise_for_status()
                    payload = response.json()
                except requests.RequestException as exc:
                    raise CaptionError(f"Whisper transcription failed through the central voice gateway on chunk {index + 1} of {count}") from exc
                except ValueError as exc:
                    raise CaptionError("Whisper returned invalid transcription JSON") from exc
                if not isinstance(payload, dict):
                    raise CaptionError("Whisper returned invalid transcription data")
                engine = str(payload.get("engine") or profile["engine"])
                if "whisper" not in engine.lower():
                    raise CaptionError("The voice gateway did not confirm a Whisper transcript")
                if profile["model"] and payload.get("model") and str(payload["model"]) != profile["model"]:
                    raise CaptionError("The Whisper model changed during transcription; retry with the current model")
                # Parse before caching so malformed provider data cannot become
                # a reusable successful transcript.
                _chunk_words(payload, padded_start, core_start, core_end, index, duration)
                if cache_enabled:
                    _atomic_json(chunk_cache, {"identity": identity, "response": payload})
            records.extend(_chunk_words(payload, padded_start, core_start, core_end, index, duration))
            responses.append({key: payload.get(key) for key in ("engine", "model", "language", "duration", "text")})
            if progress_cb:
                progress_cb((index + 1) / count)
        cues = _validated_cues(_word_cues(records, content_windows), duration, content_windows)
        result = {"identity": identity, "source": "whisper", "noSpeech": not cues, "cues": [asdict(cue) for cue in cues], "responses": responses}
        _atomic_json(cache, result)
        return cues


def _validated_cues(cues: list[CaptionCue], duration: float, content_windows: list[tuple[float, float]] | None = None) -> list[CaptionCue]:
    result = []
    previous_end = 0.0
    for cue in cues:
        start, end = _finite(cue.start, "cue start"), _finite(cue.end, "cue end")
        if start < 0 or end <= start or start < previous_end - 0.001:
            raise CaptionError("Caption cues have invalid or overlapping timestamps")
        if start >= duration or end > duration + 0.25:
            raise CaptionError("Caption cues extend beyond the video")
        if content_windows is not None and not any(start >= lower - 0.001 and end <= upper + 0.001 for lower, upper in content_windows):
            raise CaptionError("Caption cues extend into a title or transition card")
        text = _text(cue.text)
        if not text:
            raise CaptionError("Caption cues contain empty text")
        result.append(CaptionCue(start, min(end, duration), text))
        previous_end = min(end, duration)
    return result


def _wrapped(text: str) -> str:
    lines, current = [], ""
    previous_end = 0
    for match in re.finditer(r"[\u3400-\u9fff]|[^\s\u3400-\u9fff]+", text):
        token = match.group()
        separated = match.start() > previous_end
        previous_end = match.end()
        while _width(token) > 32:
            if current:
                lines.append(current)
                current = ""
            split = 1
            while split < len(token) and _width(token[:split + 1]) <= 32:
                split += 1
            lines.append(token[:split])
            token = token[split:]
        if not token:
            continue
        separator = " " if current and separated else ""
        if current and _width(current + separator + token) > 32:
            lines.append(current)
            current = token
        else:
            current += separator + token
    if current:
        lines.append(current)
    return "\n".join(lines)


def _ass_escape(text: str) -> str:
    # ASS override tags require braces; remove only control syntax while
    # preserving the visible text. A literal backslash must not become \N.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def _ass_time(value: float) -> str:
    ticks = max(0, round(value * 100))
    hours, ticks = divmod(ticks, 360000)
    minutes, ticks = divmod(ticks, 6000)
    seconds, ticks = divmod(ticks, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{ticks:02d}"


def _srt_time(value: float) -> str:
    ticks = max(0, round(value * 1000))
    hours, ticks = divmod(ticks, 3600000)
    minutes, ticks = divmod(ticks, 60000)
    seconds, ticks = divmod(ticks, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ticks:03d}"


def _ass(cues: list[CaptionCue], width: int, height: int) -> str:
    fontsize = max(20, round(min(width, height) * (0.049 if height > width else 0.043)))
    margin_x, margin_y = max(24, round(width * 0.06)), max(24, round(height * (0.13 if height > width else 0.07)))
    outline = max(2, round(fontsize * 0.11))
    # Quantize inward so ASS's centisecond clock cannot put a caption on the
    # preceding/following card frame at a fractional-frame boundary.
    events = "".join(f"Dialogue: 0,{_ass_time(math.ceil(cue.start * 100 - 1e-8) / 100)},{_ass_time(math.floor(cue.end * 100 + 1e-8) / 100)},Speech,,0,0,0,,{_ass_escape(_wrapped(cue.text))}\n" for cue in cues)
    return (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Speech,Noto Sans CJK SC,{fontsize},&H00FFFFFF,&H00FFFFFF,&H70000000,&H70000000,-1,0,0,0,100,100,0,0,3,{outline},0,2,{margin_x},{margin_x},{margin_y},1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + events
    )


def _publish_bundle(pairs: list[tuple[Path, Path]], scratch: Path) -> None:
    backups, published = {}, []
    for index, (_, target) in enumerate(pairs):
        if target.exists():
            backup = scratch / f"previous-{index}"
            try:
                os.link(target, backup)
            except OSError:
                shutil.copyfile(target, backup)
            backups[target] = backup
    try:
        for staged, target in pairs:
            os.replace(staged, target)
            published.append(target)
    except OSError:
        for target in reversed(published):
            if target in backups:
                os.replace(backups[target], target)
            else:
                target.unlink(missing_ok=True)
        raise


def burn_captions(input_path: Path, output_path: Path, cues: list[CaptionCue], width: int, height: int, sidecar_base: Path | None = None, fps: float | None = None, *, preset: str = "medium", crf: int = 18, log=None, source: str = "timed-cues", source_kind: str | None = None, content_windows: list[tuple[float, float]] | None = None) -> dict:
    """Burn validated cues and atomically publish the MP4 and transcript sidecars."""
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise CaptionError("Caption video dimensions must be positive")
    original = _probe(input_path)
    duration = _duration(original)
    content_windows = _content_windows(content_windows, duration)
    source_hash = _hash(input_path)
    if source_kind is not None:
        source = {"whisper": "whisper", "script": "narration-script"}.get(source_kind, source_kind)
    else:
        source_kind = "script" if source == "narration-script" else source
    videos = [stream for stream in original.get("streams", []) if stream.get("codec_type") == "video"]
    audio = [stream for stream in original.get("streams", []) if stream.get("codec_type") == "audio"]
    if not videos or (videos[0].get("width"), videos[0].get("height")) != (width, height):
        raise CaptionError("Caption dimensions do not match the rendered video")
    cues = _validated_cues(cues, duration, content_windows)
    if fps is not None and (not math.isfinite(float(fps)) or not 0 < float(fps) <= 240):
        raise CaptionError("Caption video frame rate is invalid")
    base = Path(sidecar_base) if sidecar_base is not None else output_path.with_suffix("")
    sidecars = {name: Path(str(base) + suffix) for name, suffix in (("srt", ".srt"), ("ass", ".ass"), ("json", ".captions.json"))}
    for path in sidecars.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="caption-burn-", dir=output_path.parent) as temporary:
        scratch = Path(temporary)
        ass_file, srt_file, json_file, video = (scratch / name for name in ("captions.ass", "captions.srt", "captions.json", "captioned.mp4"))
        ass_file.write_text(_ass(cues, width, height), encoding="utf-8")
        srt_file.write_text("\n".join(f"{index}\n{_srt_time(math.ceil(cue.start * 1000 - 1e-8) / 1000)} --> {_srt_time(math.floor(cue.end * 1000 + 1e-8) / 1000)}\n{_wrapped(cue.text)}\n" for index, cue in enumerate(cues, 1)), encoding="utf-8")
        if cues:
            command = ["ffmpeg", "-v", "error", "-y", "-i", str(input_path), "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-vf", "ass=filename=captions.ass", "-c:v", "libx264", "-preset", str(preset), "-crf", str(int(crf)), "-pix_fmt", "yuv420p", "-c:a", "copy"]
            if fps is not None:
                command += ["-r", str(fps)]
            command += ["-movflags", "+faststart", str(video)]
            _run(command, cwd=scratch, log=log)
        else:
            shutil.copyfile(input_path, video)
        rendered = _probe(video)
        actual_video = [stream for stream in rendered.get("streams", []) if stream.get("codec_type") == "video"]
        actual_audio = [stream for stream in rendered.get("streams", []) if stream.get("codec_type") == "audio"]
        if not actual_video or (actual_video[0].get("width"), actual_video[0].get("height")) != (width, height):
            raise CaptionError("Caption encoding changed the video dimensions")
        if len(actual_audio) != len(audio) or any(before.get("codec_name") != after.get("codec_name") for before, after in zip(audio, actual_audio)):
            raise CaptionError("Caption encoding did not preserve the original audio streams")
        if abs(_duration(rendered) - duration) > max(0.25, 2 / float(fps or 30)):
            raise CaptionError("Caption encoding changed the video duration")
        output_hash = _hash(video)
        report = {"version": CAPTION_VERSION, "source": source, "sourceKind": source_kind, "timing": "script-proportional" if source == "narration-script" else "speech-timestamps", "cueCount": len(cues), "noSpeech": not cues, "duration": duration, "width": width, "height": height, "contentWindows": None if content_windows is None else [list(window) for window in content_windows], "cues": [asdict(cue) for cue in cues], "sourceFilename": input_path.name, "outputFilename": output_path.name, "sourceSha256": source_hash, "inputSha256": source_hash, "outputSha256": output_hash, "videoSha256": output_hash, "createdAt": time.time(), "files": {name: path.name for name, path in sidecars.items()}}
        json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        # Publish the video last. Roll back sidecars if any atomic rename fails.
        _publish_bundle([(srt_file, sidecars["srt"]), (ass_file, sidecars["ass"]), (json_file, sidecars["json"]), (video, output_path)], scratch)
    return report
