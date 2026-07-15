"""
Thin wrapper around the Anthropic API. Ported from the reference bot, with one
addition: ask_json() now accepts an optional `validate` callback so a SEMANTIC
contract check (spec §6/§10) runs in the same corrective-retry loop as the
existing syntax-repair — valid-but-wrong-shaped JSON no longer slips through.

The `anthropic` SDK call is synchronous; it is pushed into a thread so the
asyncio event loop is never blocked on a network round trip.
"""
import asyncio
import json
import logging
import time

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_RETRIES, RETRY_BACKOFF_SECONDS

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class ClaudeError(RuntimeError):
    """Raised when Claude could not be reached / parsed / validated after all retries."""


def _call_raw_sync(system: str, user_message: str, max_tokens: int) -> tuple[str, str]:
    """Returns (text, stop_reason). stop_reason == 'max_tokens' means the response was
    cut off for hitting the token budget — a length problem, not a formatting mistake,
    so callers must not treat it like an ordinary malformed-JSON retry (see ask_json)."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = _client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            return text, response.stop_reason
        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            last_exc = exc
            logger.warning("Claude API call failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise ClaudeError(f"Claude API call failed after {MAX_RETRIES} attempts: {last_exc}")


async def ask(system: str, user_message: str, max_tokens: int = 2000) -> str:
    text, _ = await asyncio.to_thread(_call_raw_sync, system, user_message, max_tokens)
    return text


def _extract_json_block(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not (text.startswith("{") or text.startswith("[")):
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if starts:
            text = text[min(starts):]
    return _trim_to_matching_close(text)


def _trim_to_matching_close(text: str) -> str:
    """Find where the JSON value starting at index 0 actually closes, respecting string
    literals and escapes, and drop anything after it (e.g. trailing chat prose).

    A naive `text.rfind('}')` is NOT safe here: exam content routinely contains square
    brackets inside string values (a reference range like "[4-8 L/min]", a citation like
    "[1]") — rfind would find THAT bracket instead of the JSON's real closing brace and
    silently truncate everything after it, corrupting valid JSON. This tracks actual
    bracket depth and ignores brackets while inside a quoted string.
    """
    if not text or text[0] not in "{[":
        return text
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[: i + 1]
    return text  # never closed — let json.loads raise its own informative error


async def ask_json(system: str, user_message: str, max_tokens: int = 2000, validate=None):
    """Ask Claude for JSON and parse it, retrying with a corrective nudge on either a
    JSON *syntax* error or a *semantic* contract violation.

    `validate(obj) -> None | str`: return None if the parsed object satisfies the
    contract, or a short human-readable reason it doesn't. The reason is fed back to
    Claude verbatim so the retry is targeted (spec §6/§10).
    """
    strict_system = (
        system
        + "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown code fences, "
        "no commentary before or after the JSON."
    )
    current_user_message = user_message
    current_max_tokens = max_tokens
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        raw, stop_reason = await asyncio.to_thread(_call_raw_sync, strict_system, current_user_message, current_max_tokens)
        if stop_reason == "max_tokens":
            # A genuine length problem, not a formatting mistake — re-nudging "reply with
            # valid JSON only" would just truncate again at the same budget. Give real
            # headroom on the retry instead of wasting an attempt on the wrong fix.
            last_error = f"response truncated at max_tokens={current_max_tokens}"
            logger.warning(
                "Claude response hit max_tokens (attempt %s/%s, budget=%s) — raising budget for retry",
                attempt, MAX_RETRIES, current_max_tokens,
            )
            current_max_tokens = min(current_max_tokens * 2, 8192)
            continue
        candidate = _extract_json_block(raw)
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON ({exc})"
            logger.warning("Claude JSON parse failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            current_user_message = (
                user_message
                + f"\n\nYour previous reply could not be parsed as JSON ({exc}). "
                "Reply again with ONLY a single valid JSON object and nothing else."
            )
            continue
        if validate is not None:
            problem = validate(obj)
            if problem:
                last_error = f"contract violation ({problem})"
                logger.warning(
                    "Claude JSON contract check failed (attempt %s/%s): %s",
                    attempt, MAX_RETRIES, problem,
                )
                current_user_message = (
                    user_message
                    + f"\n\nYour previous reply was valid JSON but violated the required "
                    f"contract: {problem}. Fix exactly that and reply again with ONLY the "
                    "corrected JSON object."
                )
                continue
        return obj
    raise ClaudeError(f"Claude did not return contract-valid JSON after {MAX_RETRIES} attempts: {last_error}")
