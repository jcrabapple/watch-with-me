"""Optional Jev entity selection (System One decisions API).

Strictly optional: with ``jev_enabled`` off, or no key configured, or the
provider unreachable, every entry point here degrades to ``None`` and the
build follows its existing heuristic — no exception, no log noise, no UI
state. Nothing in this module imports or assumes the ``hermes-jev`` plugin;
it is a stdlib-only client for the two public transports.

The decision model answers ONE question per segment: of the entities the
extraction call just named (plus an explicit NONE option), which one or two
are worth a Wikipedia fetch and a context note. Selection happens BEFORE
the expensive fetch, not filtering after it.

Key handling: the API key is write-only. It lives in the plugin data dir at
mode 0600 (``jev-api-key.json``, same convention as the Plex credentials),
is never written to ``config.yaml``, never returned in an API payload (at
most ``{"configured": true, "last4": "abcd"}``), and never logged. The
``TYPESAFE_API_KEY`` / ``OPENROUTER_API_KEY`` env vars are the alternative
route; both are declared as optional env in plugin.yaml.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error  # noqa: F401 - tests monkeypatch jev.urllib.request
import urllib.request  # noqa: F401 - tests monkeypatch jev.urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import store

OPENROUTER_BASE_URL = "https://openrouter.ai"
OPENROUTER_PATH = "/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"
TYPESAFE_BASE_URL = "https://api.typesafe.ai"
TYPESAFE_PATH = "/v1/systemone"
TYPESAFE_MODEL = "jev-latest"

DEFAULT_TIMEOUT = 5.0        # seconds; a selection must not stall a segment
RETRIES = 1                  # one retry after a failure, then fail open
_BACKOFF_SECONDS = (0.4,)    # sleep before each retry (tests may shrink)

# Entity-selection contract (mirrors entities.py caps).
MAX_CANDIDATES = 5           # the extraction call may name up to five
CHOSEN_LIMIT = 2             # at most the chosen two are offered for fetching
NONE_ID = "NONE"             # explicit "nothing here is worth expanding"
CONFIDENT_THRESHOLD = 0.6    # below this the winner is advisory only

KEY_FILE = "jev-api-key.json"
DECISIONS_LOG = "jev-decisions.jsonl"
LOG_CAP_BYTES = 4 * 1024 * 1024   # rotate at a few MB

PROVIDERS = ("auto", "typesafe", "openrouter")

#: The one-line dashboard copy (settings block; nothing more, per spec).
OPTIONAL_LINE = ("Optional: use a decision model to choose which entities "
                 "are worth expanding")


class JevError(RuntimeError):
    """A provider or contract failure. Callers treat it as 'fall back'."""


# -- key storage (the only writers/readers of the key file) --------------------

def api_key_path(path: Path | None = None) -> Path:
    return path or store.data_dir() / KEY_FILE


def save_api_key(key: str, *, path: Path | None = None) -> Path:
    """Write the key file at mode 0600. Never logs, never returns the key."""
    clean = (key or "").strip()
    if not clean:
        raise ValueError("api_key must be a non-empty string")
    target = api_key_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"api_key": clean}), encoding="utf-8")
    os.chmod(target, 0o600)
    os.chmod(target.parent, 0o700)
    return target


def load_api_key(*, path: Path | None = None) -> str:
    """The configured key, or "" — silently, when missing or unreadable."""
    target = api_key_path(path)
    if not target.exists():
        return ""
    try:
        data = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    key = data.get("api_key") if isinstance(data, dict) else ""
    return key.strip() if isinstance(key, str) else ""


def key_status(*, path: Path | None = None) -> dict:
    """The most an API payload may reveal about the key."""
    key = load_api_key(path=path)
    return {"configured": bool(key), "last4": key[-4:] if key else None}


# -- configuration resolution --------------------------------------------------

@dataclass(frozen=True)
class Config:
    provider: str      # "typesafe" | "openrouter"
    api_key: str
    model: str         # the model id sent in the request body
    base_url: str

    @property
    def path(self) -> str:
        return TYPESAFE_PATH if self.provider == "typesafe" else OPENROUTER_PATH


_ENV_NAMES = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY",
              "TYPESAFE_BASE_URL", "OPENROUTER_BASE_URL")


def _env_view(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    """Only the variables this module consults, never the whole environ."""
    if env is not None:
        return env
    return {name: os.environ[name] for name in _ENV_NAMES if name in os.environ}


def _resolve_env(name: str, env: Optional[Mapping[str, str]]) -> str:
    """Read an env var the way the host keeps secrets (scope first, env fall
    back). Never raises: a missing secret means 'no key', not an error."""
    environ = _env_view(env)
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]

        try:
            scoped = str(get_secret(name, "") or "").strip()
        except Exception:
            scoped = ""
        if scoped:
            return scoped
    except ImportError:
        pass
    return (environ.get(name) or "").strip()


def _config_for(provider: str, key: str, model: str,
                env: Optional[Mapping[str, str]]) -> Config:
    environ = _env_view(env)
    if provider == "typesafe":
        base = (environ.get("TYPESAFE_BASE_URL") or TYPESAFE_BASE_URL)
        return Config(provider, key, model or TYPESAFE_MODEL, base.rstrip("/"))
    base = (environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL)
    return Config(provider, key, model or OPENROUTER_MODEL, base.rstrip("/"))


def resolve(settings, *, env: Optional[Mapping[str, str]] = None) -> Config | None:
    """The effective Jev config for these settings, or None when disabled or
    unkeyed. ``jev_provider: auto`` picks by which key is present: an
    explicitly configured key first (routed to OpenRouter, the transport the
    model id ``typesafe/jev-1.13`` lives on — set ``jev_provider: typesafe``
    for a direct TypeSafe key), then ``TYPESAFE_API_KEY``, then
    ``OPENROUTER_API_KEY``."""
    if not getattr(settings, "jev_enabled", False):
        return None
    provider = (getattr(settings, "jev_provider", "auto") or "auto").strip().lower()
    if provider not in PROVIDERS:
        provider = "auto"
    explicit = (getattr(settings, "jev_api_key", "") or "").strip() \
        or load_api_key()
    model = (getattr(settings, "jev_model", "") or "").strip()

    if provider in ("typesafe", "openrouter"):
        var = "TYPESAFE_API_KEY" if provider == "typesafe" else "OPENROUTER_API_KEY"
        key = explicit or _resolve_env(var, env)
        if not key:
            return None
        return _config_for(provider, key, model, env)
    if explicit:
        return _config_for("openrouter", explicit, model, env)
    key = _resolve_env("TYPESAFE_API_KEY", env)
    if key:
        return _config_for("typesafe", key, model, env)
    key = _resolve_env("OPENROUTER_API_KEY", env)
    if key:
        return _config_for("openrouter", key, model, env)
    return None


# -- transport + client ---------------------------------------------------------

TransportResult = tuple[int, bytes]
Transport = Callable[[str, dict[str, str], bytes, float], TransportResult]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes,
                      timeout: float) -> TransportResult:
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""


@dataclass(frozen=True)
class Decision:
    question_id: str
    choice: str
    probabilities: dict[str, float]
    model: str
    provider: str
    latency_ms: float


def validate_choice(choice: str, probabilities: Mapping[str, Any],
                    offered_ids: list[str]) -> None:
    """Strict contract check. Any violation raises JevError (fail open)."""
    if choice not in offered_ids:
        raise JevError(f"choice {choice!r} is not one of the offered ids")
    probs: dict[str, float] = {}
    for cid in offered_ids:
        raw = probabilities.get(cid) if isinstance(probabilities, Mapping) else None
        if raw is None:
            raise JevError(f"probabilities missing offered id {cid!r}")
        try:
            probs[cid] = float(raw)
        except (TypeError, ValueError) as exc:
            raise JevError(f"probability for {cid!r} is not a number") from exc
    top = max(probs.items(), key=lambda kv: kv[1])[0]
    if top != choice:
        raise JevError(f"argmax {top!r} does not equal the reported choice {choice!r}")


class Client:
    """One decision call per segment; timeout, one retry, strict validation.

    ``transport(url, headers, body, timeout) -> (status, bytes)`` is injected
    by tests; production uses urllib via :func:`_urllib_transport`.
    """

    def __init__(self, config: Config, *, transport: Transport | None = None,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = RETRIES,
                 backoff: tuple[float, ...] = _BACKOFF_SECONDS) -> None:
        self.config = config
        self._transport = transport or _urllib_transport
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff

    def decide(self, *, state: Any, question_id: str,
               criteria: dict[str, str], goal: str,
               rules: list[str]) -> Decision:
        """POST one choice question. Raises JevError on any failure; the
        caller decides what fallback applies. Never logs the key."""
        questions = {question_id: {
            "type": "choice",
            "criteria": criteria,          # a RECORD, not an array (400s)
            "instructions": {"goal": goal, "rules": rules},
        }}
        payload = {"state": state, "model": self.config.model,
                   "questions": questions}
        body = json.dumps(payload, separators=(",", ":"),
                          ensure_ascii=False, default=str).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "watch-with-me/jev",
        }
        url = self.config.base_url + self.config.path
        started = time.monotonic()
        status, raw = self._post(url, headers, body)
        latency_ms = (time.monotonic() - started) * 1000
        if status < 200 or status >= 300:
            raise JevError(f"Jev API returned HTTP {status}")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise JevError("Jev API returned malformed JSON") from exc
        answers = data.get("answers") if isinstance(data, dict) else None
        answer = answers.get(question_id) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            raise JevError("Jev API response is missing the answer")
        choice = str(answer.get("choice") or "")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping):
            raise JevError("Jev API answer has no probabilities record")
        # Strict contract: choice is one offered id, every offered id is
        # present, and the argmax equals the reported choice. Thresholding
        # happens on probabilities[choice] by the caller — never on any
        # confidence field.
        validate_choice(choice, probabilities, list(criteria))
        return Decision(question_id=question_id, choice=choice,
                        probabilities={str(k): float(v) for k, v in
                                       probabilities.items()},
                        model=str(data.get("model") or self.config.model),
                        provider=self.config.provider,
                        latency_ms=latency_ms)

    def _post(self, url: str, headers: dict[str, str], body: bytes
              ) -> TransportResult:
        attempt = 0
        while True:
            try:
                return self._transport(url, headers, body, self._timeout)
            except Exception as exc:
                if attempt >= self._retries:
                    raise JevError(f"Jev transport failed: {type(exc).__name__}") \
                        from exc
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                if delay > 0:
                    time.sleep(delay)
                attempt += 1


# -- decision log ---------------------------------------------------------------

def decisions_log_path(path: Path | None = None) -> Path:
    return path or store.data_dir() / DECISIONS_LOG


def log_decision(record: Mapping[str, Any], *, path: Path | None = None) -> None:
    """Append one decision to the JSONL log (timestamp, question id, choice,
    probability, latency, provider, model — never key material). Rotates the
    file once past the cap. Never raises; the log is telemetry, not state."""
    target = decisions_log_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > LOG_CAP_BYTES:
            rotated = target.with_name(target.name + ".1")
            try:
                rotated.unlink(missing_ok=True)
                target.replace(rotated)
            except OSError:
                pass
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(record), separators=(",", ":"),
                                ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


# -- the entity-selection seam used by entities.Expander ------------------------

def select_entities(client: Client, candidates: list[dict], segment: dict,
                    log_path: Path | None = None) -> list[dict] | None:
    """Ask Jev which of the named candidates are worth expanding.

    Returns the chosen candidates (at most CHOSEN_LIMIT; [] when NONE wins),
    or None when the answer is unusable or the winner sits in the ambiguous
    band — the caller then falls back to its existing heuristic. Every
    outcome (including failures) is appended to the decision log. Never
    raises and never logs: a silent fail-open by design.
    """
    if not candidates:
        return []
    offered: dict[str, str] = {}
    id_by_name: dict[str, str] = {}
    for entity in candidates[:MAX_CANDIDATES]:
        name = " ".join(str(entity.get("name") or "").split())
        if not name or name in id_by_name:
            continue
        cid = f"c{len(id_by_name)}"
        id_by_name[name] = cid
        why = " ".join(str(entity.get("why") or "").split())[:160]
        offered[cid] = name + (f" — {why}" if why else "")
    if not offered:
        return None
    offered[NONE_ID] = "Nothing mentioned here is worth expanding"
    goal = ("Choose which single entity mentioned in this transcript "
            "segment a viewer would most want explained right now, or NONE.")
    rules = [
        "Answer with exactly one entity id, or NONE.",
        "Prefer entities with real-world substance a viewer might genuinely "
        "not know: places, historical events, works, notable people.",
        "Reject throwaway mentions, set dressing, and anything a viewer "
        "would not care about mid-scene.",
        "NONE is correct when every candidate is trivial or off-topic.",
    ]
    state = {
        "segment_start_s": int(segment.get("start_ms", 0)) // 1000,
        "segment_end_s": int(segment.get("end_ms", 0)) // 1000,
        "transcript": "\n".join(
            (line.get("text") or "") for line in segment.get("lines") or []
        )[:2000],
    }
    question_id = "entity_selection"
    record: dict[str, Any] = {"ts": int(time.time()), "q": question_id,
                              "provider": client.config.provider,
                              "model": client.config.model}
    try:
        decision = client.decide(state=state, question_id=question_id,
                                 criteria=offered, goal=goal, rules=rules)
    except JevError as exc:
        record.update(outcome="error", error=str(exc)[:200])
        log_decision(record, path=log_path)
        return None
    record.update(outcome="ok", choice=decision.choice,
                  p=round(decision.probabilities.get(decision.choice, 0.0), 4),
                  latency_ms=round(decision.latency_ms, 1))
    log_decision(record, path=log_path)
    if decision.choice == NONE_ID:
        return []
    if decision.probabilities.get(decision.choice, 0.0) < CONFIDENT_THRESHOLD:
        return None  # ambiguous band: the existing heuristic decides
    # The strict contract pins `choice` to ONE id (argmax == choice). The
    # offer takes that winner plus the next entity ids by probability, at
    # most CHOSEN_LIMIT — the probability record is the ranking, the
    # validated choice is the "at least one is worth it" signal.
    ranked = sorted((cid for cid in offered if cid != NONE_ID),
                    key=lambda cid: decision.probabilities.get(cid, 0.0),
                    reverse=True)
    chosen_ids = set(ranked[:CHOSEN_LIMIT])
    names_by_id = {cid: name for name, cid in id_by_name.items()}
    chosen_names = {names_by_id[cid] for cid in chosen_ids if cid in names_by_id}
    return [e for e in candidates[:MAX_CANDIDATES]
            if " ".join(str(e.get("name") or "").split()) in chosen_names]


def make_selector(settings, *, env: Optional[Mapping[str, str]] = None,
                  transport: Transport | None = None) -> Callable | None:
    """The ``selector`` for entities.Expander, or None when Jev is off or
    unkeyed. A None return from the selector (transport error, HTTP 500,
    timeout, malformed JSON, contract violation, ambiguous band) means 'use
    the heuristic'; a [] return means 'expand nothing this segment'."""
    config = resolve(settings, env=env)
    if config is None:
        return None
    client = Client(config, transport=transport)

    def selector(candidates: list[dict], segment: dict) -> list[dict] | None:
        return select_entities(client, candidates, segment)

    return selector
