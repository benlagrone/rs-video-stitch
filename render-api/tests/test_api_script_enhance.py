import asyncio
import sys
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import api
from app.schemas import LeadCardGenerateRequest, ScriptEnhanceRequest, YouTubeDescriptionRequest


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
        self.assertEqual(providers["vibevoice"]["ttsApi"], "vibevoice-proxy")
        self.assertEqual(providers["vibevoice"]["voices"], ["Carter", "en-Emma_woman"])
        self.assertTrue(providers["azure_voice"]["selectable"])
        self.assertEqual(providers["azure_voice"]["ttsApi"], "azure-proxy")
        self.assertEqual(providers["azure_voice"]["label"], "Azure Speech · Fortress proxy")
        self.assertEqual(providers["flite"]["voices"], ["kal", "awb", "rms", "slt"])

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
            return_value=_FakeDescriptionResponse(complete_description),
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
