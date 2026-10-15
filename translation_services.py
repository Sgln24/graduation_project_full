"""Dedicated machine translation APIs; credentials stay on the server."""
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SERVICE_MODELS = {
    "libretranslate": ("libretranslate", "LibreTranslate (open source)"),
    "microsoft-translator": ("microsoft", "Microsoft Translator v3 (NMT)"),
}
MICROSOFT_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"


class TranslationServiceError(RuntimeError):
    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_services():
    return {
        "libretranslate": bool(os.getenv("LIBRETRANSLATE_URL", "").strip()),
        "microsoft": bool(os.getenv("MICROSOFT_TRANSLATOR_KEY", "").strip()),
    }


def character_count(text):
    # Count UTF-16 units conservatively for service request limits.
    return len(text.encode("utf-16-le")) // 2


def service_batches(items, provider):
    max_items, max_chars = (100, 10000) if provider == "libretranslate" else (1000, 50000)
    batches, batch, size = [], [], 0
    for item in items:
        count = character_count(item["text"])
        if count > max_chars:
            raise ValueError(f"A caption exceeds {provider}'s {max_chars:,}-character batch limit.")
        if batch and (len(batch) >= max_items or size + count > max_chars):
            batches.append(batch)
            batch, size = [], 0
        batch.append(item)
        size += count
    if batch:
        batches.append(batch)
    return batches


def translate_texts(provider, texts, target):
    if target not in {"en", "de", "ar", "pl"}:
        raise ValueError("Unsupported translation language")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if provider == "libretranslate":
        base = os.getenv("LIBRETRANSLATE_URL", "").strip().rstrip("/")
        parsed_url = urlsplit(base)
        if (
            parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname
            or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment
            or (parsed_url.scheme == "http" and parsed_url.hostname not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError("LIBRETRANSLATE_URL must be HTTPS, or HTTP on localhost, without credentials or query parameters.")
        url = base + "/translate"
        payload = {"q": texts, "source": "auto", "target": target, "format": "text"}
        key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
        if key:
            payload["api_key"] = key
    elif provider == "microsoft":
        key = os.getenv("MICROSOFT_TRANSLATOR_KEY", "").strip()
        if not key:
            raise ValueError("Configure MICROSOFT_TRANSLATOR_KEY on the server.")
        headers["Ocp-Apim-Subscription-Key"] = key
        region = os.getenv("MICROSOFT_TRANSLATOR_REGION", "").strip()
        if region:
            headers["Ocp-Apim-Subscription-Region"] = region
        url = MICROSOFT_ENDPOINT + "?" + urlencode({"api-version": "3.0", "to": target, "textType": "plain"})
        payload = [{"Text": text} for text in texts]
    else:
        raise ValueError("Unsupported translation service")
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers=headers, method="POST")
    try:
        with build_opener(NoRedirects()).open(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as error:
        status = error.code
        error.close()
        guidance = (
            " Check the server API key and resource region." if status in {401, 403}
            else " Check installed languages and service input limits." if status == 400 else ""
        )
        raise TranslationServiceError(
            f"{provider} translation returned HTTP {status}.{guidance}",
            transient=status == 429 or status >= 500,
        ) from None
    except (URLError, TimeoutError, socket.timeout):
        raise TranslationServiceError(f"{provider} translation could not connect or timed out after 30s.",
                                      transient=True) from None
    except (ValueError, UnicodeError):
        raise TranslationServiceError(f"{provider} translation returned invalid JSON.") from None
    if provider == "libretranslate":
        translated = data.get("translatedText") if isinstance(data, dict) else None
        if isinstance(translated, str) and len(texts) == 1:
            translated = [translated]
    else:
        translated = []
        if not isinstance(data, list) or len(data) != len(texts):
            raise TranslationServiceError("Microsoft Translator returned an incomplete response.")
        for item in data:
            values = item.get("translations") if isinstance(item, dict) else None
            if (
                not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict)
                or values[0].get("to") != target
            ):
                raise TranslationServiceError("Microsoft Translator returned invalid translation entries.")
            translated.append(values[0].get("text"))
    if (
        not isinstance(translated, list) or len(translated) != len(texts)
        or any(not isinstance(text, str) or not text.strip() for text in translated)
    ):
        raise TranslationServiceError(f"{provider} translation returned incomplete or empty text.")
    return translated
