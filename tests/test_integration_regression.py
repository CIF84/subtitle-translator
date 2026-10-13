"""Regression coverage for v1's unconstrained text and discarded error metadata."""
from decimal import Decimal
import json
import re
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import translate_srt as tr
from test_translate_srt import SAMPLE, TRANSLATIONS, client_for, response, checkpoint


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
    return tr.parser().parse_args([str(source), "--batch-size", "1", "--output-dir", str(tmp_path / "out"),
                                  "--checkpoint-dir", str(tmp_path / "cp")])


def schema_allows_row(schema, row):
    """Check the contract portions relevant to this regression without new deps."""
    return any(row.get("id") in v["properties"]["id"]["enum"]
               and isinstance(row.get("text"), str)
               and re.fullmatch(v["properties"]["text"]["pattern"], row["text"])
               for v in schema["properties"]["entries"]["items"]["anyOf"])


def test_generated_constraints_accept_valid_formatted_czech(entries):
    schema = tr.response_schema(entries, "constrained")
    for entry in entries:
        assert schema_allows_row(schema, {"id": entry.id, "text": TRANSLATIONS[entry.id]})
    array = schema["properties"]["entries"]
    assert array["minItems"] == array["maxItems"] == len(entries)


@pytest.mark.parametrize("id,text", [
    ("1", "<i>JOHN: Ahoj, příteli.</i> - Přijdeš?"),  # collapsed two lines
    ("1", "JOHN: Ahoj, příteli.\n- Přijdeš?"),  # lost italics
    ("1", "<b>JOHN: Ahoj, příteli.</b>\n- Přijdeš?"),  # changed tags
    ("1", "<i>JOHN: Ahoj, příteli.</i>\nPřijdeš?"),  # lost speaker dash
    ("2", "bouchnutí dveří"),  # lost SDH brackets
    ("2", "[bouchnutí [dveří]]"),  # introduced SDH brackets
    ("3", "[jemná hudba]"),  # lost music markers
    ("99", "Navíc")])
def test_schema_and_validator_reject_missing_formatting_and_speakers(entries, id, text):
    if id == "99":
        assert not schema_allows_row(tr.response_schema(entries, "constrained"), {"id": id, "text": text})
    else:
        # Variable wrapping cannot be constrained with the former fixed tag pattern.
        # The same harmful outputs must still fail the independent production validator.
        with pytest.raises(tr.InvalidBatch):
            tr.validate_text(next(e for e in entries if e.id == id), text)


@pytest.mark.parametrize("source,translated", [
    (r"{\an8}<font color='#fff'>[whispering]</font>", r"{\an8}<font color='#fff'>[šeptem]</font>"),
    ("<i>- Hi.</i>\n<i>- Come here.</i>", "<i>- Ahoj.</i>\n<i>- Pojď sem.</i>"),
    ("Value < 3 and {x}.", "Hodnota < 3 a {x}."),
    ("♪ [music] ♪", "♪ [hudba] ♪")])
def test_schema_patterns_handle_ass_attributes_dashes_and_literal_punctuation(source, translated):
    entry = tr.Entry("1", "00:00:00,000 --> 00:00:01,000", source)
    assert re.fullmatch(tr.formatting_pattern(entry), source)
    assert re.fullmatch(tr.formatting_pattern(entry), translated)
    tr.validate_text(entry, translated)


def test_missing_duplicate_and_unexpected_identifiers_actionable(entries):
    raw = tr.dumps({"entries": [{"id": "1", "text": TRANSLATIONS["1"]},
                               {"id": "1", "text": TRANSLATIONS["1"]},
                               {"id": "9", "text": "Navíc"}], "glossary": []})
    with pytest.raises(tr.InvalidBatch) as exc:
        tr.validate_batch(raw, entries, [])
    assert exc.value.category == "identifiers"
    assert exc.value.details == {"expected_entries": 3, "received_entries": 3,
                                 "missing_ids": ["2", "3"], "duplicate_ids": ["1"], "unexpected_ids": ["9"]}


def test_report_records_line_failure_without_dialogue(job, entries, capsys):
    client = client_for(entries)
    broken = {**TRANSLATIONS, "1": TRANSLATIONS["1"].replace("\n", " ")}
    client.responses.create.side_effect = lambda **kw: response(entries[:1], translations=broken)
    with pytest.raises(tr.InvalidBatch, match="line_structure"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda p: "y", sleep=Mock())
    output = capsys.readouterr().out
    for text in ("Batch 1 validation failed:", "Category: line_structure", "Expected entries: 1",
                 "Received entries: 1", "Lines expected/received: 2 / 1", "Response status: completed",
                 "Input tokens: 100", "Output tokens: 80", "Cost incurred: $0.000422"):
        assert text in output
    assert "JOHN" not in output and "Přijdeš" not in output
    state = json.loads(checkpoint(job).read_text())
    assert state["requests"][0]["diagnostic"]["entry_id"] == "1"
    assert state["requests"][1]["retry_decision"] == "stop_repeated_failure"
    assert tr.totals(state)[0] == Decimal("0.000843")
    assert client.responses.create.call_count == 2


def test_corrective_retry_receives_feedback_and_succeeds(job, entries):
    client = client_for(entries)
    good = client.responses.create.side_effect
    calls = []
    def once_broken(**kw):
        calls.append(json.loads(kw["input"]))
        if len(calls) == 1:
            broken = {**TRANSLATIONS, "1": TRANSLATIONS["1"].replace("\n", " ")}
            return response(entries[:1], translations=broken)
        return good(**kw)
    client.responses.create.side_effect = once_broken
    tr.run(job, client_factory=lambda: client, input_fn=lambda p: "y", sleep=Mock())
    assert "validation_feedback" not in calls[0]
    assert calls[1]["validation_feedback"]["category"] == "line_structure"
    assert calls[1]["validation_feedback"]["entry_id"] == "1"
    assert calls[1]["validation_feedback"]["expected_lines"] == 2
    assert "validation_feedback" not in calls[2]  # reset for the next batch
    assert tr.totals(json.loads(checkpoint(job).read_text()))[0] == Decimal("0.001686")


def test_truncation_reason_usage_and_cost_saved_without_identical_retries(job, entries, capsys):
    client = client_for(entries)
    def truncated(**kw):
        return NS(id="resp_test", _request_id="req_test", status="incomplete",
                  incomplete_details=NS(reason="max_output_tokens"), output=[], output_text='{"entries":[',
                  usage=NS(input_tokens=100, output_tokens=kw["max_output_tokens"],
                           input_tokens_details=NS(cached_tokens=0), output_tokens_details=NS(reasoning_tokens=0)))
    client.responses.create.side_effect = truncated
    with pytest.raises(tr.InvalidBatch, match="output_truncated"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda p: "y", sleep=Mock())
    assert client.responses.create.call_count == 1
    record = json.loads(checkpoint(job).read_text())["requests"][0]
    assert record["response_status"] == "incomplete" and record["incomplete_reason"] == "max_output_tokens"
    assert record["response_id"] == "resp_test" and record["request_id"] == "req_test"
    assert record["output_tokens"] == record["max_output_tokens"]
    assert record["diagnostic"]["received_entries"] is None
    assert record["retry_decision"] == "stop_nonrepairable"
    assert Decimal(record["cost"]) > 0
    assert "Incomplete reason: max_output_tokens" in capsys.readouterr().out


def test_refusal_not_retried_or_logged(job, entries, capsys):
    client = client_for(entries)
    refused = response(entries[:1])
    refused.output = [NS(type="message", content=[NS(type="refusal", refusal="PRIVATE DIALOGUE OR SECRET")])]
    refused.output_text = ""
    client.responses.create.side_effect = lambda **kw: refused
    with pytest.raises(tr.InvalidBatch, match="refusal"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda p: "y", sleep=Mock())
    assert client.responses.create.call_count == 1
    assert "PRIVATE" not in capsys.readouterr().out and "PRIVATE" not in checkpoint(job).read_text()
    assert tr.totals(json.loads(checkpoint(job).read_text()))[0] == Decimal("0.0004215")


def test_unknown_identifier_never_echoes_dialogue_or_credentials(entries):
    raw = tr.dumps({"entries": [{"id": "SECRET_OR_DIALOGUE", "text": "private"}] * 3, "glossary": []})
    with pytest.raises(tr.InvalidBatch) as exc:
        tr.validate_batch(raw, entries, [])
    diagnostic = tr.diagnose_failure(NS(output_text=raw), entries, exc.value)
    assert "SECRET_OR_DIALOGUE" not in tr.dumps(diagnostic) and "private" not in tr.dumps(diagnostic)


def test_failed_usage_missing_metadata_survives(job, entries):
    client = client_for(entries)
    reply = response(entries[:1], usage=False, status="incomplete")
    reply.incomplete_details = NS(reason="max_output_tokens")
    client.responses.create.side_effect = lambda **kw: reply
    with pytest.raises(tr.TranslationError, match="no usage"):
        tr.run(job, client_factory=lambda: client, input_fn=lambda p: "y")
    record = json.loads(checkpoint(job).read_text())["requests"][0]
    assert record["response_status"] == "incomplete"
    assert record["diagnostic"]["category"] == "usage_unavailable"
    assert tr.totals(json.loads(checkpoint(job).read_text()))[1] > 0


def test_existing_v1_ledger_remains_compatible_and_charged(tmp_path, entries, price):
    pricing = json.loads((tr.BASE / "pricing.json").read_text())
    identity, key = tr.identity_for(SAMPLE.encode(), tr.Config(batch_size=1), pricing)
    state = tr.new_state(identity)
    # Exact legacy field set: new response metadata is optional when reading v1.
    state["requests"] = [{"status": "settled", "reservation": "0.03017175", "batch_first_id": "1",
                          "input_tokens": 1827, "output_tokens": out, "cached_tokens": cached,
                          "cost": str(tr.cost(price, 1827, out, cached))}
                         for out, cached in [(1523, 0), (1613, 1280), (1558, 1280)]]
    path = tmp_path / (key + ".json")
    tr.save_state(path, state)
    loaded = tr.load_state(path, identity, entries, tr.Config(batch_size=1), price)
    assert tr.totals(loaded) == (Decimal("0.02350575"), Decimal(0), 5481, 4694, 2560)
    with pytest.raises(tr.TranslationError, match="Budget guard"):
        tr.budget_guard(loaded, Decimal("0.024"), Decimal("0.001"))
    assert path.read_text() == tr.dumps(state) + "\n"


def test_new_schema_and_feedback_counted_in_budget(entries, price):
    config, counter = tr.Config(batch_size=1), tr.TokenCounter()
    payload = tr.payload_for(entries, entries[:1], config, {}, [])
    baseline = tr.estimate_request(entries[:1], config, payload, price, counter)
    payload["validation_feedback"] = {"category": "formatting", "entry_id": "1", "correction": "retry correctly"}
    retry = tr.estimate_request(entries[:1], config, payload, price, counter)
    assert retry.upper_input > baseline.upper_input and retry.upper_cost > baseline.upper_cost
    request = tr.request_for(entries[:1], config, payload, price, retry.output_cap)
    assert retry.upper_input >= len(tr.dumps(request).encode("utf-8"))


def test_tool_response_metadata_has_no_content_safely():
    reply = NS(status="failed", output=[NS(type="tool", content=17)], incomplete_details=None)
    assert tr.response_metadata(reply)["refusal"] is False


def test_schema_http_error_is_actionable_without_sensitive_error_text(job, entries, capsys):
    client = client_for(entries)
    class SchemaError(Exception):
        status_code = 400
        code = "invalid_json_schema"
    client.responses.create.side_effect = SchemaError("SECRET_HEADER_AND_DIALOGUE")
    with pytest.raises(tr.TranslationError):
        tr.run(job, client_factory=lambda: client, input_fn=lambda p: "y")
    output = capsys.readouterr().out
    assert "Batch 1 validation failed:" in output and "Category: api_schema" in output
    assert "SECRET" not in output and "SECRET" not in checkpoint(job).read_text()
    record = json.loads(checkpoint(job).read_text())["requests"][0]
    assert record["http_status"] == 400 and record["retry_decision"] == "stop_api_failure"
    assert client.responses.create.call_count == 1
