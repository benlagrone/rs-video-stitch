import unittest
from unittest import mock

import requests

from app import gpu_admission


class Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class GpuAdmissionTest(unittest.TestCase):
    def test_live_sextant_fails_closed_when_admission_is_not_configured(self):
        with mock.patch.object(gpu_admission, "OPTIMIZATION_MCP_URL", ""), mock.patch.object(
            gpu_admission, "OPTIMIZATION_MCP_TOKEN", ""
        ), mock.patch.object(gpu_admission, "ADMISSION_REQUIRED", True):
            with self.assertRaises(gpu_admission.GpuAdmissionError):
                with gpu_admission.admit_gpu("ollama", vram_required_mb=8192):
                    pass

    def test_context_holds_heartbeats_and_releases_matching_lease(self):
        responses = {
            "/internal/v1/phronesis/admission/acquire": Response(200, {"leaseId": "lease-1"}),
            "/internal/v1/phronesis/admission/release": Response(200, {"status": "released"}),
        }

        def post(path, payload, timeout=15):
            return responses[path]

        with mock.patch.object(gpu_admission, "OPTIMIZATION_MCP_URL", "http://optimizer"), mock.patch.object(
            gpu_admission, "OPTIMIZATION_MCP_TOKEN", "secret"
        ), mock.patch.object(gpu_admission, "_post", side_effect=post) as request:
            with gpu_admission.admit_gpu("comfyui", workload_id="render-1", vram_required_mb=12000) as lease:
                self.assertEqual(lease, "lease-1")

        self.assertEqual(request.call_args_list[-1].args, (
            "/internal/v1/phronesis/admission/release",
            {"lease_id": "lease-1"},
        ))

    def test_governed_post_holds_lease_for_provider_response(self):
        provider = mock.Mock()
        provider.post.return_value = Response(200, {"response": "ok"})
        with mock.patch.object(gpu_admission, "admit_gpu") as admission:
            response = gpu_admission.governed_post(
                provider,
                "http://phronesis/api/generate",
                workload_class="ollama",
                vram_required_mb=8192,
                json={"model": "test"},
                timeout=30,
            )
        self.assertEqual(response.json()["response"], "ok")
        admission.assert_called_once_with(
            "ollama",
            vram_required_mb=8192,
            duration_slots=1,
            priority=1,
        )
        provider.post.assert_called_once_with(
            "http://phronesis/api/generate",
            json={"model": "test"},
            timeout=30,
        )

    def test_acquire_retries_a_slow_scheduler_until_it_answers(self):
        calls = {"acquire": 0}

        def post(path, payload, timeout=None):
            if path.endswith("/acquire"):
                calls["acquire"] += 1
                if calls["acquire"] == 1:
                    raise requests.Timeout("read timed out")
                return Response(200, {"leaseId": "lease-2"})
            return Response(200, {"status": "released"})

        with mock.patch.object(gpu_admission, "OPTIMIZATION_MCP_URL", "http://optimizer"), mock.patch.object(
            gpu_admission, "OPTIMIZATION_MCP_TOKEN", "secret"
        ), mock.patch.object(gpu_admission, "ADMISSION_WAIT_SECONDS", 30), mock.patch.object(
            gpu_admission, "_post", side_effect=post
        ), mock.patch.object(gpu_admission.time, "sleep"):
            with gpu_admission.admit_gpu("ollama", workload_id="lead-card", vram_required_mb=8192) as lease:
                self.assertEqual(lease, "lease-2")

        self.assertEqual(calls["acquire"], 2)


if __name__ == "__main__":
    unittest.main()
