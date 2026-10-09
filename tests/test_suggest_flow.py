"""``invio.cli.suggest_flow``: preview, actions, refinement and editing, scripted."""

import pytest

from invio.cli.errors import format_llm_error
from invio.cli.prompts import WizardAborted
from invio.cli.suggest_flow import (
    Create,
    Discard,
    FlowResult,
    PrintYaml,
    render_preview,
    run_suggest_flow,
)
from invio.llm.base import LLMInvalidOutputError, LLMUnavailableError, Usage
from invio.services.suggest import ModelChoice, SearchSuggestion
from tests.cli_helpers import CLEAR, FakePrompter
from tests.suggest_helpers import DESCRIPTION, make_suggestion

CHOICE = ModelChoice("anthropic", "claude-big")
NEXT = "What next?"
ACTIONS = ["Create job", "Refine", "Edit a field", "Print YAML", "Discard"]


class Echoes:
    """Records ``echo(text, err=...)`` calls."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    def __call__(self, text: str = "", *, err: bool = False, nl: bool = True) -> None:
        (self.err if err else self.out).append(text)


class Asker:
    """A recording ``ask`` stub returning scripted suggestions or raising scripted errors."""

    def __init__(self, *results: SearchSuggestion | Exception) -> None:
        self._results = iter(results)
        self.calls: list[tuple[str, SearchSuggestion]] = []

    def __call__(self, remark: str, previous: SearchSuggestion) -> SearchSuggestion:
        self.calls.append((remark, previous))
        result = next(self._results)
        if isinstance(result, Exception):
            raise result
        return result


def _run(
    answers: list[str], *, first: SearchSuggestion | None = None, ask: Asker | None = None
) -> tuple[FlowResult, FakePrompter, Echoes, Asker]:
    prompter = FakePrompter(answers)
    echoes = Echoes()
    asker = ask or Asker()
    result = run_suggest_flow(
        prompter,
        asker,
        first=first or make_suggestion(),
        choice=CHOICE,
        language="de",
        echo=echoes,
    )
    return result, prompter, echoes, asker


# --- format_llm_error ------------------------------------------------------------------------


def test_format_llm_error_names_provider_model_class_and_message() -> None:
    text = format_llm_error(LLMUnavailableError("down\x1b[31m\nagain"), CHOICE)

    assert text == "Error: anthropic/claude-big: LLMUnavailableError: down again"


def test_format_llm_error_of_an_invalid_answer() -> None:
    error = LLMInvalidOutputError("bad", errors="e", usage=Usage(1, 1))

    assert format_llm_error(error, CHOICE).startswith(
        "Error: anthropic/claude-big: LLMInvalidOutputError: "
    )


# --- preview ---------------------------------------------------------------------------------


def test_preview_shows_choice_language_lists_description_hint_and_warnings() -> None:
    suggestion = make_suggestion(keywords_exclude=["Heat Pump"], keywords_any=["heat pump"])
    suggestion = suggestion.replace()  # normalised: the conflicting exclude is dropped and warned

    preview = render_preview(suggestion, choice=CHOICE, language="de")

    assert "anthropic / claude-big" in preview and "language: de" in preview
    assert "heat pump" in preview
    assert "Keywords — all of:   (none)" in preview
    assert "Keywords — exclude:  (none)" in preview
    assert "Relevant: heat pump news." in preview and "Not relevant: job ads." in preview
    assert "Trade portals" in preview and "Industry associations" in preview
    assert "! no exclusions" in preview
    assert "! 'Heat Pump' removed from exclude" in preview


def test_preview_prefixes_every_warning() -> None:
    suggestion = make_suggestion(keywords_any=[], keywords_exclude=[]).replace()  # two warnings

    preview = render_preview(suggestion, choice=CHOICE, language="en")

    assert "  ! no 'any' keywords" in preview and "  ! no exclusions" in preview


def test_preview_shows_the_full_description_and_strips_nothing_visible() -> None:
    long = "Relevant: " + "x" * 1900 + "\nNot relevant: y"

    preview = render_preview(
        make_suggestion(semantic_description=long), choice=CHOICE, language="en"
    )

    assert "x" * 1900 in preview


# --- actions ---------------------------------------------------------------------------------


def test_discard_returns_discard_without_asking() -> None:
    result, prompter, _, asker = _run(["Discard"])

    assert result == Discard()
    assert asker.calls == []
    assert prompter.asked == [NEXT]


def test_print_yaml_returns_the_first_suggestion() -> None:
    first = make_suggestion()

    result, _, _, _ = _run(["Print YAML"], first=first)

    assert result == PrintYaml(first)


def test_the_menu_offers_the_documented_choices() -> None:
    _, prompter, _, _ = _run(["Discard"])

    assert prompter.choices[NEXT] == ACTIONS


def test_the_preview_goes_to_the_echo_before_the_menu() -> None:
    _, _, echoes, _ = _run(["Discard"])

    assert any("Suggestion (anthropic / claude-big, language: de)" in text for text in echoes.out)
    assert echoes.err == []


def test_running_out_of_answers_aborts() -> None:
    with pytest.raises(WizardAborted):
        _run([])


# --- refine ----------------------------------------------------------------------------------


def test_refine_asks_with_the_remark_and_shows_the_new_suggestion() -> None:
    first, second = make_suggestion(), make_suggestion(keywords_any=["narrow"])

    result, _, echoes, asker = _run(
        ["Refine", "make it narrower", "Print YAML"], first=first, ask=Asker(second)
    )

    assert asker.calls == [("make it narrower", first)]
    assert result == PrintYaml(second)
    assert any("narrow" in text for text in echoes.out)


def test_two_refinements_pass_the_most_recent_suggestion() -> None:
    first = make_suggestion()
    second = make_suggestion(keywords_any=["two"])
    third = make_suggestion(keywords_any=["three"])

    result, _, _, asker = _run(
        ["Refine", "one", "Refine", "two", "Print YAML"], first=first, ask=Asker(second, third)
    )

    assert [call[1] for call in asker.calls] == [first, second]
    assert result == PrintYaml(third)


def test_an_empty_remark_is_rejected_inline_and_asked_again() -> None:
    result, prompter, _, asker = _run(
        ["Refine", "", "  ", "narrower", "Print YAML"], ask=Asker(make_suggestion())
    )

    assert [e for e in prompter.errors if e[0] == "How should it change?"]
    assert len(asker.calls) == 1 and asker.calls[0][0] == "narrower"
    assert isinstance(result, PrintYaml)


def test_the_remark_is_cleaned_of_control_characters() -> None:
    _, _, _, asker = _run(
        ["Refine", "tight\x1b[31mer\n", "Print YAML"], ask=Asker(make_suggestion())
    )

    assert asker.calls[0][0] == "tighter"


def test_a_failed_refinement_keeps_the_previous_suggestion() -> None:
    first = make_suggestion()

    result, _, echoes, _ = _run(
        ["Refine", "narrower", "Print YAML"],
        first=first,
        ask=Asker(LLMUnavailableError("provider down")),
    )

    assert result == PrintYaml(first)
    assert echoes.err == ["Error: anthropic/claude-big: LLMUnavailableError: provider down"]
    previews = [text for text in echoes.out if text.startswith("Suggestion (")]
    assert len(previews) == 2  # shown again after the failure


# --- edit ------------------------------------------------------------------------------------

FIELDS = ["Keywords — any of", "Keywords — all of", "Keywords — exclude", "Description"]


def test_editing_a_keyword_list_prefills_the_comma_joined_value() -> None:
    result, prompter, _, _ = _run(["Edit a field", "Keywords — exclude", "a, b", "Print YAML"])

    assert isinstance(result, PrintYaml)
    assert result.suggestion.keywords_exclude == ("a", "b")
    assert prompter.choices["Which field?"] == FIELDS
    assert prompter.defaults["Keywords — exclude"] == "Stellenangebot"


def test_editing_the_any_list_prefills_all_current_entries() -> None:
    _, prompter, _, _ = _run(["Edit a field", "Keywords — any of", "", "Discard"])

    assert prompter.defaults["Keywords — any of"] == "Wärmepumpe, heat pump"


def test_editing_the_description_is_multiline_with_the_current_default() -> None:
    result, prompter, _, _ = _run(["Edit a field", "Description", "Relevant: new", "Print YAML"])

    assert isinstance(result, PrintYaml)
    assert result.suggestion.semantic_description == "Relevant: new"
    assert prompter.defaults["Description"] == DESCRIPTION
    assert prompter.multiline["Description"] is True


def test_an_empty_description_is_rejected_inline_and_asked_again() -> None:
    result, prompter, _, _ = _run(
        ["Edit a field", "Description", CLEAR, "Relevant: ok", "Print YAML"]
    )

    assert [e for e in prompter.errors if e[0] == "Description"]
    assert isinstance(result, PrintYaml)
    assert result.suggestion.semantic_description == "Relevant: ok"


def test_too_many_keywords_are_rejected_inline() -> None:
    many = ", ".join(f"k{i}" for i in range(31))

    result, prompter, _, _ = _run(["Edit a field", "Keywords — any of", many, "k1", "Print YAML"])

    assert [e for e in prompter.errors if e[0] == "Keywords — any of"]
    assert isinstance(result, PrintYaml) and result.suggestion.keywords_any == ("k1",)


def test_an_exclude_equal_to_an_include_is_dropped_with_a_warning_in_the_next_preview() -> None:
    _, _, echoes, _ = _run(["Edit a field", "Keywords — exclude", "heat pump, spam", "Discard"])

    last_preview = [text for text in echoes.out if text.startswith("Suggestion (")][-1]
    assert "! 'heat pump' removed from exclude: it is also an include keyword" in last_preview
    assert "spam" in last_preview


def test_clearing_the_exclude_list_empties_it_and_warns() -> None:
    result, _, echoes, _ = _run(["Edit a field", "Keywords — exclude", CLEAR, "Print YAML"])

    assert isinstance(result, PrintYaml) and result.suggestion.keywords_exclude == ()
    last_preview = [text for text in echoes.out if text.startswith("Suggestion (")][-1]
    assert "Keywords — exclude:  (none)" in last_preview
    assert "! no exclusions" in last_preview


def test_edits_are_what_a_following_refinement_sends() -> None:
    _, _, _, asker = _run(
        ["Edit a field", "Keywords — any of", "edited", "Refine", "more", "Discard"],
        ask=Asker(make_suggestion()),
    )

    assert asker.calls[0][1].keywords_any == ("edited",)


def test_create_job_returns_the_current_edited_suggestion() -> None:
    result, _, _, _ = _run(["Edit a field", "Keywords — all of", "pump", "Create job"])

    assert isinstance(result, Create)
    assert result.suggestion.keywords_all == ("pump",)
