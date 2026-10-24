import asyncio
import sys
from pathlib import Path
from unittest import TestCase, mock

from fastapi import BackgroundTasks, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import api
from app.schemas import LeadCardGenerateRequest, ScriptEnhanceRequest, VoiceSampleRequest, YouTubeDescriptionRequest


class _FakeOllamaResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"response": "A tighter narrated script."}


class _FakeDescriptionResponse(_FakeOllamaResponse):
    def __init__(self, response):
        self.response = response

    def json(self):
        return {"response": self.response}


class _FakeLeadCardResponse(_FakeOllamaResponse):
    def json(self):
        return {"response": '{"lines":["Genesis 1","The Beginning of Creation","King James Version"]}'}


class _FakeVoiceCatalogResponse(_FakeOllamaResponse):
    def json(self):
        return {
            "backends": [
                {"name": "vibevoice", "voices": ["en-Emma_woman"], "detail": "ready"},
                {"name": "azure_voice", "voices": ["en-US-JennyNeural"], "detail": "ready"},
            ]
        }


class ScriptEnhanceApiTest(TestCase):
    def test_parse_byte_range_supports_open_ended_range(self):
        self.assertEqual(api._parse_byte_range("bytes=100-", 1000), (100, 999))

    def test_parse_byte_range_supports_suffix_range(self):
        self.assertEqual(api._parse_byte_range("bytes=-250", 1000), (750, 999))

    def test_parse_byte_range_rejects_out_of_bounds_range(self):
        self.assertIsNone(api._parse_byte_range("bytes=1000-1200", 1000))

    def test_voice_options_labels_sources_and_enables_provider_specific_routes(self):
        with mock.patch.object(api, "VOICE_GATEWAY_URL", "http://voice-gateway.local/"), mock.patch.object(
            api.requests,
            "get",
            return_value=_FakeVoiceCatalogResponse(),
        ) as get:
            response = asyncio.run(api.voice_options())

        get.assert_called_once_with("http://voice-gateway.local/control/api/voice", timeout=10)
        providers = {provider["id"]: provider for provider in response["providers"]}
        self.assertTrue(providers["vibevoice"]["selectable"])
        self.assertEqual(providers["vibevoice"]["ttsApi"], "voice-gateway")
        self.assertEqual(providers["vibevoice"]["voices"], ["Carter", "en-Emma_woman"])
        self.assertTrue(providers["azure_voice"]["selectable"])
        self.assertEqual(providers["azure_voice"]["ttsApi"], "azure-proxy")
        self.assertEqual(providers["azure_voice"]["label"], "Azure Speech · Fortress proxy")
        self.assertEqual(providers["flite"]["voices"], ["kal", "awb", "rms", "slt"])

    def test_voice_options_uses_recent_catalog_after_transient_gateway_timeout(self):
        cached_catalog = {"providers": [{"id": "vibevoice", "voices": ["Carter"]}], "gateway": "fortress-lan:voice-gateway"}
        with api._voice_catalog_lock:
            previous_cache = api._voice_catalog_cache
            previous_cached_at = api._voice_catalog_cached_at
            api._voice_catalog_cache = cached_catalog
            api._voice_catalog_cached_at = api.time.monotonic()
        try:
            with mock.patch.object(api.requests, "get", side_effect=api.requests.Timeout("timed out")):
                response = api._voice_options_payload()
            self.assertEqual(response, cached_catalog)
        finally:
            with api._voice_catalog_lock:
                api._voice_catalog_cache = previous_cache
                api._voice_catalog_cached_at = previous_cached_at

    def test_voice_sample_uses_selected_gateway_voice_without_creating_a_project(self):
        async def create_sample():
            with mock.patch.object(
                api,
                "_voice_options_payload",
                return_value={
                    "providers": [
                        {
                            "id": "vibevoice",
                            "ttsApi": "voice-gateway",
                            "selectable": True,
                            "voices": ["Carter"],
                        }
                    ]
                },
            ), mock.patch.object(api, "_synthesize_voice_gateway") as synthesize:
                def write_audio(text, destination, **kwargs):
                    destination.write_bytes(b"RIFFsampleWAVE")

                synthesize.side_effect = write_audio
                response = await api.create_voice_sample(
                    VoiceSampleRequest(text="A short sample.", voice="Carter", ttsApi="voice-gateway"),
                    BackgroundTasks(),
                )
                return response, synthesize

        response, synthesize = asyncio.run(create_sample())
        sample_path = Path(response.path)
        try:
            self.assertEqual(response.media_type, "audio/wav")
            self.assertTrue(sample_path.exists())
            synthesize.assert_called_once()
            self.assertEqual(synthesize.call_args.kwargs["project_id"], "voice-sample")
            self.assertEqual(synthesize.call_args.kwargs["voice"], "Carter")
        finally:
            api._remove_voice_sample_files(sample_path)

    def test_voice_sample_rejects_voice_outside_live_catalog(self):
        async def request_sample():
            with mock.patch.object(api, "_voice_options_payload", return_value={"providers": []}):
                await api.create_voice_sample(
                    VoiceSampleRequest(text="A short sample.", voice="Unknown", ttsApi="voice-gateway"),
                    BackgroundTasks(),
                )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(request_sample())
        self.assertEqual(error.exception.status_code, 422)

    def test_enhance_script_calls_ollama_model(self):
        with mock.patch.object(api, "OLLAMA_BASE_URL", "http://ollama.local:11434"), mock.patch.object(
            api,
            "OLLAMA_MODEL",
            "mixtral:latest",
        ), mock.patch.object(api.requests, "post", return_value=_FakeOllamaResponse()) as post:
            response = asyncio.run(
                api.enhance_script(
                    ScriptEnhanceRequest(script="Original narration.", targetSeconds=45)
                )
            )

        self.assertEqual(response.script, "A tighter narrated script.")
        post.assert_called_once()
        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(url, "http://ollama.local:11434/api/generate")
        self.assertEqual(payload["model"], "mixtral:latest")
        self.assertIn("45 seconds", payload["prompt"])
        self.assertIn("Original narration.", payload["prompt"])

    def test_enhance_script_includes_room_info_when_supplied(self):
        with mock.patch.object(api, "OLLAMA_BASE_URL", "http://ollama.local:11434"), mock.patch.object(
            api.requests,
            "post",
            return_value=_FakeOllamaResponse(),
        ) as post:
            asyncio.run(
                api.enhance_script(
                    ScriptEnhanceRequest(
                        script="Original narration.",
                        targetSeconds=30,
                        roomInfo=[
                            {
                                "filename": "GK-1.jpg",
                                "header": "Chef kitchen",
                                "roomDescription": "Oversized island and new appliances",
                                "label": "kitchen",
                            }
                        ],
                    )
                )
            )

        payload = post.call_args.kwargs["json"]
        self.assertIn("Chef kitchen", payload["prompt"])
        self.assertIn("kitchen", payload["prompt"])
        self.assertIn("Oversized island", payload["prompt"])

    def test_enhance_script_returns_one_scene_text_per_requested_image_scene(self):
        scene_texts = ["A bright foyer welcomes you in.", "The private deck extends the living space."]
        with mock.patch.object(
            api.requests,
            "post",
            return_value=_FakeDescriptionResponse('{"sceneTexts":["A bright foyer welcomes you in.","The private deck extends the living space."]}'),
        ) as post:
            response = asyncio.run(
                api.enhance_script(
                    ScriptEnhanceRequest(
                        script="Original narration.",
                        targetSeconds=30,
                        scenePlan=[
                            {"filename": "GK-1.jpg", "room": "Foyer", "notes": "Natural light"},
                            {"filename": "GK-2.jpg", "room": "Garage apartment", "notes": "Private deck"},
                        ],
                    )
                )
            )

        self.assertEqual(response.sceneTexts, scene_texts)
        self.assertEqual(response.script, "\n\n".join(scene_texts))
        prompt = post.call_args.kwargs["json"]["prompt"]
        self.assertIn('"sceneTexts"', prompt)
        self.assertIn("Garage apartment", prompt)

    def test_listing_pipeline_prompt_requires_simplified_chinese_for_mandarin_projects(self):
        prompt = api._script_enhance_prompt(
            "Welcome to this home.",
            45,
            language="zh-CN",
        )

        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("Do not leave any English narration", prompt)

    def test_listing_pipeline_splits_chinese_sentences_without_spaces(self):
        sections = api._split_script_for_pipeline(
            "欢迎来到这处住宅。宽敞的空间适合日常生活。请联系 LeCrown Properties 预约看房。",
            3,
        )

        self.assertEqual(sections, [
            "欢迎来到这处住宅。",
            "宽敞的空间适合日常生活。",
            "请联系 LeCrown Properties 预约看房。",
        ])

    def test_generate_lead_card_calls_ollama_and_returns_three_lines(self):
        with mock.patch.object(api, "OLLAMA_BASE_URL", "http://ollama.local:11434"), mock.patch.object(
            api,
            "OLLAMA_MODEL",
            "mixtral:latest",
        ), mock.patch.object(api.requests, "post", return_value=_FakeLeadCardResponse()) as post:
            response = asyncio.run(
                api.generate_lead_card(
                    LeadCardGenerateRequest(
                        title="Genesis 1 (KJV)",
                        script="In the beginning God created the heaven and the earth.",
                    )
                )
            )

        self.assertEqual(response.lines, ["Genesis 1", "The Beginning of Creation", "King James Version"])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "http://ollama.local:11434/api/generate")
        self.assertEqual(payload["model"], "mixtral:latest")
        self.assertIn("three-line leader card", payload["prompt"])
        self.assertIn("Genesis 1 (KJV)", payload["prompt"])

    def test_generate_hyperframes_transition_title_uses_scene_context(self):
        with mock.patch.object(api, "OLLAMA_BASE_URL", "http://ollama.local:11434"), mock.patch.object(
            api,
            "OLLAMA_MODEL",
            "mixtral:latest",
        ), mock.patch.object(api.requests, "post", return_value=_FakeDescriptionResponse("Light-Filled Living")) as post:
            response = asyncio.run(
                api.generate_hyperframes_transition_title(
                    api.HyperframesTransitionTitleRequest(
                        outgoingTitle="Front Entry",
                        incomingTitle="Living Room",
                        narration="Step into the bright living space.",
                    )
                )
            )

        self.assertEqual(response.title, "Light-Filled Living")
        self.assertEqual(response.model, "mixtral:latest")
        payload = post.call_args.kwargs["json"]
        self.assertIn("Outgoing scene: Front Entry", payload["prompt"])
        self.assertIn("Incoming scene: Living Room", payload["prompt"])

    def test_transition_title_batch_parser_accepts_numbered_ollama_output(self):
        parsed = api._parse_hyperframes_transition_titles(
            "1. Grand Arrival\n2. Light-Filled Living\n3. Private Retreat",
            3,
        )

        self.assertEqual(parsed, ["Grand Arrival", "Light-Filled Living", "Private Retreat"])

    def test_enhance_youtube_description_calls_ollama_with_listing_prompt(self):
        complete_description = " ".join(["Property details and viewing information"] * 20)
        with mock.patch.object(api, "OLLAMA_BASE_URL", "http://ollama.local:11434"), mock.patch.object(
            api,
            "OLLAMA_MODEL",
            "mixtral:latest",
        ), mock.patch.object(
            api.requests,
            "post",
            return_value=_FakeDescriptionResponse(complete_description),
        ) as post:
            response = asyncio.run(
                api.enhance_youtube_description(
                    YouTubeDescriptionRequest(
                        title="2223 Dorrington Drive",
                        script="Commercial property near the Texas Medical Center.",
                        currentDescription="",
                        roomInfo=[{"filename": "GK-1.jpg", "header": "Front Entrance"}],
                    )
                )
            )

        self.assertEqual(response.description, complete_description)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "mixtral:latest")
        self.assertEqual(payload["options"]["num_predict"], 512)
        self.assertIn("YouTube video description", payload["prompt"])
        self.assertIn("same primary language", payload["prompt"])
        self.assertIn("2223 Dorrington Drive", payload["prompt"])
        self.assertIn("Front Entrance", payload["prompt"])

    def test_enhance_youtube_description_retries_truncated_response(self):
        complete_description = " ".join(["Property details and viewing information"] * 20)
        responses = [
            _FakeDescriptionResponse("Welcome to the property at 980"),
            _FakeDescriptionResponse(complete_description),
        ]
        with mock.patch.object(api.requests, "post", side_effect=responses) as post:
            response = asyncio.run(
                api.enhance_youtube_description(
                    YouTubeDescriptionRequest(
                        title="9800 Richmond Avenue",
                        script="Flexible commercial office space in Houston.",
                    )
                )
            )

        self.assertEqual(response.description, complete_description)
        self.assertEqual(post.call_count, 2)
        retry_payload = post.call_args.kwargs["json"]
        self.assertIn("previous response was incomplete", retry_payload["prompt"])

    def test_enhance_youtube_description_retries_placeholder_artifacts(self):
        artifact_description = (
            "欢迎了解这处优质房产。" * 15
            + "\n电话：[INSERT CONTACT PHONE NUMBER]\n（注：信息可能有变化。）"
        )
        complete_description = "欢迎了解这处位于休斯顿、空间实用的优质房产。" * 12
        responses = [
            _FakeDescriptionResponse(artifact_description),
            _FakeDescriptionResponse(complete_description),
        ]
        with mock.patch.object(api.requests, "post", side_effect=responses) as post:
            response = asyncio.run(
                api.enhance_youtube_description(
                    YouTubeDescriptionRequest(
                        title="休斯顿优质房产",
                        script="欢迎参观这处交通便利、空间实用的房产。",
                    )
                )
            )

        self.assertEqual(response.description, complete_description)
        self.assertEqual(post.call_count, 2)

    def test_enhance_youtube_description_rejects_two_truncated_responses(self):
        responses = [
            _FakeDescriptionResponse("Welcome to the property at 980"),
            _FakeDescriptionResponse("Explore this property today"),
        ]
        with mock.patch.object(api.requests, "post", side_effect=responses):
            with self.assertRaises(api.HTTPException) as raised:
                asyncio.run(
                    api.enhance_youtube_description(
                        YouTubeDescriptionRequest(
                            title="9800 Richmond Avenue",
                            script="Flexible commercial office space in Houston.",
                        )
                    )
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("incomplete", raised.exception.detail)

    def test_enhance_youtube_description_accepts_complete_chinese_response(self):
        complete_description = "欢迎了解这处位于休斯顿的优质房产。" * 12
        with mock.patch.object(
            api.requests,
            "post",
            return_value=_FakeDescriptionResponse(
                complete_description
                + "\n\n（请注意：本视频信息仅供参考。）"
                + "\n\n(226 characters)"
            ),
        ) as post:
            response = asyncio.run(
                api.enhance_youtube_description(
                    YouTubeDescriptionRequest(
                        title="休斯顿优质房产",
                        script="欢迎参观这处交通便利、空间实用的房产。",
                    )
                )
            )

        self.assertEqual(response.description, complete_description)
        self.assertEqual(post.call_count, 1)
