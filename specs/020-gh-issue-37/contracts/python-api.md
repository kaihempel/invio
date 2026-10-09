# Python API Contract (gh-issue-37)

Signatures are normative; bodies are left to implementation.

## `invio.services.suggest` (new)

```python
MAX_TOPIC_CHARS: Final = 500
MAX_KEYWORDS: Final = 30
MAX_DESCRIPTION_CHARS: Final = 2000
TEMPERATURE: Final = 0.3


class SuggestionAnswer(BaseModel):  # LLM schema, extra="forbid", all fields required
    keywords_any: list[_Keyword]
    keywords_all: list[_Keyword]
    keywords_exclude: list[_Keyword]
    semantic_description: str
    suggested_sources_hint: str


@dataclass(frozen=True, kw_only=True, slots=True)
class SearchSuggestion:
    keywords_any: tuple[str, ...]
    keywords_all: tuple[str, ...]
    keywords_exclude: tuple[str, ...]
    semantic_description: str
    suggested_sources_hint: str
    warnings: tuple[str, ...] = ()

    def to_search_config(self) -> SearchConfig: ...
    def to_prompt_json(self) -> str: ...
    def replace(self, **fields: object) -> "SearchSuggestion": ...  # re-normalised


@dataclass(frozen=True, slots=True)
class ModelChoice:
    provider_name: str
    model: str


def normalise(answer: SuggestionAnswer | SearchSuggestion) -> SearchSuggestion: ...


def build_messages(
    topic: str,
    language: str,
    *,
    previous: SearchSuggestion | None = None,
    remark: str | None = None,
) -> tuple[str, str]: ...


def choose_model(
    settings: Settings,
    registry: ModelRegistry,
    *,
    provider: str | None,
    model: str | None,
) -> ModelChoice: ...  # raises LLMConfigError / LLMAuthError


async def suggest(
    provider: LLMProvider,
    model: str,
    *,
    topic: str,
    language: str,
) -> tuple[SearchSuggestion, Usage]: ...


async def refine(
    provider: LLMProvider,
    model: str,
    *,
    topic: str,
    language: str,
    previous: SearchSuggestion,
    remark: str,
) -> tuple[SearchSuggestion, Usage]: ...  # LLMError propagates; caller keeps `previous`


def search_yaml(suggestion: SearchSuggestion) -> str: ...  # hint as "# " comments + search: block
```

Imports allowed: `invio.config.*`, `invio.llm.base`, `invio.llm.registry`,
`invio.llm.factory` (for `registered_providers`, `has_credentials`). Not allowed: `typer`,
`invio.db`, `invio.graph`, `invio.cli`.

## `invio.llm.registry` (changed)

```python
class ModelRegistry:
    def most_expensive(self, provider: str) -> ModelInfo | None: ...
```

## `invio.llm.factory` (changed)

```python
def registered_providers() -> list[str]: ...  # sorted, after module discovery
def has_credentials(name: str, settings: Settings) -> bool: ...
```

## `invio.cli.suggest_flow` (new)

```python
Ask = Callable[[str | None, SearchSuggestion | None], SearchSuggestion]  # (remark, previous)

@dataclass(frozen=True) class Create:    suggestion: SearchSuggestion
@dataclass(frozen=True) class PrintYaml: suggestion: SearchSuggestion
@dataclass(frozen=True) class Discard:   pass
FlowResult = Create | PrintYaml | Discard

def format_llm_error(exc: LLMError, choice: ModelChoice) -> str: ...
# "Error: <provider>/<model>: <ErrorClass>: <message>"

def render_preview(s: SearchSuggestion, *, choice: ModelChoice, language: str) -> str: ...

def run_suggest_flow(
    prompter: Prompter, ask: Ask, *, first: SearchSuggestion, choice: ModelChoice,
    language: str, echo: Echo = typer.echo,
) -> FlowResult: ...                         # WizardAborted propagates
```

`ask` is only called for refinement (the first suggestion is passed in as `first`); an
`LLMError` raised by `ask` is reported via `echo(..., err=True)` and the loop continues with the
previous suggestion.

## `invio.cli.wizard` (changed)

```python
@dataclass(frozen=True, kw_only=True)
class WizardPrefill:
    keywords: Mapping[str, Sequence[str]]
    description: str
    language: str = "en"
    provider: str | None = None
    smart_model: str | None = None
    sources_note: str | None = None

    @classmethod
    def from_suggestion(
        cls,
        s: SearchSuggestion,
        *,
        language: str,
        choice: ModelChoice,
    ) -> "WizardPrefill": ...


def validate_language(value: str) -> bool | str: ...


def run_wizard(
    prompter: Prompter,
    checker: SourceChecker,
    *,
    registry: ModelRegistry | None,
    existing_names: Collection[str],
    name: str | None = None,
    default_timezone: str = "UTC",
    echo: Echo = typer.echo,
    prefill: WizardPrefill | None = None,  # NEW
) -> tuple[str, JobConfig] | None: ...
```

## `invio.cli.commands.job` (changed)

New command `suggest` (see `cli-job-suggest.md`) and test seams:

```python
def _make_suggest_provider(name: str, registry: ModelRegistry) -> LLMProvider: ...


# production: get_provider(name, get_settings(), registry=registry) — always the logging wrapper
```

Existing seams reused: `_make_prompter`, `_make_checker`, `_make_registry`, `_make_service`,
`_is_interactive`, `_default_timezone`.
