from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import translate_srt as tr

SAMPLE = '''1
00:00:01,000 --> 00:00:03,000
<i>JOHN: Hello, my friend.</i>
- Are you coming?

2
00:00:03,100 --> 00:00:05,000
[door slams]

3
00:00:05,100 --> 00:00:07,000
♪ [gentle music] ♪

'''
TRANSLATIONS = {"1": "<i>JOHN: Ahoj, příteli.</i>\n- Přijdeš?",
                "2": "[bouchnutí dveří]", "3": "♪ [jemná hudba] ♪"}


@pytest.fixture
def entries():
    return tr.parse_srt(SAMPLE)


@pytest.fixture
def price():
    return tr.load_pricing(tr.BASE / "pricing.json", "gpt-5.4-mini")[1]


@pytest.fixture
def job(tmp_path):
    source = tmp_path / "movie.en.srt"
    source.write_text(SAMPLE, encoding="utf-8")
    args = tr.parser().parse_args([str(source), "--batch-size", "1", "--output-dir", str(tmp_path / "out"),
                                  "--checkpoint-dir", str(tmp_path / "cp")])
    return args


def response(entries, *, translations=TRANSLATIONS, usage=True, status="completed", invalid=False, glossary=None):
    data = {"entries": [{"id": e.id, "text": translations[e.id]} for e in entries], "glossary": glossary or []}
    return NS(status=status, output_text="bad JSON" if invalid else tr.dumps(data),
              usage=NS(input_tokens=100, output_tokens=80, input_tokens_details=NS(cached_tokens=20)) if usage else None)


def client_for(entries):
    client = Mock()
    def create(**kwargs):
        payload = json.loads(kwargs["input"])
        selected = [e for e in entries if e.id in {r["id"] for r in payload["translate"]}]
        assert "timing" not in kwargs["input"] and "-->" not in kwargs["input"]
        assert kwargs["store"] is False and kwargs["service_tier"] == "default"
        assert kwargs["text"]["format"]["strict"] is True
        return response(selected)
    client.responses.create.side_effect = create
    return client


def checkpoint(args):
    return next(args.checkpoint_dir.glob("*.json"))


def test_parse_crlf_bom_multiline_sdh(entries):
    assert tr.parse_srt("\ufeff" + SAMPLE.replace("\n", "\r\n")) == entries
    assert len(entries) == 3 and "\n- Are" in entries[0].text
    assert entries[1].text == "[door slams]" and "♪" in entries[2].text


@pytest.mark.parametrize("source", ["", "garbage", SAMPLE.replace("00:00:01,000", "00:61:01,000"),
                                     SAMPLE.replace("00:00:03,000", "00:00:00,000"),
                                     SAMPLE.replace("\n2\n", "\n1\n"), SAMPLE.replace("[door slams]", "")])
def test_invalid_srt(source):
    with pytest.raises(tr.TranslationError):
        tr.parse_srt(source)


def test_preserve_nonsequential_identifiers_and_coordinates():
    source = "007\n100:00:00,000 --> 100:00:01,000 X1:10 X2:20 Y1:30 Y2:40\n<b>Hello</b>\n\n"
    entries = tr.parse_srt(source)
    text = tr.render_output(entries, {"007": "<b>Ahoj</b>"})
    assert text == source.replace("Hello", "Ahoj")
    assert tr.formatting_warnings(entries)


def test_encoding_error(job):
    job.input.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\n\xff\n")
    with pytest.raises(tr.TranslationError, match="UTF-8"):
        tr.run(job)


def test_batch_construction_context_unique(entries):
    config = tr.Config(batch_size=1, context=1)
    groups = tr.batches(entries, 1)
    assert [e.id for batch in groups for e in batch] == [e.id for e in entries]
    payload = tr.payload_for(entries, groups[1], config, {"1": TRANSLATIONS["1"]}, [])
    assert [row["id"] for row in payload["translate"]] == ["2"]
    assert payload["before"][0]["previous_translation"] == TRANSLATIONS["1"]
    assert [row["id"] for row in payload["after"]] == ["3"]
    assert "timing" not in tr.dumps(payload)


def test_structured_schema(entries):
    schema = tr.response_schema(entries)
    assert schema["additionalProperties"] is False
    row = schema["properties"]["entries"]["items"]
    assert row["properties"]["id"]["enum"] == ["1", "2", "3"]
    assert row["properties"]["text"] == {"type": "string"}
    assert row["required"] == ["id", "text"] and row["additionalProperties"] is False
    assert schema["properties"]["entries"]["minItems"] == schema["properties"]["entries"]["maxItems"] == 3


@pytest.mark.parametrize("raw", ["invalid", "[]", '{"entries":[],"glossary":[]}',
    '{"entries":[],"entries":[],"glossary":[]}',
    tr.dumps({"entries": [{"id": "1", "text": "Ahoj"}] * 3, "glossary": []}),
    tr.dumps({"entries": [{"id": str(i), "text": "Ahoj"} for i in (1, 2, 99)], "glossary": []}),
    tr.dumps({"entries": [{"id": e, "text": ""} for e in ("1", "2", "3")], "glossary": []}),
    tr.dumps({"entries": [{"id": i, "text": "Ahoj"} for i in (1, 2, 3)], "glossary": []})])
def test_reject_invalid_response(entries, raw):
    with pytest.raises(tr.InvalidBatch):
        tr.validate_batch(raw, entries, [])


@pytest.mark.parametrize("translated", ["JOHN: Ahoj.\n- Přijdeš?", "<b>JOHN: Ahoj.</b>\n- Přijdeš?",
                                       "<i>JOHN: Ahoj.</i>", "<i>JOHN: Ahoj.</i>\nPřijdeš?",
                                       "<i>JOHN: Ahoj.</i>\n\n- Přijdeš?", "x" * 1000, "\ud800"])
def test_format_line_speaker_and_unicode_rejection(entries, translated):
    with pytest.raises(tr.InvalidBatch):
        tr.validate_text(entries[0], translated)


def test_valid_response_order_mapping_and_terms(entries):
    reply = response(list(reversed(entries)), glossary=[{"source": "boss", "target": "šéf"}])
    translations, terms = tr.validate_batch(reply.output_text, entries, [])
    assert translations == TRANSLATIONS and terms[0]["target"] == "šéf"


def test_glossary_consistency(entries):
    reply = response(entries, glossary=[{"source": "boss", "target": "vedoucí"}])
    with pytest.raises(tr.InvalidBatch, match="glossary"):
        tr.validate_batch(reply.output_text, entries, [{"source": "boss", "target": "šéf"}])


def test_cost_with_cached_tokens(price):
    assert tr.cost(price, 1_000_000, 1_000_000) == Decimal("5.25")
    assert tr.cost(price, 100, 80, 20) == Decimal("0.0004215")


def test_estimation_accounts_prompts_context_and_retries(entries, price):
    counter = tr.TokenCounter()
    a = tr.preflight(entries, tr.batches(entries, 1), tr.Config(batch_size=1, context=0), {}, [], price, counter)
    b = tr.preflight(entries, tr.batches(entries, 1), tr.Config(batch_size=1, context=1), {}, [], price, counter)
    assert sum(x.input_tokens for x in b) > sum(x.input_tokens for x in a)
    assert sum(x.input_tokens for x in a) > counter.count(SAMPLE)
    assert all(x.upper_cost > x.expected_cost and x.output_cap > x.output_tokens for x in b)


def test_unknown_pricing_fail_closed(job):
    job.model = "unverified-model"
    factory = Mock()
    with pytest.raises(tr.TranslationError, match="pricing"):
        tr.run(job, client_factory=factory)
    factory.assert_not_called()


@pytest.mark.parametrize("field,value", [("input_per_million", "NaN"), ("output_per_million", "-1"),
                                         ("max_output_tokens", 0), ("structured_output", "yes")])
def test_bad_pricing(job, tmp_path, field, value):
    data = json.loads(job.pricing.read_text())
    data["models"][job.model][field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(tr.TranslationError):
        tr.load_pricing(path, job.model)


@pytest.mark.parametrize("answer", ["", "n", "no", "ok", "auto", "true"])
def test_mandatory_approval_default_no(job, answer, capsys):
    factory = Mock()
    assert tr.run(job, client_factory=factory, input_fn=lambda prompt: answer) == 0
    factory.assert_not_called()
    assert not list(job.checkpoint_dir.glob("*.json"))
    assert "Cancelled" in capsys.readouterr().out


def test_eof_declines():
    def eof(prompt):
        raise EOFError
    assert tr.approve(eof) is False


def test_dry_run_no_api_no_key_no_mutation(job, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    job.dry_run = True
    factory, approval = Mock(), Mock()
    assert tr.run(job, client_factory=factory, input_fn=approval) == 0
    factory.assert_not_called()
    approval.assert_not_called()
    assert not job.checkpoint_dir.exists() and not job.output_dir.exists()
    output = capsys.readouterr().out
    assert "Entries: 3" in output and "Conservative" in output and "Zero API calls" in output


def test_budget_blocks_first_request_and_availability(job):
    job.max_cost = Decimal("0.000001")
    factory = Mock()
    with pytest.raises(tr.TranslationError, match="Budget guard"):
        tr.run(job, client_factory=factory, input_fn=lambda prompt: "y")
    factory.assert_not_called()


def test_approved_complete_structure_usage_and_output(job, entries, capsys):
    client = client_for(entries)
    assert tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y") == 0
    client.models.retrieve.assert_called_once_with(job.model)
    assert client.responses.create.call_count == 3
    client.close.assert_called_once()
    output = job.output_dir / "movie.en.cs.srt"
    parsed = tr.parse_srt(output.read_bytes().decode("utf-8"))
    assert [(e.id, e.timing) for e in parsed] == [(e.id, e.timing) for e in entries]
    assert [e.text for e in parsed] == list(TRANSLATIONS.values())
    state = json.loads(checkpoint(job).read_text())
    assert tr.totals(state)[:4] == (Decimal("0.0012645"), Decimal(0), 300, 240)
    assert "Validation: PASSED" in capsys.readouterr().out


def test_invalid_paid_response_usage_is_saved_and_retried(job, entries):
    client = client_for(entries)
    success = client.responses.create.side_effect
    calls = 0
    def once_bad(**kwargs):
        nonlocal calls
        calls += 1
        return response(entries[:1], invalid=True) if calls == 1 else success(**kwargs)
    client.responses.create.side_effect = once_bad
    sleep = Mock()
    tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y", sleep=sleep)
    assert calls == 4 and sleep.call_args_list[0].args == (1,)
    state = json.loads(checkpoint(job).read_text())
    assert tr.totals(state)[0] == Decimal("0.001686")


def test_bounded_validation_retries_no_partial_output(job, entries):
    client = client_for(entries)
    client.responses.create.side_effect = lambda **kw: response(entries[:1], invalid=True)
    with pytest.raises(tr.InvalidBatch):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y", sleep=Mock())
    assert client.responses.create.call_count == 2
    assert not job.output_dir.exists()
    assert len(json.loads(checkpoint(job).read_text())["requests"]) == 2


def test_transient_retry_backoff_reserves_unknown_usage(job, entries):
    client = client_for(entries)
    original = client.responses.create.side_effect
    calls = 0
    def twice_failed(**kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("sensitive error should not be displayed")
        return original(**kwargs)
    client.responses.create.side_effect = twice_failed
    sleep = Mock()
    tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y", sleep=sleep)
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    state = json.loads(checkpoint(job).read_text())
    assert tr.totals(state)[1] > 0 and tr.totals(state)[0] == Decimal("0.0012645")


def test_nontransient_failure_no_retry_and_sanitized(job, entries):
    client = client_for(entries)
    class Denied(Exception):
        status_code = 401
    client.responses.create.side_effect = Denied("SECRET_KEY_DO_NOT_PRINT")
    with pytest.raises(tr.TranslationError) as exc:
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y", sleep=Mock())
    assert "SECRET" not in str(exc.value) and client.responses.create.call_count == 1


def test_usage_missing_stops_without_retry(job, entries):
    client = client_for(entries)
    client.responses.create.side_effect = lambda **kw: response(entries[:1], usage=False)
    with pytest.raises(tr.TranslationError, match="no usage"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y")
    state = json.loads(checkpoint(job).read_text())
    assert tr.totals(state)[1] > 0 and client.responses.create.call_count == 1


def test_budget_checks_every_request(job, entries, price):
    payload = tr.payload_for(entries, entries[:1], tr.Config(batch_size=1), {}, [])
    first = tr.estimate_request(entries[:1], tr.Config(batch_size=1), payload, price, tr.TokenCounter())
    job.max_cost = first.upper_cost + Decimal("0.000001")
    client = client_for(entries)
    # Usage is just within the first reservation, leaves insufficient room for another request.
    def expensive(**kwargs):
        return NS(status="completed", output_text=response(entries[:1]).output_text,
                  usage=NS(input_tokens=first.upper_input, output_tokens=first.output_cap,
                           input_tokens_details=NS(cached_tokens=0)))
    client.responses.create.side_effect = expensive
    with pytest.raises(tr.TranslationError, match="Budget guard"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y")
    assert client.responses.create.call_count == 1
    assert json.loads(checkpoint(job).read_text())["translations"] == {"1": TRANSLATIONS["1"]}


def test_interrupt_resume_preserves_completed_usage_and_unknown(job, entries, capsys):
    client = client_for(entries)
    original = client.responses.create.side_effect
    calls = 0
    def interrupt(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(**kwargs)
    client.responses.create.side_effect = interrupt
    with pytest.raises(KeyboardInterrupt):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y")
    before = json.loads(checkpoint(job).read_text())
    assert before["translations"] == {"1": TRANSLATIONS["1"]} and before["requests"][-1]["status"] == "pending"
    job.resume = True
    resumed = client_for(entries)
    approval = Mock(return_value="yes")
    tr.run(job, client_factory=lambda: resumed, input_fn=approval)
    approval.assert_called_once()
    assert resumed.responses.create.call_count == 2
    after = json.loads(checkpoint(job).read_text())
    assert tr.totals(after)[0] == Decimal("0.0012645") and tr.totals(after)[1] > 0
    assert after["requests"][1]["status"] == "unknown"
    assert "Previous actual spending: $0.000422" in capsys.readouterr().out


def test_resume_budget_includes_previous_attempts(job, entries):
    client = client_for(entries)
    original = client.responses.create.side_effect
    def crash(**kwargs):
        if json.loads(kwargs["input"])["translate"][0]["id"] == "2":
            raise KeyboardInterrupt
        return original(**kwargs)
    client.responses.create.side_effect = crash
    with pytest.raises(KeyboardInterrupt):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y")
    job.resume, job.max_cost = True, Decimal("0.0005")
    factory = Mock()
    with pytest.raises(tr.TranslationError, match="Budget guard"):
        tr.run(job, client_factory=factory, input_fn=lambda prompt: "y")
    factory.assert_not_called()


@pytest.mark.parametrize("change", ["source", "target", "model", "batch_size", "context", "prompt", "pricing"])
def test_resume_identity_compatibility(entries, price, change):
    pricing = json.loads((tr.BASE / "pricing.json").read_text())
    _, baseline = tr.identity_for(SAMPLE.encode(), tr.Config(), pricing)
    cfg = tr.Config()
    raw = SAMPLE.encode()
    if change == "source":
        raw += b"\n"
    elif change in ("target", "model", "batch_size", "context"):
        values = tr.asdict(cfg)
        values[change] = {"target": "de", "model": "gpt-5.4", "batch_size": 50, "context": 4}[change]
        cfg = tr.Config(**values)
    elif change == "pricing":
        pricing["last_verified"] = "2026-10-10"
    elif change == "prompt":
        # Prompt fingerprint is independently incorporated, without relying on manual version bumps.
        baseline_identity, _ = tr.identity_for(raw, cfg, pricing)
        assert "prompt_sha256" in baseline_identity
        from unittest.mock import patch
        with patch.object(tr, "SYSTEM_PROMPT", tr.SYSTEM_PROMPT + "changed"):
            _, changed = tr.identity_for(raw, cfg, pricing)
        assert changed != baseline
        return
    assert tr.identity_for(raw, cfg, pricing)[1] != baseline


def test_existing_job_requires_resume_and_no_reapproval_auto(job, entries):
    tr.run(job, client_factory=lambda: client_for(entries), input_fn=lambda prompt: "y")
    with pytest.raises(tr.TranslationError, match="--resume"):
        tr.run(job, client_factory=Mock(), input_fn=lambda prompt: "y")
    job.resume = True
    factory = Mock()
    tr.run(job, client_factory=factory, input_fn=lambda prompt: "n")
    factory.assert_not_called()


def test_corrupt_checkpoint_fails_closed_preserves_file(job, entries):
    tr.run(job, client_factory=lambda: client_for(entries), input_fn=lambda prompt: "y")
    path = checkpoint(job)
    path.write_text('{"identity": "bad"}')
    job.resume = True
    with pytest.raises(tr.TranslationError, match="corrupt"):
        tr.run(job, client_factory=Mock(), input_fn=lambda prompt: "y")
    assert path.read_text() == '{"identity": "bad"}'


def test_corrupt_usage_cannot_reduce_ledger(job, entries):
    tr.run(job, client_factory=lambda: client_for(entries), input_fn=lambda prompt: "y")
    path = checkpoint(job)
    data = json.loads(path.read_text())
    data["requests"][0]["cost"] = "0"
    path.write_text(json.dumps(data))
    job.resume = True
    with pytest.raises(tr.TranslationError, match="corrupt"):
        tr.run(job, client_factory=Mock(), input_fn=lambda prompt: "y")


def test_atomic_write_failure_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "completed.srt"
    path.write_text("previous valid result")
    def fail(*args):
        raise OSError("disk problem")
    monkeypatch.setattr(tr.os, "replace", fail)
    with pytest.raises(OSError):
        tr.atomic_write(path, "replacement")
    assert path.read_text() == "previous valid result" and len(list(tmp_path.iterdir())) == 1


def test_final_validation_missing_extra_invalid(entries):
    for translations in ({"1": TRANSLATIONS["1"]}, {**TRANSLATIONS, "99": "Navíc"}, {**TRANSLATIONS, "2": ""}):
        with pytest.raises(tr.TranslationError):
            tr.render_output(entries, translations)


def test_quality_warnings_are_advisory(entries):
    warnings = tr.quality_warnings(entries, {e.id: e.text for e in entries}, "en", "cs")
    assert warnings and "identical" in warnings[0]
    assert tr.render_output(entries, {e.id: e.text for e in entries})


def test_key_environment_precedence_and_dotenv(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('OPENAI_API_KEY="local-test-value"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-value")
    assert tr.load_api_key(path) == "environment-test-value"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert tr.load_api_key(path) == "local-test-value"
    path.write_text("OPENAI_API_KEY=\n")
    with pytest.raises(tr.TranslationError):
        tr.load_api_key(tmp_path / "absent")


def test_job_lock_prevents_concurrent_spending(tmp_path):
    path = tmp_path / "job.lock"
    with tr.job_lock(path):
        with pytest.raises(tr.TranslationError, match="already running"):
            with tr.job_lock(path):
                pass
    with tr.job_lock(path):
        pass


def test_json_mode_explicit_configuration(entries, price):
    alternative = {**price, "structured_output": False, "reasoning_effort": None}
    args = tr.request_for(entries, tr.Config(reasoning_effort=None), {}, alternative, 1000)
    assert args["text"]["format"] == {"type": "json_object"} and "reasoning" not in args


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_budget_cli(value):
    with pytest.raises(SystemExit):
        tr.parser().parse_args(["file.srt", "--max-cost", value])


def test_create_sdk_client_local_only(monkeypatch):
    import openai
    constructor = Mock()
    monkeypatch.setattr(openai, "OpenAI", constructor)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-value")
    tr.create_client()
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["base_url"] == "https://api.openai.com/v1"


def test_zero_number_and_whitespace_separators():
    sample = "\n \n0\n00:00:00,000 --> 00:00:01,000\nHello\n \n\t\n9\n00:00:01,000 --> 00:00:02,000\nHi\n \n"
    entries = tr.parse_srt(sample)
    assert [e.id for e in entries] == ["0", "9"]
    assert tr.render_output(entries, {"0": "Ahoj", "9": "Čau"}).startswith("0\n")


def test_sdh_brackets_and_ass_override_preserved():
    entry = tr.Entry("1", "00:00:00,000 --> 00:00:01,000", r"{\an8}[whispering] Hello")
    tr.validate_text(entry, r"{\an8}[šeptem] Ahoj")
    for wrong in ("[šeptem] Ahoj", r"{\an8}šeptem Ahoj"):
        with pytest.raises(tr.InvalidBatch):
            tr.validate_text(entry, wrong)


def test_bad_tokenizer_cache_never_downloads(tmp_path, monkeypatch):
    import tiktoken
    url = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
    name = tr.hashlib.sha1(url.encode()).hexdigest()
    (tmp_path / name).write_bytes(b"bad cache")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    download = Mock(side_effect=AssertionError("should not load bad tokenizer cache"))
    monkeypatch.setattr(tiktoken, "get_encoding", download)
    counter = tr.TokenCounter()
    assert counter.encoding is None and counter.count("abc") == 1
    download.assert_not_called()


def test_approved_completed_resume_regenerates_without_api(job, entries):
    tr.run(job, client_factory=lambda: client_for(entries), input_fn=lambda prompt: "y")
    output = job.output_dir / "movie.en.cs.srt"
    output.unlink()
    job.resume = True
    factory = Mock()
    assert tr.run(job, client_factory=factory, input_fn=lambda prompt: "y") == 0
    factory.assert_not_called()
    assert output.exists()


def test_model_unavailable_stops_before_generation(job, entries):
    client = client_for(entries)
    client.models.retrieve.side_effect = RuntimeError("PRIVATE_HEADER")
    with pytest.raises(tr.TranslationError) as exc:
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y")
    assert "PRIVATE_HEADER" not in str(exc.value)
    client.responses.create.assert_not_called()
    client.close.assert_called_once()


def test_retry_cannot_exceed_remaining_budget(job, entries, price):
    cfg = tr.Config(batch_size=1)
    payload = tr.payload_for(entries, entries[:1], cfg, {}, [])
    first = tr.estimate_request(entries[:1], cfg, payload, price, tr.TokenCounter())
    job.max_cost = first.upper_cost + Decimal("0.000001")
    client = client_for(entries)
    client.responses.create.side_effect = TimeoutError("lost connection")
    with pytest.raises(tr.TranslationError, match="Budget guard"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y", sleep=Mock())
    assert client.responses.create.call_count == 1
    state = json.loads(checkpoint(job).read_text())
    assert tr.totals(state)[1] == first.upper_cost


def test_real_sdk_through_mocked_http_transport(job, entries):
    """Exercise official SDK request serialization/response parsing without a socket."""
    import httpx
    import openai
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/models/gpt-5.4-mini"):
            return httpx.Response(200, json={"id": job.model, "object": "model", "created": 0, "owned_by": "openai"})
        assert request.url.path == "/v1/responses"
        data = json.loads(request.content)
        assert data["reasoning"] == {"effort": "none"} and data["text"]["format"]["strict"] is True
        ids = {row["id"] for row in json.loads(data["input"])["translate"]}
        text = response([e for e in entries if e.id in ids]).output_text
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 0, "model": job.model,
            "status": "completed", "error": None, "incomplete_details": None,
            "instructions": data["instructions"], "metadata": {}, "parallel_tool_calls": True,
            "temperature": 1, "top_p": 1, "tool_choice": "auto", "tools": [],
            "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20},
                      "output_tokens": 80, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 180}})
    client = openai.OpenAI(api_key="fake-local-test-key", base_url="https://api.openai.com/v1", max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y") == 0
    assert len(seen) == 4
    assert tr.totals(json.loads(checkpoint(job).read_text()))[0] == Decimal("0.0012645")


def test_checkpoint_dry_run_readonly_preserves_pending_record(job, entries):
    client = client_for(entries)
    client.responses.create.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        tr.run(job, client_factory=lambda: client, input_fn=lambda prompt: "y")
    path = checkpoint(job)
    previous = path.read_bytes()
    job.resume, job.dry_run = True, True
    tr.run(job, client_factory=Mock(), input_fn=Mock())
    assert path.read_bytes() == previous


def test_output_cannot_overwrite_input(job):
    job.input = job.input.with_name("movie.en.cs.srt")
    job.input.write_text(SAMPLE, encoding="utf-8")
    job.output_dir = job.input.parent
    job.dry_run = True
    # The default always appends a language suffix, but explicit Path equality guard
    # still protects unusual output-dir/symlink combinations.
    target = job.input.parent / "movie.en.cs.cs.srt"
    target.symlink_to(job.input)
    with pytest.raises(tr.TranslationError, match="Output path equals"):
        tr.run(job)
