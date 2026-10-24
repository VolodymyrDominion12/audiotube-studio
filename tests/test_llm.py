import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from audiotube.config import Config
from audiotube.llm import (
    AnthropicClient,
    LLMError,
    OllamaClient,
    OpenAIClient,
    build_client,
    provider_report,
)
from audiotube.planner import generate_plan
from audiotube.transcript import Segment, Transcript


class FakeAPI:
    """Мінімальний сервер, що імітує Ollama / OpenAI / Anthropic."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.fail_times = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/api/tags":
                    self._json({"models": [{"name": "qwen2.5:7b"}]})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                api.requests.append((self.path, payload))
                if api.fail_times > 0:
                    api.fail_times -= 1
                    self._json({"error": "boom"}, 500)
                    return
                if self.path == "/api/chat":
                    content = api.chat_content
                    self._json({"message": {"content": content}})
                elif self.path == "/v1/chat/completions":
                    self._json({"choices": [{"message": {"content": api.chat_content}}]})
                elif self.path == "/v1/messages":
                    self._json({"content": [{"type": "text", "text": api.chat_content}]})
                else:
                    self._json({"error": "not found"}, 404)

            def _json(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.chat_content = '{"title": "T"}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.messages = [
            {"role": "system", "content": "ти редактор"},
            {"role": "user", "content": "зроби метадані"},
        ]

    def tearDown(self):
        self.api.close()

    def test_ollama(self):
        cfg = Config().llm
        cfg.base_url = self.api.url
        client = OllamaClient(cfg)
        self.assertEqual(client.complete(self.messages), '{"title": "T"}')
        path, payload = self.api.requests[-1]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(payload["format"], "json")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["model"], cfg.model)

    def test_ollama_ping(self):
        cfg = Config().llm
        cfg.base_url = self.api.url
        ok, detail = OllamaClient(cfg).ping()
        self.assertTrue(ok)
        self.assertIn("qwen2.5", detail)

    def test_openai(self):
        cfg = Config().llm
        cfg.provider = "openai"
        cfg.base_url = self.api.url + "/v1"
        cfg.api_key_env = "AUDIOTUBE_TEST_OPENAI"
        os.environ["AUDIOTUBE_TEST_OPENAI"] = "secret"
        try:
            client = OpenAIClient(cfg)
            self.assertEqual(client.complete(self.messages), '{"title": "T"}')
        finally:
            del os.environ["AUDIOTUBE_TEST_OPENAI"]
        path, payload = self.api.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_anthropic_moves_system_message(self):
        cfg = Config().llm
        cfg.provider = "anthropic"
        cfg.base_url = self.api.url + "/v1"
        cfg.api_key_env = "AUDIOTUBE_TEST_ANTHROPIC"
        os.environ["AUDIOTUBE_TEST_ANTHROPIC"] = "secret"
        try:
            client = AnthropicClient(cfg)
            self.assertEqual(client.complete(self.messages), '{"title": "T"}')
        finally:
            del os.environ["AUDIOTUBE_TEST_ANTHROPIC"]
        path, payload = self.api.requests[-1]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(payload["system"], "ти редактор")
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])

    def test_missing_key(self):
        cfg = Config().llm
        cfg.provider = "anthropic"
        cfg.base_url = self.api.url + "/v1"
        cfg.api_key_env = "AUDIOTUBE_TEST_MISSING"
        os.environ.pop("AUDIOTUBE_TEST_MISSING", None)
        with self.assertRaises(LLMError):
            AnthropicClient(cfg).complete(self.messages)

    def test_retry_on_500(self):
        cfg = Config().llm
        cfg.base_url = self.api.url
        cfg.retries = 1
        self.api.fail_times = 1
        with mock.patch("audiotube.llm.time.sleep"):
            self.assertEqual(OllamaClient(cfg).complete(self.messages), '{"title": "T"}')
        self.assertEqual(len(self.api.requests), 2)

    def test_build_client(self):
        cfg = Config()
        cfg.llm.base_url = self.api.url
        self.assertIsInstance(build_client(cfg.llm), OllamaClient)
        cfg.llm.provider = "none"
        with self.assertRaises(LLMError):
            build_client(cfg.llm)

    def test_provider_report(self):
        cfg = Config().llm
        cfg.base_url = self.api.url
        ok, detail = provider_report(cfg)
        self.assertTrue(ok)
        self.assertIn("qwen2.5", detail)


class PlannerWithServerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.cfg = Config()
        self.cfg.llm.base_url = self.api.url
        self.cfg.llm.retries = 0
        self.transcript = Transcript(
            language="uk", duration=120,
            segments=[Segment(0, 60, "Вступ про Docker"), Segment(60, 120, "Контейнери")],
        )

    def tearDown(self):
        self.api.close()

    def test_generate_plan_over_http(self):
        self.api.chat_content = json.dumps({
            "title": "Docker за 20 хвилин",
            "description": "Опис",
            "tags": ["docker"],
            "chapters": ["00:00 Вступ", "01:00 Контейнери"],
            "playlist": "DevOps за 20 хвилин",
            "visual_keywords": ["docker"],
            "thumbnail_text": "Docker",
        })
        plan = generate_plan(self.transcript, self.cfg, "Docker")
        self.assertEqual(plan.title, "Docker за 20 хвилин")
        self.assertEqual([c.time for c in plan.chapters], ["00:00", "01:00"])

    def test_generate_plan_reports_bad_json(self):
        self.api.chat_content = "не json"
        with self.assertRaises(Exception) as ctx:
            generate_plan(self.transcript, self.cfg, "Docker")
        self.assertIn("JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
