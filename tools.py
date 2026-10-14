"""HTTP client for the Sintex mock tools API, plus number normalisation.

Changes from the original:
- One shared httpx.AsyncClient per call session (the old code opened a new TLS
  connection to ngrok on every tool call, which added latency to every lookup).
- API errors come back to the LLM as {"status": "error", ...} instead of raising,
  so the prompt's error paths (Section 8) actually trigger.
- spoken_to_digits() turns what the caller said ("saat do aath ek", "सात दो आठ",
  "double six", "७२८") into plain digits, so the LLM never has to do it.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# This header bypasses the ngrok free tier HTML warning page
HEADERS = {"ngrok-skip-browser-warning": "true"}


# ---------------------------------------------------------------------------
# Spoken number normalisation
# ---------------------------------------------------------------------------

_DIGIT_WORDS: dict[str, str] = {}


def _add(digit: str, *words: str) -> None:
    for w in words:
        _DIGIT_WORDS[w] = digit


# English (+ common mis-hearings inside a digit sequence)
_add("0", "zero", "oh", "o", "nil", "null")
_add("1", "one", "won")
_add("2", "two", "to", "too")
_add("3", "three", "tree")
_add("4", "four", "for", "fore")
_add("5", "five")
_add("6", "six")
_add("7", "seven")
_add("8", "eight", "ate")
_add("9", "nine")
# Hindi (Roman + Devanagari)
_add("0", "shunya", "shoonya", "sunya", "शून्य", "सुन्य", "ज़ीरो", "जीरो")
_add("1", "ek", "एक")
_add("2", "do", "दो")
_add("3", "teen", "tin", "तीन")
_add("4", "char", "chaar", "चार")
_add("5", "paanch", "panch", "पांच", "पाँच")
_add("6", "chhe", "che", "chhah", "chah", "chheh", "छह", "छः", "छे", "छ")
_add("7", "saat", "sat", "सात")
_add("8", "aath", "ath", "आठ")
_add("9", "nau", "no", "नौ")
# Gujarati (Roman + Gujarati script)
_add("0", "શૂન્ય", "ઝીરો")
_add("1", "એક")
_add("2", "be", "bay", "બે")
_add("3", "tran", "tren", "ત્રણ")
_add("4", "ચાર")
_add("5", "પાંચ")
_add("6", "chha", "છ")
_add("7", "સાત")
_add("8", "આઠ")
_add("9", "nav", "નવ")
# Tamil (Roman + Tamil script)
_add("0", "poojyam", "pujyam", "பூஜ்யம்", "பூஜ்ஜியம்", "ஜீரோ")
_add("1", "onnu", "ondru", "ஒன்று", "ஒண்ணு", "ஒன்னு")
_add("2", "rendu", "irandu", "இரண்டு", "ரெண்டு")
_add("3", "moonu", "munu", "moondru", "மூன்று", "மூணு")
_add("4", "naalu", "nalu", "naangu", "நான்கு", "நாலு")
_add("5", "anju", "ainthu", "aindhu", "ஐந்து", "அஞ்சு")
_add("6", "aaru", "ஆறு")
_add("7", "ezhu", "elu", "ஏழு")
_add("8", "ettu", "எட்டு")
_add("9", "ombodhu", "onbathu", "onbadhu", "ஒன்பது", "ஒம்போது")

# English grouped numbers ("seventy two eighty one" -> 7281)
_TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
          "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}
_TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fourty": "4", "fifty": "5",
         "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9"}

_REPEATERS = {
    "double": 2, "doubal": 2, "dabal": 2, "डबल": 2, "ડબલ": 2, "டபுள்": 2,
    "triple": 3, "tripal": 3, "ट्रिपल": 3, "ટ્રિપલ": 3, "ட்ரிபிள்": 3,
}

# Native-script numerals -> ASCII (Devanagari, Gujarati, Tamil)
_NATIVE_NUMERALS = str.maketrans(
    "०१२३४५६७८९" "૦૧૨૩૪૫૬૭૮૯" "௦௧௨௩௪௫௬௭௮௯",
    "0123456789" * 3,
)


def spoken_to_digits(text: str) -> str:
    """Convert a spoken or typed number into a digit string.

    Handles digits, digit words in English/Hindi/Gujarati/Tamil (Roman or native
    script), "double six" / "triple zero", and native numerals. Unknown words
    (fillers like "umm", "aur") are ignored, so a missing digit shows up as a
    short result rather than a guess.
    """
    text = (text or "").translate(_NATIVE_NUMERALS).lower()
    # Split on spaces/punctuation only: Indic words contain vowel signs that
    # regex "word" classes treat as separators.
    tokens = [t for t in re.split(r"[\s,.;:!?()\[\]\"'/|\-\u2013\u2014\u0964\u0965]+", text) if t]
    out: list[str] = []
    repeat = 1
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok.isascii() and tok.isdigit():
            out.append(tok[0] * repeat + tok[1:])
            repeat = 1
        elif tok in _TEENS:
            out.append(_TEENS[tok])
        elif tok in _TENS:
            nxt = tokens[i] if i < len(tokens) else ""
            unit = _DIGIT_WORDS.get(nxt) if nxt in ("one", "two", "three", "four", "five",
                                                    "six", "seven", "eight", "nine") else None
            if unit:
                out.append(_TENS[tok] + unit)
                i += 1
            else:
                out.append(_TENS[tok] + "0")
        elif tok in _REPEATERS:
            repeat = _REPEATERS[tok]
        elif tok in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok] * repeat)
            repeat = 1
    return "".join(out)


def normalize_phone(raw: str) -> tuple[Optional[str], str]:
    """Returns (ten_digit_number_or_None, digits_heard)."""
    digits = spoken_to_digits(raw)
    if raw and raw.strip().startswith("+91") and len(digits) == 12:
        digits = digits[2:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return (digits if len(digits) == 10 else None), digits


def normalize_invoice(raw: str) -> tuple[Optional[str], str]:
    """Returns (eight_digit_id_or_None, digits_heard)."""
    digits = spoken_to_digits(raw)
    return (digits if len(digits) == 8 else None), digits


# ---------------------------------------------------------------------------
# Speaking numbers
# ---------------------------------------------------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS_WORDS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_to_words(n: int) -> str:
    """6500 -> "six thousand five hundred". Handles 0 to 999,999,999."""
    n = int(n)
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS_WORDS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + (" " + number_to_words(rest) if rest else "")
    for size, name in ((1_000_000, "million"), (1000, "thousand")):
        if n >= size:
            rest = n % size
            return number_to_words(n // size) + " " + name + (" " + number_to_words(rest) if rest else "")
    return str(n)


def digits_spoken(digits: str) -> str:
    """"72810715" -> "seven, two, eight, one, zero, seven, one, five"."""
    return ", ".join(_ONES[int(d)] for d in digits if d.isdigit())


def describe_claim(result: dict) -> dict:
    """Adds plain-English facts to a get_claim_status response so the LLM
    reports the status exactly instead of interpreting raw fields."""
    if result.get("status") != "success":
        return result
    inv = result.get("invoice_id", "")
    out = dict(result)
    out["invoice_id_spoken"] = digits_spoken(inv)
    if not result.get("claim_found"):
        out["facts_for_caller"] = "No claim was found for this invoice."
        return out

    status = result.get("claim_status")
    hours = result.get("expected_update_hours")
    points = result.get("points_credited")
    hours_w = f"{number_to_words(hours)} hours" if hours else None
    points_w = f"{number_to_words(points)} points" if points else None

    if status == "under_review":
        facts = ["The claim is UNDER REVIEW. It is not approved or rejected yet."]
        if hours_w:
            facts.append(f"The caller will get an update within {hours_w}.")
    elif status == "approved":
        facts = ["The claim is APPROVED. It is not under review any more."]
        if points_w:
            facts.append(f"{points_w.capitalize()} will be added to the account"
                         + (f" within {hours_w}." if hours_w else "."))
    elif status == "points_credited":
        facts = ["The claim is COMPLETE."]
        if points_w:
            facts.append(f"{points_w.capitalize()} have already been added to the account.")
    elif status == "rejected":
        facts = ["The claim was REJECTED (not approved)."]
        if result.get("rejection_reason"):
            reason = re.sub(r"\d+", lambda m: number_to_words(int(m.group())), result["rejection_reason"])
            facts.append(f"Reason: {reason}.")
        facts.append("Offer to connect the caller to a Sintex sales representative.")
    else:
        facts = [f"The claim status is {status}."]
    out["facts_for_caller"] = " ".join(facts)
    return out


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class SintexAPI:
    """One instance per call session. Call aclose() when the session ends."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        # Read at runtime (not import time) so values from .env are picked up.
        base_url = base_url or os.getenv("SINTEX_API_BASE_URL") or DEFAULT_BASE_URL
        logger.info("Sintex API base URL: %s", base_url)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=HEADERS,
            timeout=httpx.Timeout(6.0, connect=3.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            r = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            logger.warning("Sintex API %s %s failed: %s", method, path, e)
            return {"status": "error", "error_code": "NETWORK_ERROR"}
        try:
            data = r.json()
        except ValueError:
            logger.warning(
                "Sintex API %s %s returned non-JSON (HTTP %s). Is the backend running at this URL? "
                "If you use ngrok, the URL changes every restart.", method, r.url, r.status_code)
            return {"status": "error", "error_code": f"HTTP_{r.status_code}"}
        if r.status_code >= 400:
            logger.warning("Sintex API %s %s -> HTTP %s: %s", method, path, r.status_code, data)
            if not (isinstance(data, dict) and data.get("status") == "error"):
                data = {"status": "error", "error_code": f"HTTP_{r.status_code}"}
        return data

    async def get_reward_summary(self, phone_number: str) -> dict:
        return await self._request("GET", "/tools/get_reward_summary",
                                   params={"phone_number": phone_number})

    async def get_recent_transactions(self, phone_number: str, limit: int = 3) -> dict:
        return await self._request("GET", "/tools/get_recent_transactions",
                                   params={"phone_number": phone_number, "limit": limit})

    async def get_reward_statement(self, phone_number: str, start_date: Optional[str] = None,
                                   end_date: Optional[str] = None) -> dict:
        params = {"phone_number": phone_number}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return await self._request("GET", "/tools/get_reward_statement", params=params)

    async def get_claim_status(self, invoice_id: str, phone_number: Optional[str] = None) -> dict:
        params = {"invoice_id": invoice_id}
        if phone_number:
            params["phone_number"] = phone_number
        return await self._request("GET", "/tools/get_claim_status", params=params)

    async def send_claim_link(self, phone_number: str, language: str) -> dict:
        return await self._request("POST", "/tools/send_claim_link",
                                   json={"phone_number": phone_number, "language": language})

    async def transfer_to_human(self, phone_number: str, reason: str, caller_language: str,
                                summary: str, invoice_id: Optional[str] = None) -> dict:
        payload = {"phone_number": phone_number, "reason": reason,
                   "caller_language": caller_language, "summary": summary}
        if invoice_id:
            payload["invoice_id"] = invoice_id
        return await self._request("POST", "/tools/transfer_to_human", json=payload)