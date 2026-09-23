"""Project isolation and stale-gate regressions; never synthesize audio."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.app as service


class ProjectBindingTests(unittest.TestCase):
    def test_another_project_cannot_save_into_current_project(self):
        with patch.object(service, "write_json_atomic") as write:
            response = TestClient(service.app).post("/api/voice-selection", json={
                "url": "https://www.youtube.com/watch?v=other-video", "assignments": {}})
            self.assertEqual(response.status_code, 409)
            write.assert_not_called()

    def test_stale_translation_cannot_cast_or_authorize_payment(self):
        with patch.object(service, "verify_translation_gate", return_value=False), patch.object(service, "write_json_atomic") as write:
            response = TestClient(service.app).post("/api/voice-selection", json={
                "url": f"https://www.youtube.com/watch?v={service.PROJECT_ID}", "assignments": {}})
            self.assertEqual(response.status_code, 409)
            self.assertFalse(service.paid_audition_authorized())
            write.assert_not_called()
