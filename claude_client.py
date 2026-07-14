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


def _call_raw_sync(system: str, user_message: str, max_tokens: int) -> str:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = _client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            return "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            last_exc = exc
            logger.warning("Claude API call failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise ClaudeError(f"Claude API call failed after {MAX_RETRIES} attempts: {last_exc}")


async def ask(system: str, user_message: str, max_tokens: int = 2000) -> str:
    return await asyncio.to_thread(_call_raw_sync, system, user_message, max_tokens)


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
    # trim any trailing prose after the final closing brace/bracket
    for closer in ("}", "]"):
        idx = text.rfind(closer)
        if idx != -1:
            text = text[: idx + 1]
            break
    return text


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
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        raw = await ask(strict_system, current_user_message, max_tokens)
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
