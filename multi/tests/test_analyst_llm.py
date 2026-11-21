"""LLM adapter contracts with real Requests behavior and synthetic HTTP I/O."""
from contextlib import contextmanager
from io import BytesIO
import importlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import NewConnectionError, ReadTimeoutError
from urllib3.response import HTTPResponse

try:
    analyst_llm = importlib.import_module("analyst_llm")
except ModuleNotFoundError as exc:
    if exc.name != "analyst_llm":
        raise
    analyst_llm = None


class TrackingBody(BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


@contextmanager
def transport(outcomes):
    """Replace socket-level responses, retaining Requests/urllib3 policies."""
    state = SimpleNamespace(attempts=[], sessions=[], closed=[], bodies=[], adapters=[], adapters_closed=[])
    pending = iter(outcomes)
    real_session = requests.Session
    real_close = requests.Session.close
    real_adapter_init = HTTPAdapter.__init__
    real_adapter_close = HTTPAdapter.close

    def init_adapter(adapter, *args, **kwargs):
        real_adapter_init(adapter, *args, **kwargs)
        state.adapters.append(adapter)

    def close_adapter(adapter):
        state.adapters_closed.append(adapter)
        real_adapter_close(adapter)

    def make_session():
        session = real_session()
        state.sessions.append(session)
        return session

    def close_session(session):
        state.closed.append(session)
        real_close(session)

    def respond(pool, connection, method, url, **kwargs):
        state.attempts.append({
            "method": method, "host": pool.host, "port": pool.port,
            "url": url, "headers": dict(kwargs.get("headers", {})),
            "body": kwargs.get("body"), "timeout": kwargs.get("timeout"),
        })
        outcome = next(pending)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        status, headers, body = outcome
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        stream = TrackingBody(body)
        state.bodies.append(stream)
        return HTTPResponse(
            status=status, headers={"Content-Type": "application/json", **headers},
            body=stream, preload_content=False,
        )

    with (patch.object(requests, "Session", side_effect=make_session),
          patch.object(real_session, "close", close_session),
          patch.object(HTTPAdapter, "__init__", init_adapter),
          patch.object(HTTPAdapter, "close", close_adapter),
          patch.object(HTTPConnectionPool, "_make_request", autospec=True, side_effect=respond),
          patch("socket.socket.connect", side_effect=AssertionError("Socket I/O forbidden")),
          patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden"))):
        yield state


def evidence():
    return {
        "records": [{
            "id": 42, "ref": "job#42", "url_display": "https://contoh.id/",
            "initial_host": "contoh.id", "final_host": "contoh.id",
            "status": "done", "risk": "medium_risk", "reason": "Perlu pemeriksaan",
            "updated_at": "2026-11-21T00:00:00Z", "classification": {
                "status": "medium_risk", "ml_source": "classic_ml", "ml_conf": 0.71,
            }, "data_gaps": ["Tidak ada tangkapan layar tersimpan."],
        }],
        "notes": ["Waktu pembaruan status bukan waktu pengambilan halaman."],
    }


def openai_environment(**extra):
    return {"BIMA_ANALYST_PROVIDER": "openai", "BIMA_ANALYST_MODEL": "configured-model",
            "BIMA_ANALYST_ALLOW_CLOUD": "1", "OPENAI_API_KEY": "synthetic-secret", **extra}


def ollama_environment(**extra):
    return {"BIMA_ANALYST_PROVIDER": "ollama", "BIMA_ANALYST_MODEL": "local-model",
            "BIMA_ANALYST_OLLAMA_LOCAL_ONLY": "1", **extra}


def openai_response(text="Bukti memerlukan pemeriksaan. [job#42]", **extra):
    return {
        "id": "resp_synthetic", "object": "response", "created_at": 1795219200,
        "status": "completed", "error": None, "incomplete_details": None,
        "model": "configured-model", "output": [
            {"type": "reasoning", "id": "rs_synthetic", "summary": []},
            {"type": "message", "id": "msg_synthetic", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}]},
        ], "usage": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125}, **extra,
    }


def ollama_response(text="Bukti memerlukan pemeriksaan. [job#42]", **extra):
    return {
        "model": "local-model", "created_at": "2026-11-21T00:00:00Z",
        "message": {"role": "assistant", "content": text}, "done": True,
        "done_reason": "stop", "total_duration": 1000000, "load_duration": 100,
        "prompt_eval_count": 100, "prompt_eval_duration": 1000,
        "eval_count": 25, "eval_duration": 1000, **extra,
    }


class AnalystLLMTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(analyst_llm, "The configured LLM adapter is not implemented")

    def test_default_disabled_does_not_read_credentials_or_make_requests(self):
        with transport([]) as observed, patch("builtins.open", side_effect=AssertionError("No secret read")):
            result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                "OPENAI_API_KEY": "synthetic-secret",
                "BIMA_ANALYST_OPENAI_KEY_FILE": "/not/a/real/key",
            })
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["provider"], "disabled")
        self.assertEqual(result["model"], "")
        self.assertEqual(observed.attempts, [])

    def test_explicit_disabled_returns_bounded_plain_text(self):
        with transport([]):
            result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                "BIMA_ANALYST_PROVIDER": "disabled",
            })
        self.assertIsInstance(result["text"], str)
        self.assertLessEqual(len(result["text"]), 3000)
        self.assertNotIn("[job#", result["text"])

    def test_unknown_provider_is_a_configuration_error_without_network(self):
        with transport([]) as observed:
            result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                "BIMA_ANALYST_PROVIDER": "some-other-provider",
            })
        self.assertEqual(result["status"], "configuration_error")
        self.assertEqual(observed.attempts, [])

    def test_enabled_provider_requires_explicit_model(self):
        for provider in ("openai", "ollama"):
            with self.subTest(provider=provider), transport([]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                    "BIMA_ANALYST_PROVIDER": provider,
                    "BIMA_ANALYST_ALLOW_CLOUD": "1",
                    "BIMA_ANALYST_OLLAMA_LOCAL_ONLY": "1",
                })
            self.assertEqual(result["status"], "configuration_error")
            self.assertEqual(observed.attempts, [])

    def test_cloud_consent_is_checked_before_secret_access(self):
        class ConsentEnvironment(dict):
            def get(self, key, default=None):
                if key in {"OPENAI_API_KEY", "BIMA_ANALYST_OPENAI_KEY_FILE"}:
                    raise AssertionError("Credential access preceded consent")
                return super().get(key, default)

        for consent in (None, "0", "true"):
            values = ConsentEnvironment({
                "BIMA_ANALYST_PROVIDER": "openai", "BIMA_ANALYST_MODEL": "configured-model",
                "BIMA_ANALYST_ALLOW_CLOUD": consent,
            })
            with self.subTest(consent=consent), transport([]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=values)
            self.assertEqual(result["status"], "configuration_error")
            self.assertEqual(observed.attempts, [])

    def test_openai_requires_one_nonempty_well_formed_secret_source(self):
        bad_sources = (
            {}, {"OPENAI_API_KEY": "bad\nsecret"},
            {"OPENAI_API_KEY": "synthetic-key", "BIMA_ANALYST_OPENAI_KEY_FILE": "/tmp/key"},
            {"BIMA_ANALYST_OPENAI_KEY_FILE": "relative-key"},
            {"OPENAI_API_KEY": "x" * 4097},
        )
        for extra in bad_sources:
            with self.subTest(extra=tuple(extra)), transport([]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                    "BIMA_ANALYST_PROVIDER": "openai", "BIMA_ANALYST_MODEL": "configured-model",
                    "BIMA_ANALYST_ALLOW_CLOUD": "1", **extra,
                })
            self.assertEqual(result["status"], "configuration_error")
            self.assertEqual(observed.attempts, [])
            self.assertNotIn("bad\nsecret", result["text"])

    def test_ollama_requires_local_only_daemon_confirmation(self):
        with transport([]) as observed:
            result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                "BIMA_ANALYST_PROVIDER": "ollama", "BIMA_ANALYST_MODEL": "local-model",
            })
        self.assertEqual(result["status"], "configuration_error")
        self.assertEqual(observed.attempts, [])

    def test_ollama_rejects_non_loopback_and_ambiguous_origins(self):
        origins = (
            "http://localhost:11434", "https://ollama.com", "http://192.168.1.2:11434",
            "http://127.0.0.1:11434@evil.example", "http://127.0.0.1:11434/api",
            "http://127.0.0.1:11434?secret=1", "http://127.0.0.1:11434#fragment",
            "http://127.0.0.1:11434\n", "http://[::1%25eth0]:11434", "file:///tmp/ollama",
        )
        for origin in origins:
            with self.subTest(origin=origin), transport([]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ={
                    "BIMA_ANALYST_PROVIDER": "ollama", "BIMA_ANALYST_MODEL": "local-model",
                    "BIMA_ANALYST_OLLAMA_LOCAL_ONLY": "1", "BIMA_ANALYST_OLLAMA_ORIGIN": origin,
                })
            self.assertEqual(result["status"], "configuration_error")
            self.assertEqual(observed.attempts, [])

    def test_missing_evidence_avoids_provider_and_secret_reads(self):
        with transport([]) as observed, patch("os.open", side_effect=AssertionError("No secret read")):
            result = analyst_llm.generate_answer("Apakah aman?", {"records": [], "notes": []}, environ={
                "BIMA_ANALYST_PROVIDER": "openai", "BIMA_ANALYST_MODEL": "configured-model",
                "BIMA_ANALYST_ALLOW_CLOUD": "1", "BIMA_ANALYST_OPENAI_KEY_FILE": "/not/a/real/key",
            })
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(observed.attempts, [])

    def test_openai_sends_responses_payload_and_extracts_nested_text(self):
        with transport([(200, {}, openai_response())]) as observed:
            result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["text"], "Bukti memerlukan pemeriksaan. [job#42]")
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "configured-model")
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 25})
        self.assertEqual(len(observed.attempts), 1)
        request = observed.attempts[0]
        self.assertEqual((request["method"], request["host"], request["url"]),
                         ("POST", "api.openai.com", "/v1/responses"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer synthetic-secret")
        payload = json.loads(request["body"])
        self.assertEqual(payload["model"], "configured-model")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["tool_choice"], "none")
        self.assertEqual(payload["truncation"], "disabled")
        self.assertGreater(payload["max_output_tokens"], 0)
        self.assertLessEqual(payload["max_output_tokens"], 4096)
        packet = json.loads(payload["input"][0]["content"])
        self.assertEqual(packet["question"], "Mengapa?")
        self.assertEqual(packet["allowed_job_ids"], [42])
        self.assertEqual(packet["evidence"]["records"][0]["ref"], "job#42")
        self.assertNotIn("synthetic-secret", request["body"].decode())
        self.assertTrue(all(adapter in observed.adapters_closed for adapter in observed.adapters))
        self.assertTrue(observed.bodies[0].closed)

    def test_ollama_uses_numeric_loopback_without_credentials(self):
        for origin, host in (("http://127.0.0.1:11434", "127.0.0.1"),
                             ("http://[::1]:11434/", "::1")):
            with self.subTest(origin=origin), transport([(200, {}, ollama_response())]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=ollama_environment(
                    BIMA_ANALYST_OLLAMA_ORIGIN=origin, OPENAI_API_KEY="must-not-leave",
                ))
            self.assertEqual(result["status"], "answered")
            request = observed.attempts[0]
            self.assertEqual((request["method"], request["host"], request["url"]),
                             ("POST", host, "/api/chat"))
            self.assertNotIn("Authorization", request["headers"])
            payload = json.loads(request["body"])
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["keep_alive"], 0)
            self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
            self.assertGreater(payload["options"]["num_predict"], 0)
            self.assertLessEqual(payload["options"]["num_ctx"], 8192)
            self.assertNotIn("tools", payload)
            self.assertNotIn("must-not-leave", request["body"].decode())

    def test_openai_ignores_ambient_proxy_netrc_and_ca_environment(self):
        with TemporaryDirectory() as directory:
            netrc = Path(directory) / "netrc"
            netrc.write_text("machine api.openai.com login wrong-user password wrong-password\n")
            with patch.dict(os.environ, {
                    "HTTPS_PROXY": "http://proxy.example:8080", "https_proxy": "http://proxy.example:8080",
                    "NETRC": str(netrc), "REQUESTS_CA_BUNDLE": "/not/a/real/ca-bundle",
                    "OPENAI_BASE_URL": "https://wrong.example/", "NO_PROXY": "", "no_proxy": "",
            }), transport([(200, {}, openai_response())]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(observed.attempts[0]["host"], "api.openai.com")
        self.assertEqual(observed.attempts[0]["headers"]["Authorization"], "Bearer synthetic-secret")

    def test_secret_file_is_read_only_after_opt_in_and_not_forwarded_in_prompt(self):
        with TemporaryDirectory() as directory:
            key_file = Path(directory) / "key"
            key_file.write_text("synthetic-file-secret\n")
            key_file.chmod(0o600)
            with transport([(200, {}, openai_response())]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment(
                    OPENAI_API_KEY="", BIMA_ANALYST_OPENAI_KEY_FILE=str(key_file),
                ))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(observed.attempts[0]["headers"]["Authorization"], "Bearer synthetic-file-secret")
        self.assertNotIn("synthetic-file-secret", observed.attempts[0]["body"].decode())

    def test_secret_file_rejects_public_permissions_symlink_and_oversize(self):
        with TemporaryDirectory() as directory:
            key_file = Path(directory) / "key"
            key_file.write_text("synthetic-secret")
            key_file.chmod(0o644)
            symlink = Path(directory) / "key-link"
            symlink.symlink_to(key_file)
            large_file = Path(directory) / "large-key"
            large_file.write_text("x" * 8193)
            large_file.chmod(0o600)
            for path in (key_file, symlink, large_file, Path(directory)):
                with self.subTest(path=path.name), transport([]) as observed:
                    result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment(
                        OPENAI_API_KEY="", BIMA_ANALYST_OPENAI_KEY_FILE=str(path),
                    ))
                self.assertEqual(result["status"], "configuration_error")
                self.assertEqual(observed.attempts, [])

    def test_retryable_http_statuses_never_repeat_generation_or_echo_errors(self):
        for status in (401, 429, 500, 502, 503, 504):
            with self.subTest(status=status), transport([
                    (status, {"Retry-After": "3600"}, {"error": "sensitive-provider-body"}),
                    (200, {}, openai_response()),
            ]) as observed, patch("urllib3.util.retry.time.sleep", side_effect=AssertionError("No retry wait")):
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(len(observed.attempts), 1)
            self.assertNotIn("sensitive-provider-body", result["text"])
            self.assertTrue(observed.bodies[0].closed)

    def test_connect_and_read_failures_never_repeat_generation(self):
        for error in (NewConnectionError(None, "sensitive-connect-error"),
                      ReadTimeoutError(None, "/v1/responses", "sensitive-timeout")):
            with self.subTest(error=type(error).__name__), transport([
                    error, (200, {}, openai_response()),
            ]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(len(observed.attempts), 1)
            self.assertNotIn("sensitive-", result["text"])
            self.assertTrue(all(adapter in observed.adapters_closed for adapter in observed.adapters))

    def test_redirect_does_not_forward_auth_or_consume_redirect_body(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), transport([
                    (status, {"Location": "https://collector.example/secret"}, b"x" * 262144),
                    (200, {}, openai_response()),
            ]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual([request["host"] for request in observed.attempts], ["api.openai.com"])
            self.assertEqual(observed.bodies[0].bytes_read, 0)
            self.assertTrue(observed.bodies[0].closed)

    def test_completed_answers_must_reference_only_selected_job_ids(self):
        for text in ("Tidak ada sumber.", "Temuan. [job#999]", "Temuan. [job#42] [job#999]",
                     "Temuan. [job#42] dan job#999", "Temuan. [job#42] [job#042]"):
            with self.subTest(text=text), transport([(200, {}, openai_response(text))]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(len(observed.attempts), 1)
            self.assertNotIn("[job#42]", result["text"])

    def test_answer_over_3000_characters_is_not_silently_truncated(self):
        with transport([(200, {}, openai_response("x" * 3000 + " [job#42]"))]):
            result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
        self.assertEqual(result["status"], "incomplete")
        self.assertLessEqual(len(result["text"]), 3000)

    def test_openai_refusal_and_incomplete_response_are_not_answers(self):
        refusal = openai_response(output=[{
            "type": "message", "id": "msg_refusal", "role": "assistant", "status": "completed",
            "content": [{"type": "refusal", "refusal": "Synthetic refusal"}],
        }])
        for body, expected in ((refusal, "refused"),
                               (openai_response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), "incomplete")):
            with self.subTest(expected=expected), transport([(200, {}, body)]):
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], expected)

    def test_raw_response_requires_text_items_and_rejects_tool_calls(self):
        invalid = (
            {"output_text": "Invented SDK shortcut. [job#42]"},
            openai_response(output=[{"type": "function_call", "name": "run_sql", "arguments": "{}"}]),
            openai_response(output=[]), openai_response(output="bad-shape"),
            openai_response(output=[{"type": "message", "role": "user", "status": "completed", "content": []}]),
        )
        for body in invalid:
            with self.subTest(body=body), transport([(200, {}, body)]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(len(observed.attempts), 1)

    def test_ollama_incomplete_and_tool_output_are_not_answers(self):
        for body, expected in (
            (ollama_response(done=False), "incomplete"),
            (ollama_response(done_reason="length"), "incomplete"),
            (ollama_response(message={"role": "assistant", "content": "Text [job#42]",
                                      "tool_calls": [{"function": {"name": "run_sql", "arguments": {}}}]}), "invalid_response"),
        ):
            with self.subTest(expected=expected), transport([(200, {}, body)]):
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=ollama_environment())
            self.assertEqual(result["status"], expected)

    def test_malformed_json_and_oversized_response_are_rejected_with_bounded_reads(self):
        for body in (b"not-json", b'{"text":"' + b"x" * 262144):
            with self.subTest(size=len(body)), transport([(200, {}, body)]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "invalid_response")
            self.assertLessEqual(observed.bodies[0].bytes_read, 131073)
            self.assertTrue(observed.bodies[0].closed)

    def test_content_length_and_encoding_are_checked_before_body_read(self):
        for headers in ({"Content-Length": "131073"}, {"Content-Encoding": "gzip"},
                        {"Content-Type": "text/html"}):
            with self.subTest(headers=headers), transport([(200, headers, b"private-raw-body")]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(observed.bodies[0].bytes_read, 0)
            self.assertTrue(observed.bodies[0].closed)

    def test_packet_contains_only_allowlisted_sanitized_evidence_and_context(self):
        supplied = evidence()
        supplied["selector"] = {"value": "https://contoh.id/private-route?token=selector-secret"}
        supplied["raw_html"] = "<html>raw-html-secret</html>"
        record = supplied["records"][0]
        record.update({
            "ref": "job#999", "error": "private-error-body", "evidence_path": "/srv/private-evidence",
            "screenshot": "private-base64", "reviewer_id": "private-reviewer",
            "reason": "contextual_gambling_phrase_detected", "initial_host": "a" * 63 + ".id",
            "review_excerpt": "Temuan https://contoh.id/private-route?token=url-secret /home/fauzi/private-file",
            "review": {
                "case_id": 7, "source_job_id": 42, "label": "needs_review",
                "excerpt": "token=review-secret", "review_notes": "private-review-note",
                "reviewer": "private-reviewer", "classification": {
                    "status": "medium_risk", "ml_source": "classic_ml", "ml_conf": 0.71,
                    "raw_text": "private-classifier-body",
                },
            },
        })
        record["classification"]["raw_text"] = "private-classifier-body"
        record["classification"]["reason"] = "contextual_gambling_phrase_detected"
        question = "Jelaskan https://contoh.id/secret-path?token=question-secret dan token=another-secret"
        history = [{"question": "token=history-secret", "answer": "Bukti lama. [job#42]",
                    "targets": ["private-context-target"]}]
        with transport([(200, {}, openai_response())]) as observed:
            result = analyst_llm.generate_answer(question, supplied, history=history, environ=openai_environment())
        self.assertEqual(result["status"], "answered")
        raw = observed.attempts[0]["body"].decode()
        for secret in ("selector-secret", "raw-html-secret", "private-error-body", "private-evidence",
                       "private-base64", "private-reviewer", "private-route", "url-secret", "private-file",
                       "private-review-note", "review-secret", "private-classifier-body", "secret-path",
                       "question-secret", "another-secret", "history-secret", "private-context-target"):
            self.assertNotIn(secret, raw)
        packet = json.loads(json.loads(raw)["input"][0]["content"])
        self.assertEqual(set(packet["evidence"]), {"records", "notes"})
        clean_record = packet["evidence"]["records"][0]
        self.assertEqual(clean_record["ref"], "job#42")
        self.assertEqual(clean_record["initial_host"], "a" * 63 + ".id")
        self.assertEqual(clean_record["reason"], "contextual_gambling_phrase_detected")
        self.assertEqual(clean_record["classification"]["reason"], "contextual_gambling_phrase_detected")
        self.assertEqual(clean_record["review"]["label"], "needs_review")
        self.assertEqual(clean_record["classification"]["ml_conf"], 0.71)
        self.assertNotIn("review_notes", clean_record["review"])

    def test_history_is_bounded_and_stale_answer_sources_are_omitted(self):
        history = [
            {"question": "oldest turn", "answer": "oldest answer [job#42]"},
            {"question": "q" * 800, "answer": "a" * 1000 + " [job#42]"},
            {"question": "Pertanyaan sebelumnya", "answer": "stale-answer [job#999]"},
        ]
        with transport([(200, {}, openai_response())]) as observed:
            result = analyst_llm.generate_answer("Mengapa?", evidence(), history=history, environ=openai_environment())
        self.assertEqual(result["status"], "answered")
        packet = json.loads(json.loads(observed.attempts[0]["body"])["input"][0]["content"])
        self.assertEqual(len(packet["history"]), 2)
        self.assertLessEqual(len(packet["history"][0]["question"]), 200)
        self.assertLessEqual(len(packet["history"][0]["answer"]), 500)
        self.assertNotIn("oldest", json.dumps(packet))
        self.assertNotIn("stale-answer", json.dumps(packet))
        self.assertNotIn("job#999", json.dumps(packet))

    def test_invalid_source_ids_do_not_authorize_generation(self):
        for records in ([{"id": True}], [{"id": "42"}], [{"id": -1}], [{"id": 2 ** 63}],
                        [evidence()["records"][0], evidence()["records"][0]]):
            with self.subTest(records=records), transport([(200, {}, openai_response())]) as observed:
                result = analyst_llm.generate_answer("Mengapa?", {"records": records, "notes": []}, environ=openai_environment())
            self.assertEqual(result["status"], "insufficient_evidence")
            self.assertEqual(observed.attempts, [])

    def test_input_and_packet_limits_prevent_any_provider_request(self):
        large = {"records": [], "notes": ["Tidak ada rincian tambahan. " * 30] * 12}
        for job_id in range(1, 11):
            row = dict(evidence()["records"][0], id=job_id, ref="job#%d" % job_id)
            row["data_gaps"] = ["Keterangan bukti belum tersedia. " * 30] * 12
            large["records"].append(row)
        for question, supplied in (("q" * 1001, evidence()), ("Mengapa?", large)):
            with self.subTest(size=len(question)), transport([(200, {}, openai_response())]) as observed:
                result = analyst_llm.generate_answer(question, supplied, environ=openai_environment())
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(observed.attempts, [])

    def test_ollama_has_a_conservative_prompt_budget(self):
        supplied = evidence()
        supplied["records"][0]["data_gaps"] = ["Keterangan bukti belum tersedia. " * 30] * 12
        with transport([(200, {}, ollama_response())]) as observed:
            result = analyst_llm.generate_answer("Mengapa?", supplied, environ=ollama_environment())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(observed.attempts, [])

    def test_two_typical_records_and_two_history_turns_fit_ollama_packet(self):
        supplied = evidence()
        supplied["records"].append(dict(supplied["records"][0], id=84, ref="job#84"))
        supplied["records"][0]["review"] = {
            "case_id": 7, "source_job_id": 42, "label": "needs_review", "excerpt": "Kutipan terbatas.",
        }
        history = [
            {"question": "Mengapa perlu diperiksa?", "answer": "Bukti belum lengkap. [job#42]"},
            {"question": "Apakah hasilnya sama?", "answer": "Hasil tersimpan berbeda. [job#42] [job#84]"},
        ]
        with transport([(200, {}, ollama_response("Bandingkan bukti tersimpan. [job#42] [job#84]"))]) as observed:
            result = analyst_llm.generate_answer("Apa bedanya?", supplied, history=history, environ=ollama_environment())
        self.assertEqual(result["status"], "answered")
        payload = json.loads(observed.attempts[0]["body"])
        self.assertLessEqual(sum(len(message["content"].encode()) for message in payload["messages"]), 6912)
        self.assertLessEqual(len(observed.attempts[0]["body"]), 32768)

    def test_invalid_classification_numbers_are_not_sent_as_evidence(self):
        supplied = evidence()
        supplied["records"][0]["classification"].update({
            "ml_conf": float("nan"), "indobert_conf": float("inf"), "bert_used": False,
            "indobert_pred": "gambling", "bert_ms": -1,
        })
        with transport([(200, {}, openai_response())]) as observed:
            result = analyst_llm.generate_answer("Mengapa?", supplied, environ=openai_environment())
        self.assertEqual(result["status"], "answered")
        packet = json.loads(json.loads(observed.attempts[0]["body"])["input"][0]["content"])
        classification = packet["evidence"]["records"][0]["classification"]
        self.assertNotIn("ml_conf", classification)
        self.assertNotIn("indobert_conf", classification)
        self.assertNotIn("indobert_pred", classification)
        self.assertNotIn("bert_ms", classification)

    def test_generation_admission_is_nonblocking_and_recovers_after_completion(self):
        entered, release = threading.Event(), threading.Event()
        first = []

        def hold_response():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test worker was not released")
            return (200, {}, openai_response())

        with transport([hold_response, (200, {}, openai_response())]) as observed:
            worker = threading.Thread(target=lambda: first.append(analyst_llm.generate_answer(
                "Pertama", evidence(), environ=openai_environment())))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                busy = analyst_llm.generate_answer("Kedua", evidence(), environ=openai_environment())
                self.assertEqual(busy["status"], "unavailable")
                self.assertEqual(len(observed.attempts), 1)
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(first[0]["status"], "answered")
            after = analyst_llm.generate_answer("Sesudahnya", evidence(), environ=openai_environment())
            self.assertEqual(after["status"], "answered")

    def test_cleanup_failure_does_not_escape_or_erase_confirmed_answer(self):
        with transport([(200, {}, openai_response())]):
            with patch.object(HTTPAdapter, "close", side_effect=OSError("private-cleanup-error")):
                result = analyst_llm.generate_answer("Mengapa?", evidence(), environ=openai_environment())
        self.assertEqual(result["status"], "answered")
        self.assertNotIn("private-cleanup-error", result["text"])


if __name__ == "__main__":
    unittest.main()
