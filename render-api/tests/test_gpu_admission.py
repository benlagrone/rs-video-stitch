import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
