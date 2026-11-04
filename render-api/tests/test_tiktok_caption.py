import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app import api
from app.schemas import TikTokCaptionRequest
from app.tiktok_caption import CaptionGenerationError, caption_context, generate_caption


ENGLISH = "Office space for lease at 12238 Queenston Blvd, Unit K. Rent is $2,500 per month with water included. Contact Jie Huang at 832-798-3991 to schedule a showing. #Houston #OfficeForLease #LeCrownProperties"
CHINESE = "休斯顿办公空间出租，配有独立诊室与茶水间，每月租金 $2,500，包含水费。欢迎联系 Jie Huang 预约参观，了解适合您的办公安排。 #休斯顿 #办公空间出租 #LeCrownProperties"
SOURCE = "Office for lease at 12238 Queenston Blvd, Unit K. Rent $2,500 per month, water included, with exam rooms and a kitchenette."


class TikTokCaptionTests(unittest.IsolatedAsyncioTestCase):
    def context(self, **overrides):
        return caption_context({"originalScriptText": SOURCE, **overrides}, {}, {}, lecrown=True)

    def test_authoritative_original_text_wins_over_enhanced_script(self):
        context = caption_context({"originalScriptText": SOURCE, "script": "Invented mansion with a pool."}, {}, {"script": "Rewritten copy."}, lecrown=True)
        self.assertEqual(context["source"], "originalScriptText")
        self.assertEqual(context["sourceText"], SOURCE)
        self.assertNotIn("mansion", str(context))

    def test_unsaved_source_fields_are_used_without_changing_saved_state(self):
        state = {"originalScriptText": "Saved old text", "title": "Old title"}
        context = caption_context(state, {}, {"originalScriptText": SOURCE, "title": "New title", "propertyAddress": "12238 Queenston Blvd"}, lecrown=True)
        self.assertEqual(context["sourceText"], SOURCE)
        self.assertEqual(context["title"], "New title")
        self.assertEqual(state, {"originalScriptText": "Saved old text", "title": "Old title"})

    def test_script_and_scene_fallbacks(self):
        self.assertEqual(caption_context({"script": SOURCE}, {}, {}, lecrown=True)["source"], "script")
        context = caption_context({}, {"scenes": [{"VO": SOURCE}]}, {}, lecrown=True)
        self.assertEqual(context["source"], "scenes")
        self.assertEqual(context["sourceText"], SOURCE)

    def test_source_language_inference_and_explicit_bilingual_selection(self):
        document = {"scenes": [{"VO": "办公室出租，欢迎预约参观。"}]}
        self.assertEqual(caption_context({}, document, {}, lecrown=True)["language"], "zh-CN")
        state = {"originalScriptText": "办公室出租。 English version follows.", "language": "zh-CN"}
        self.assertEqual(caption_context(state, {}, {"language": "en-US"}, lecrown=True)["language"], "en-US")
        self.assertEqual(caption_context(state, {}, {}, lecrown=True)["language"], "zh-CN")

    def test_non_lecrown_context_has_no_invented_broker_or_contact(self):
        context = caption_context({"script": "See a lion in its natural habitat."}, {}, {}, lecrown=False)
        self.assertEqual(context["contact"], {})
        self.assertNotIn("Jie", str(context))
        self.assertNotIn("832", str(context))

    def test_ollama_generates_a_grounded_english_caption(self):
        generator = Mock(return_value=ENGLISH)
        self.assertEqual(generate_caption(self.context(), generator), ENGLISH)
        prompt = generator.call_args.args[0]
        self.assertIn("English", prompt)
        self.assertIn(SOURCE, prompt)
        self.assertEqual(generator.call_args.kwargs["num_predict"], 512)

    def test_chinese_project_returns_chinese_after_wrong_language_retry(self):
        generator = Mock(side_effect=[ENGLISH, CHINESE])
        result = generate_caption(self.context(language="zh-CN"), generator)
        self.assertEqual(result, CHINESE)
        self.assertIn("Simplified Chinese", generator.call_args.args[0])
        self.assertEqual(generator.call_count, 2)

    def test_unsupported_numeric_fact_retries_instead_of_publishing_it(self):
        generator = Mock(side_effect=[ENGLISH.replace("$2,500", "$9,999"), ENGLISH])
        self.assertEqual(generate_caption(self.context(), generator), ENGLISH)
        self.assertIn("numerical detail", generator.call_args.args[0])

    def test_unsupported_email_url_or_phone_is_rejected(self):
        for extra in (" Email fake@example.com.", " Visit https://invented.example.", " Call 832-3991-798."):
            with self.subTest(extra=extra), self.assertRaises(CaptionGenerationError):
                generate_caption(self.context(), Mock(return_value=ENGLISH + extra))

    def test_utf16_limit_rejects_caption_with_more_units_than_codepoints(self):
        value = ENGLISH + " 🏠" * 700
        self.assertLess(len(value), 2200)
        with self.assertRaises(CaptionGenerationError):
            generate_caption(self.context(), Mock(return_value=value))

    def test_empty_output_and_missing_hashtags_fail_without_template_fallback(self):
        for value in ("", "View this office and contact the listing representative for a tour."):
            with self.subTest(value=value), self.assertRaises(CaptionGenerationError):
                generate_caption(self.context(), Mock(return_value=value))

    async def test_endpoint_returns_ollama_provenance_and_does_not_save_state(self):
        with patch.object(api, "read_project_state", return_value={"originalScriptText": SOURCE}), patch.object(api, "_load_scenes", return_value={}), patch.object(api, "is_lecrown_project", return_value=True), patch.object(api, "_ollama_generate", return_value=ENGLISH) as generate, patch.object(api, "save_project_state") as save:
            response = await api.generate_tiktok_caption("property", TikTokCaptionRequest(language="en-US"))
        self.assertEqual(response.caption, ENGLISH)
        self.assertEqual(response.generatedBy, "ollama")
        self.assertEqual(response.model, api.OLLAMA_MODEL)
        self.assertEqual(response.provenance["source"], "originalScriptText")
        generate.assert_called_once()
        save.assert_not_called()

    async def test_empty_project_returns_clear_error_before_model_call(self):
        with patch.object(api, "read_project_state", return_value={}), patch.object(api, "_load_scenes", return_value={}), patch.object(api, "_ollama_generate") as generate:
            with self.assertRaises(HTTPException) as error:
                await api.generate_tiktok_caption("property", TikTokCaptionRequest())
        self.assertEqual(error.exception.status_code, 400)
        generate.assert_not_called()

    async def test_provider_failure_is_not_replaced_with_a_claimed_llm_caption(self):
        with patch.object(api, "read_project_state", return_value={"script": SOURCE}), patch.object(api, "_load_scenes", return_value={}), patch.object(api, "_ollama_generate", side_effect=HTTPException(status_code=502, detail="Ollama unavailable")), patch.object(api, "save_project_state") as save:
            with self.assertRaises(HTTPException) as error:
                await api.generate_tiktok_caption("property", TikTokCaptionRequest())
        self.assertEqual(error.exception.status_code, 502)
        self.assertIn("Ollama unavailable", error.exception.detail)
        save.assert_not_called()
