import os
import json
import re
import asyncio
import time
from contextvars import ContextVar
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types
from dotenv import load_dotenv
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from project_paths import SUMMARIES_DIR

# Gemini's free-tier quotas are counted per model, so another model often
# still has quota when the selected one is exhausted.
GEMINI_FALLBACK_MODELS = (
    "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
)
# Gemini retires older models for new API keys with 404 NOT_FOUND; skip those.
MODEL_UNAVAILABLE_MARKERS = (
    "404", "not_found", "not found", "no longer available", "model_not_found",
    "does not exist", "decommissioned", "deprecated",
)
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
PROVIDER_LABELS = {"gemini": "Gemini", "groq": "Groq", "openai": "OpenAI"}
# Concurrent translation requests for Gemini/OpenAI; Groq stays at one.
# A model that ran out of quota is skipped by automatic selection for a while.
QUOTA_COOLDOWN_SECONDS = max(60, int(os.getenv("AI_QUOTA_COOLDOWN_SECONDS", "3600")))
CHAPTER_TIMEOUT_SECONDS = 90
# About 4,000 tokens: one part plus its answer fits Groq's free 8,000 tokens per minute.
GROQ_CHAPTER_CHUNK_CHARS = 16000
# Other providers accept long prompts, but very long single requests time out.
CHAPTER_CHUNK_CHARS = 40000
CHAPTER_INSTRUCTIONS_LIMIT = 1000
TRANSCRIPT_LANGUAGES = {"de", "en", "ar", "pl"}
# Openings that describe the lesson instead of stating its content.
FILLER_OPENING = re.compile(
    r"^\s*(?:(?:in|throughout)\s+this\s+(?:part|chapter|section|segment|lecture|video|session|lesson)\s*,?\s*)?"
    r"(?:(?:the\s+)?(?:learners?|students?|participants?|viewers?|audience|you|we)\s+"
    r"(?:will\s+|can\s+|are\s+going\s+to\s+|should\s+|get\s+to\s+)?(?:also\s+|then\s+|first\s+)?"
    r"(?:be\s+introduced\s+to|be\s+shown|learns?(?:\s+about)?|understands?|discovers?|explores?|"
    r"gets?\s+to\s+know|sees?|finds?\s+out(?:\s+about)?|are\s+introduced\s+to|is\s+introduced\s+to)"
    r"|(?:the\s+|this\s+)?(?:lecture|lecturer|speaker|instructor|video|chapter|section|part|segment|lesson)\s+"
    r"(?:also\s+)?(?:explains|points\s+out|covers|discusses|describes|introduces|presents|shows|outlines|highlights|"
    r"focuses\s+on|talks\s+about|deals\s+with|is\s+about|goes\s+over|reviews))\s*(?:that\s+)?",
    re.IGNORECASE,
)
CHAPTER_DETAIL_LEVELS = {
    "brief": {
        "chapter_guidance": "Group related learning objectives under broad, clearly distinct subjects.",
        "section_guidance": "Only broad, clearly different subtopics; merge related explanations",
    },
    "balanced": {
        "chapter_guidance": "One chapter per distinct subject or learning objective. Put related subtopics inside it.",
        "section_guidance": "One section per genuinely separate topic; merge examples, steps and continuations of the same topic",
    },
    "detailed": {
        "chapter_guidance": "Distinguish independently meaningful learning objectives, not individual steps or examples.",
        "section_guidance": "Separate meaningful subtopics, but not every sentence, example or transcript fragment",
    },
}
TRANSLATION_CONCURRENCY = max(1, int(os.getenv("TRANSLATION_CONCURRENCY", "4")))
TRANSLATION_MODELS = {
    "", *GEMINI_FALLBACK_MODELS, "gemini-2.5-flash", "gemini-2.5-flash-lite",
    "groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b",
    "groq:llama-3.3-70b-versatile", "gpt-4.1-mini",
}
_COMPLETION_USAGE = ContextVar("completion_usage", default=None)


def translation_usage(requests):
    """Account for reported usage only; never present missing usage as zero."""
    reported = [request for request in requests if request.get("usage") is not None]
    models = sorted({f"{request['provider']} {request['model']}" for request in requests})
    return {
        "requests": len(requests), "reported_requests": len(reported),
        "complete": bool(requests) and len(reported) == len(requests),
        "input_tokens": sum(request["usage"]["input_tokens"] for request in reported),
        "output_tokens": sum(request["usage"]["output_tokens"] for request in reported),
        "total_tokens": sum(request["usage"]["total_tokens"] for request in reported),
        "models": models,
    }

class SummaryProcessor:
    def __init__(self):
        # Initialize Gemini API
        load_dotenv()

        # Prefer the environment variable for normal/local execution.
        GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

        # Colab Secrets are available only from the interactive notebook
        # process. The server process receives the key through the environment.
        # Do not call google.colab.userdata from the FastAPI/Uvicorn process.

        self.client = None
        self.model = None
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.unavailable_models = set()
        self.quota_exhausted_until = {}
        self.openai_client = None
        self.openai_model_name = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.groq_client = None
        self.groq_model_name = os.getenv("GROQ_LLM_MODEL", "openai/gpt-oss-120b")
        self._refresh_openai_client()
        self._refresh_groq_client()

        try:
            if not GOOGLE_API_KEY:
                raise ValueError("GOOGLE_API_KEY is not configured")

            self.client = genai.Client(api_key=GOOGLE_API_KEY)
            self.model = True
        except Exception as e:
            print(f"Warning: Gemini API initialization failed: {str(e)}")
            self.model = None

    def _refresh_gemini_client(self):
        """Load a Gemini client if a key was configured after startup."""
        api_key = os.getenv("GOOGLE_API_KEY")
        if api_key and self.client is None:
            try:
                self.client = genai.Client(api_key=api_key)
                self.model = True
            except Exception as error:
                print(f"Warning: Gemini API initialization failed: {error}")

    def _refresh_openai_client(self):
        """Load an OpenAI client if a key was configured after startup."""
        api_key = os.getenv("OPENAI_API_KEY")
        if OpenAI and api_key and self.openai_client is None:
            try:
                self.openai_client = OpenAI(api_key=api_key)
            except Exception as error:
                print(f"Warning: OpenAI API initialization failed: {error}")

    def _refresh_groq_client(self):
        """Load a Groq client (OpenAI-compatible) if GROQ_API_KEY is configured."""
        api_key = os.getenv("GROQ_API_KEY")
        if OpenAI and api_key and getattr(self, "groq_client", None) is None:
            try:
                self.groq_client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
            except Exception as error:
                print(f"Warning: Groq API initialization failed: {error}")

    def _refresh_clients(self):
        for refresh in (
            getattr(self, "_refresh_gemini_client", None),
            getattr(self, "_refresh_openai_client", None),
            getattr(self, "_refresh_groq_client", None),
        ):
            if refresh:
                refresh()

    @staticmethod
    def _provider_chain_entry(requested_model):
        if not requested_model:
            return None
        if requested_model.startswith("gpt-"):
            return ("openai", requested_model)
        if requested_model.startswith("groq:"):
            return ("groq", requested_model[len("groq:"):])
        return ("gemini", requested_model)

    def _provider_chain(self, requested_model=None, prefer_groq=False):
        """Return (provider, model) pairs to try, in order, skipping unconfigured ones."""
        configured = {
            "gemini": bool(self.model),
            "groq": bool(getattr(self, "groq_client", None)),
            "openai": bool(getattr(self, "openai_client", None)),
        }
        chain = []
        unavailable = getattr(self, "unavailable_models", set())
        now = time.time()
        exhausted = {
            pair for pair, until in getattr(self, "quota_exhausted_until", {}).items()
            if until > now
        }
        cooling = []

        def add(provider, model):
            if (
                model and configured[provider] and (provider, model) not in chain
                and (provider, model) not in unavailable
            ):
                if (provider, model) in exhausted:
                    cooling.append((provider, model))
                else:
                    chain.append((provider, model))

        if requested_model:
            requested = self._provider_chain_entry(requested_model)
            if requested not in unavailable and configured.get(requested[0]) and requested[1]:
                # An explicitly chosen model is always tried first.
                chain.append(requested)
        if prefer_groq and not requested_model:
            add("groq", getattr(self, "groq_model_name", None))
        add("gemini", self.model_name)
        for model in GEMINI_FALLBACK_MODELS:
            add("gemini", model)
        add("groq", getattr(self, "groq_model_name", None))
        add("openai", getattr(self, "openai_model_name", None))
        # Out-of-quota models stay as a last resort in case their quota has reset.
        return chain + [pair for pair in cooling if pair not in chain]

    def _mark_quota_exhausted(self, provider, model):
        if not hasattr(self, "quota_exhausted_until"):
            self.quota_exhausted_until = {}
        self.quota_exhausted_until[(provider, model)] = time.time() + QUOTA_COOLDOWN_SECONDS
        print(
            f"{PROVIDER_LABELS[provider]} {model} is out of quota; automatic selection "
            f"skips it for {QUOTA_COOLDOWN_SECONDS // 60} minutes"
        )

    @staticmethod
    def _fast_thinking_config(model):
        """Minimal reasoning for simple tasks such as translation, if the SDK supports it."""
        thinking_config = getattr(types, "ThinkingConfig", None)
        if thinking_config is None:
            return None
        fields = getattr(thinking_config, "model_fields", {})
        try:
            if model.startswith("gemini-2.5") and "thinking_budget" in fields:
                return thinking_config(thinking_budget=0)
            if "thinking_level" in fields:
                return thinking_config(thinking_level="minimal")
            if "thinking_budget" in fields:
                return thinking_config(thinking_budget=0)
        except Exception:
            return None
        return None

    def _complete(
        self, provider, model, prompt, temperature, json_output=True, max_tokens=None, fast=False,
    ):
        """Run one prompt against one provider and return the response text.

        fast=True turns model reasoning down; it is used for translation, which
        does not benefit from long thinking but pays for it in latency.
        """
        if provider == "gemini":
            config = {"temperature": temperature}
            if json_output:
                config["response_mime_type"] = "application/json"
            if max_tokens:
                config["max_output_tokens"] = max_tokens
            no_thinking_control = getattr(self, "_no_thinking_control", set())
            thinking = self._fast_thinking_config(model) if fast and model not in no_thinking_control else None
            if thinking is not None:
                try:
                    response = self.client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(**config, thinking_config=thinking),
                    )
                    self._record_completion_usage(provider, response)
                    return response.text
                except Exception as error:
                    if "thinking" not in str(error).lower():
                        raise
                    # This model rejects the reasoning setting; use its default from now on.
                    self._no_thinking_control = no_thinking_control | {model}
            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config),
            )
            self._record_completion_usage(provider, response)
            return response.text
        client = self.groq_client if provider == "groq" else self.openai_client
        options = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if json_output:
            options["response_format"] = {"type": "json_object"}
        if max_tokens:
            options["max_completion_tokens"] = max_tokens
        if fast and provider == "groq" and "gpt-oss" in model:
            options["extra_body"] = {"reasoning_effort": "low"}
        response = client.chat.completions.create(**options)
        self._record_completion_usage(provider, response)
        return response.choices[0].message.content

    @staticmethod
    def _record_completion_usage(provider, response):
        capture = _COMPLETION_USAGE.get()
        if capture is None:
            return
        usage = getattr(response, "usage_metadata" if provider == "gemini" else "usage", None)
        if usage is None:
            return
        input_tokens = getattr(usage, "prompt_token_count" if provider == "gemini" else "prompt_tokens", None)
        output_tokens = getattr(usage, "candidates_token_count" if provider == "gemini" else "completion_tokens", None)
        total_tokens = getattr(usage, "total_token_count" if provider == "gemini" else "total_tokens", None)
        if not all(isinstance(value, int) and value >= 0 for value in (input_tokens, output_tokens, total_tokens)):
            return
        if provider == "gemini":
            output_tokens += getattr(usage, "thoughts_token_count", 0) or 0
        capture["usage"] = {
            "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
        }

    def translation_models(self):
        configured = {
            "gemini": bool(self.model), "groq": bool(getattr(self, "groq_client", None)),
            "openai": bool(getattr(self, "openai_client", None)),
        }
        return [
            {"id": name, "label": name, "available": configured[self._provider_chain_entry(name)[0]]
             and self._provider_chain_entry(name) not in getattr(self, "unavailable_models", set())}
            for name in sorted(TRANSLATION_MODELS) if name and not name.startswith("gemini-2.5")
        ]

    @staticmethod
    def _provider_label(provider, index):
        label = PROVIDER_LABELS[provider]
        return label if index == 0 else f"{label} fallback"

    @staticmethod
    def _no_provider_message():
        return (
            "No AI model is configured. Set GOOGLE_API_KEY (free), GROQ_API_KEY (free) "
            "or OPENAI_API_KEY in the Colab runtime and restart the server."
        )
    @staticmethod
    def _summary_language_instruction(language):
        languages = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "ar": "Arabic", "pl": "Polish"}
        if language in (None, "", "transcript"):
            return (
                "Write all chapter titles and summary text in the same language as "
                "the supplied transcript. Infer its predominant language from its text; "
                "do not translate it into English."
            )
        if language not in languages:
            raise ValueError(f"Unsupported summary language: {language}")
        return (
            f"Write all chapter titles, summaries, section titles and points in {languages[language]}, "
            "the language of the transcript."
        )

    @staticmethod
    def _detect_transcript_language(transcript):
        """Return "ar" for transcripts written mostly in Arabic script, else None."""
        text = " ".join(str(item.get("text", "")) for item in transcript[:300] if isinstance(item, dict))
        letters = [character for character in text if character.isalpha()]
        arabic = sum(1 for character in letters if "\u0600" <= character <= "\u08ff")
        return "ar" if letters and arabic / len(letters) > 0.3 else None

    @staticmethod
    def _filler_free(text):
        """Drop openings such as 'The learner will learn' or 'This section covers'."""
        text = " ".join(str(text or "").split())
        stripped = FILLER_OPENING.sub("", text, count=1)
        if stripped == text or len(stripped.split()) < 2:
            return text
        return stripped[0].upper() + stripped[1:]

    async def generate_summary(
        self, transcript: list, video_id: str = None, requested_model: str = None,
        language: str = "transcript", structure: str = "sections", detail: str = "balanced",
        instructions: str = "", transcript_language: str = "",
    ):
        """Generate chapters (optionally with sections), preferring configured Groq.

        transcript_language is set when a translated transcript is open; the
        chapters are then written in that language and saved with the translation.
        """
        try:
            transcript_language = str(transcript_language or "").strip()
            if transcript_language and transcript_language not in TRANSCRIPT_LANGUAGES:
                raise ValueError(f"Unsupported transcript language: {transcript_language}")
            if language in (None, "", "transcript"):
                language = transcript_language or self._detect_transcript_language(transcript or []) or "transcript"
            language_instruction = self._summary_language_instruction(language)
            settings = self._chapter_settings(structure, detail, instructions)
            self._refresh_clients()
            chain = self._provider_chain(requested_model, prefer_groq=True)
            if not transcript or not chain:
                return JSONResponse({
                    "success": False,
                    "error": "No transcript is available." if not transcript
                    else self._no_provider_message(),
                })

            last = transcript[-1]
            transcript_end = float(last.get("start", 0)) + float(last.get("duration", 0) or 0)
            prompt = self._chapter_prompt(
                [f"{self._stamp(item['start'])}: {item['text']}\n" for item in transcript],
                float(transcript[0].get("start", 0)), transcript_end, 1, 1, None,
                language_instruction, settings,
            )

            # dump prompt into a debug file
            with open('debug.txt', 'w', encoding='utf-8') as f:
                f.write(prompt)
            response_text = None
            failures = []
            provider = None
            used_model = None
            for index, (candidate, model) in enumerate(chain):
                try:
                    # Long lectures are split into parts: Groq's free tier rejects
                    # long prompts (tokens per minute) and huge requests time out.
                    response_text = await self._chunked_chapters(
                        candidate, model, transcript, language_instruction, settings,
                    )
                    provider = self._provider_label(candidate, index)
                    used_model = model
                    break
                except Exception as error:
                    if isinstance(error, asyncio.TimeoutError):
                        error = RuntimeError(
                            f"no answer within {CHAPTER_TIMEOUT_SECONDS} seconds (the model is busy or slow)"
                        )
                    failures.append(f"{PROVIDER_LABELS[candidate]} {model}: {error}")
                    if isinstance(error, json.JSONDecodeError):
                        continue
                    if "timed out" in str(error).lower() or "no answer within" in str(error):
                        continue
                    if self._is_model_unavailable(error):
                        self._mark_unavailable(candidate, model)
                        continue
                    if "resource_exhausted" in str(error).lower():
                        self._mark_quota_exhausted(candidate, model)
                    if not self._is_quota_error(error):
                        return JSONResponse({
                            "success": False,
                            "error": f"{PROVIDER_LABELS[candidate]} {model} could not generate chapters: {error}",
                        })
            if response_text is None:
                return JSONResponse({
                    "success": False,
                    "error": "Every configured AI model failed: out of quota, busy, unavailable or invalid chapter JSON. "
                             "Retry after the provider's stated wait time, choose another available model, "
                             "or check quota/billing for your configured providers. "
                             "Details: " + " | ".join(failures),
                })
            chapters = self._parse_chapter_list(response_text)
            chapters = self._normalize_chapters(
                chapters, transcript,
                end=transcript_end, sectioned=settings["sections"],
            )
            self._save_chapters(video_id, chapters, transcript_language, transcript)

            return JSONResponse({
                "success": True,
                "chapters": chapters,
                "language": transcript_language or None,
                "provider": provider,
                "model": used_model,
                "notice": (
                    f"The first model was unavailable, out of quota or busy; {provider.replace(' fallback', '')} "
                    f"{used_model} generated these chapters."
                ) if provider.endswith("fallback") else None
            })

        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": str(e)
            })

    async def suggest_title(self, chapters, video_id="", source_title=""):
        """Return {"title", "filename"} summarising the video in the chapters' language, or None."""
        lines = []
        for index, chapter in enumerate(chapters or []):
            if not isinstance(chapter, dict) or not str(chapter.get("title") or "").strip():
                continue
            summary = " ".join(str(chapter.get("summary") or "").split())[:220]
            lines.append(f"{index + 1}. {str(chapter['title']).strip()}" + (f" - {summary}" if summary else ""))
        if not lines:
            return None
        outline = "\n".join(lines)[:7000]
        import hashlib
        key = hashlib.sha1(f"{source_title}\n{outline}".encode("utf-8")).hexdigest()[:16]
        cache_path = SUMMARIES_DIR / f"{video_id}_titles.json" if video_id else None
        cache = {}
        if cache_path and cache_path.is_file():
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
            if isinstance(cache, dict) and isinstance(cache.get(key), dict):
                return cache[key]
        prompt = (
            "Write a short title for this lecture video, suitable as the title of a course outline. "
            "It must capture the topic of the whole video, not one chapter. "
            "Use 3 to 8 words, in the same language as the chapter titles below. "
            "No quotes, no trailing punctuation, and no words such as 'Lecture', 'Video', "
            "'Recording', 'Overview' or 'Introduction to' unless essential.\n"
            "Also give a file name: the title in ASCII letters and digits only "
            "(transliterate non-Latin script, replace umlauts such as ae/oe/ue), words joined by underscores, "
            "at most 60 characters.\n"
            'Return JSON: {"title": "...", "filename": "..."}\n\n'
            f"Original file name (may be uninformative): {source_title}\n"
            f"Chapters:\n{outline}"
        )
        for provider, model in self._provider_chain(prefer_groq=True):
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(self._complete, provider, model, prompt, 0.3, True, 800, True),
                    timeout=30,
                )
                data = json.loads(re.sub(r"^```(?:json)?|```$", "", (text or "").strip()).strip())
                title = " ".join(str(data.get("title") or "").split()).strip(" .:;\"'")
                filename = re.sub(r"[^A-Za-z0-9_-]+", "_", str(data.get("filename") or "")).strip("_-")[:60]
                if not title:
                    continue
                result = {"title": title[:120], "filename": filename}
                if cache_path:
                    cache = cache if isinstance(cache, dict) else {}
                    cache[key] = result
                    try:
                        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
                return result
            except Exception as error:
                if self._is_quota_error(error):
                    self._mark_quota_exhausted(provider, model)
                print(f"Title suggestion with {provider} {model} failed: {error}")
        return None

    @staticmethod
    def _parse_chapter_list(response_text):
        return SummaryProcessor._parse_chapter_response(response_text)[0]

    @staticmethod
    def _chapter_settings(structure="sections", detail="balanced", instructions=""):
        if structure not in {"sections", "flat"}:
            raise ValueError(f"Unsupported chapter structure: {structure}")
        if detail not in CHAPTER_DETAIL_LEVELS:
            raise ValueError(f"Unsupported chapter detail level: {detail}")
        return {
            **CHAPTER_DETAIL_LEVELS[detail],
            "sections": structure == "sections",
            "instructions": " ".join(str(instructions or "").split())[:CHAPTER_INSTRUCTIONS_LIMIT],
        }

    @staticmethod
    def _stamp(seconds):
        seconds = int(float(seconds))
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    @classmethod
    def _chapter_prompt(cls, lines, start, end, number, total, previous_title, language_instruction, settings,
                        previous_context=None):
        scope = (
            f"This is part {number} of {total} of one lecture transcript, from {cls._stamp(start)} to {cls._stamp(end)}."
            if total > 1 else
            f"This is a lecture transcript from {cls._stamp(start)} to {cls._stamp(end)}."
        )
        title_rule = (
            "a concise title of at most 5 words: a specific noun phrase naming the topic, not a sentence, "
            "with no trailing punctuation and no filler such as \"Introduction to\" or \"Discussion of\""
        )
        guidance = [
            scope,
            language_instruction,
            "Organize the lecture by actual subjects and learning objectives, not its duration. "
            f"{settings['chapter_guidance']} "
            "Do not target a chapter count, duration, equal-sized chapters or a section limit. "
            "A brief but distinct substantive topic can be its own chapter; a long explanation of one topic stays together. "
            "Start a new chapter when the subject or learning objective genuinely changes, even if the previous "
            "chapter already has many sections. Keep examples, demonstrations, troubleshooting and continuations "
            "with the subject they explain. Do not put unrelated subjects under a vague umbrella title. "
            "Never make a chapter out of a brief remark such as a greeting or an organisational note: "
            + ("make it a section of the neighbouring chapter instead." if settings["sections"]
               else "include it in the neighbouring chapter instead."),
        ]
        if settings["sections"]:
            guidance.append(
                f"Each chapter has: timestamp (HH:MM:SS where it starts, taken from the transcript); title ({title_rule}); "
                "summary (one short sentence that states the content itself); "
                "sections (in time order, the first starting with the chapter). "
                f"{settings['section_guidance']}. Choose the number from actual topic changes, not a fixed quota. "
                f"Each section has timestamp (HH:MM:SS from the transcript), title ({title_rule}) and point "
                "(one concise complete sentence with the key takeaway, grounded only in the transcript). "
                "State content directly and concisely: write 'Gradient descent minimizes the loss.' and never "
                "'The lecture explains that...', 'The speaker points out that...', 'The learner will learn...', "
                "'You will be introduced to...' or 'This section covers...' (nor such phrases in any other "
                "language). Do not invent facts."
            )
            example = {"chapters": [{
                "timestamp": "00:00:00", "title": "Course overview",
                "summary": "Course goals, schedule and grading rules.",
                "sections": [
                    {"timestamp": "00:00:00", "title": "Learning goals",
                     "point": "The course builds practical data analysis skills."},
                    {"timestamp": "00:03:10", "title": "Grading scheme",
                     "point": "The final exam counts for 60 percent of the grade."},
                ],
            }]}
        else:
            guidance.append(
                f"For each chapter provide the timestamp where it starts (HH:MM:SS, taken from the transcript) and {title_rule}."
            )
            example = {"chapters": [{"timestamp": "00:00:00", "title": "Course overview"}]}
        if previous_title:
            if previous_context:
                guidance.append("Current chapter context: " + json.dumps(previous_context, ensure_ascii=False))
            guidance.append(
                "Transcript part boundaries are technical request boundaries, not topic boundaries. "
                "Do not start a chapter or section merely because a new part begins."
            )
            if settings["sections"]:
                example["continued_sections"] = []
                guidance.append(
                    f"The previous part ended inside the chapter \"{previous_title}\". If this part begins by "
                    "continuing that topic, return those sections in continued_sections instead of starting "
                    "a new chapter at the very beginning. Keep only major topic changes; do not turn every "
                    "transcript fragment into a section. Keep distinct topics, but group continued explanations "
                    "of the same topic together."
                )
            else:
                guidance.append(
                    f"The previous part ended inside the chapter \"{previous_title}\". If this part begins by "
                    "continuing that topic, start the first chapter only where a new topic begins."
                )
        guidance.append("Return JSON exactly like this example: " + json.dumps(example, ensure_ascii=False))
        if settings["instructions"]:
            guidance.append(
                "Additional instructions from the user (follow them while keeping the JSON format above): "
                + settings["instructions"]
            )
        return (
            "\n".join(guidance) + "\nTranscript:\n" + "".join(lines)
            + "\nReminder: " + language_instruction
        )

    @staticmethod
    def _parse_chapter_response(response_text):
        """Return (chapters, continued_sections) from one chapter answer."""
        text = (response_text or "").strip()
        fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        data = json.loads(text)
        continued = []
        chapters = data
        if isinstance(data, dict):
            if isinstance(data.get("continued_sections"), list):
                continued = [item for item in data["continued_sections"] if isinstance(item, dict)]
            chapters = data["chapters"] if isinstance(data.get("chapters"), list) else next(
                (value for key, value in data.items() if isinstance(value, list) and key != "continued_sections"), []
            )
        if not isinstance(chapters, list):
            raise json.JSONDecodeError("Expected a chapter list or object", text, 0)
        if chapters and isinstance(chapters[0], list):
            chapters = [item for sublist in chapters for item in sublist]
        return [item for item in chapters if isinstance(item, dict)], continued

    async def _chunked_chapters(self, provider, model, transcript, language_instruction, settings=None):
        """Generate chapters part by part and return them as one JSON list."""
        settings = settings or self._chapter_settings()
        chunk_chars = (
            int(GROQ_CHAPTER_CHUNK_CHARS * (0.75 if settings["sections"] else 1))
            if provider == "groq" else CHAPTER_CHUNK_CHARS
        )
        max_tokens = (3000 if settings["sections"] else 1500) if provider == "groq" else None

        parts, current, size = [], [], 0
        for item in transcript:
            line = f"{self._stamp(item['start'])}: {item['text']}\n"
            if current and size + len(line) > chunk_chars:
                parts.append(current)
                current, size = [], 0
            current.append((item, line))
            size += len(line)
        if current:
            parts.append(current)

        def part_end(items):
            last = items[-1][0]
            return float(last.get("start", 0)) + float(last.get("duration", 0) or 0)

        chapters = []
        number = 0
        while parts:
            items = parts.pop(0)
            number += 1
            total = number + len(parts)
            prompt = self._chapter_prompt(
                [line for _, line in items], float(items[0][0].get("start", 0)), part_end(items),
                number, total, chapters[-1]["title"] if chapters and chapters[-1].get("title") else None,
                language_instruction, settings,
                previous_context={
                    "title": chapters[-1].get("title"),
                    "summary": chapters[-1].get("summary"),
                    "recent_sections": [
                        section.get("title") for section in (chapters[-1].get("sections") or [])[-4:]
                        if isinstance(section, dict)
                    ],
                } if chapters else None,
            )
            text = None
            invalid_json = 0
            for attempt in range(6):
                try:
                    # Low reasoning keeps gpt-oss from spending the token budget
                    # on thinking and returning an empty or cut-off JSON answer.
                    text = await asyncio.wait_for(
                        asyncio.to_thread(self._complete, provider, model, prompt, 0.4, True, max_tokens, True),
                        timeout=CHAPTER_TIMEOUT_SECONDS,
                    )
                    part_chapters, continued = self._parse_chapter_response(text)
                    break
                except Exception as error:
                    text = None
                    message = str(error).lower()
                    if (
                        isinstance(error, json.JSONDecodeError)
                        or "json_validate_failed" in message or "failed to generate json" in message
                    ):
                        print(f"{PROVIDER_LABELS[provider]} {model} returned invalid chapter JSON "
                              f"(part {number}/{total}, attempt {attempt + 1}): {error}")
                        invalid_json += 1
                        if invalid_json == 1 and attempt < 5:
                            prompt += (
                                "\nThe previous answer was invalid JSON. Return exactly ONE JSON object "
                                "with chapters and optional continued_sections arrays. No extra objects, "
                                "commentary or markdown; escape quotes inside text."
                            )
                            continue
                        if len(items) > 1:
                            middle = len(items) // 2
                            parts[:0] = [items[:middle], items[middle:]]
                            number -= 1
                            break
                        raise
                    if "request too large" in message and len(items) > 1:
                        # Dense scripts use more tokens per character; split this part again.
                        middle = len(items) // 2
                        parts[:0] = [items[:middle], items[middle:]]
                        number -= 1
                        break
                    wait = re.search(r"try again in\s+(?:(\d+)m)?[\d.]+(?:ms|s)", message)
                    # Only wait for short per-minute limits; daily limits fall through to the next model.
                    if (
                        attempt == 5 or not self._is_quota_error(error) or "request too large" in message
                        or not wait or int(wait.group(1) or 0) > 1
                    ):
                        raise
                    delay = self._retry_delay(error)
                    print(f"{PROVIDER_LABELS[provider]} rate limit while generating chapters "
                          f"(part {number}/{total}); waiting {delay:.0f}s")
                    await asyncio.sleep(delay)
            if text is not None:
                if continued and chapters:
                    chapters[-1].setdefault("sections", [])
                    if isinstance(chapters[-1]["sections"], list):
                        chapters[-1]["sections"].extend(continued)
                chapters.extend(part_chapters)
        return json.dumps(chapters, ensure_ascii=False)

    async def translate_transcript(
        self, transcript, target_language, requested_model=None, _chain=None, _chain_offset=0,
        on_progress=None, _done=None, _started_at=None, allow_fallback=True, _requests=None,
    ):
        """Translate transcript segments in bounded concurrent batches.

        Batches that already succeeded are kept when falling back to another model.
        """
        if _chain is None:
            if not isinstance(allow_fallback, bool):
                raise ValueError("Allow fallback must be true or false")
            if not allow_fallback and not requested_model:
                raise ValueError("Choose a model when automatic fallback is disabled")
            self._refresh_clients()
            _chain = self._provider_chain(requested_model, prefer_groq=True)
            requested_pair = self._provider_chain_entry(requested_model)
            if (
                requested_model
                and (not allow_fallback or requested_pair not in getattr(self, "unavailable_models", set()))
                and (not _chain or _chain[0] != requested_pair)
            ):
                raise RuntimeError(
                    f"Model {requested_model} is unavailable or its API key is not configured."
                )
            if not allow_fallback:
                _chain = _chain[:1]
        if not _chain:
            raise RuntimeError(self._no_provider_message())
        provider, selected_model = _chain[0]
        done = {} if _done is None else _done
        started_at = _started_at or time.monotonic()
        requests = [] if _requests is None else _requests

        translation_items = [
            {
                "id": f"segment-{index}",
                "position": index,
                "text": str(item.get("text", "")),
                "seconds": round(float(item.get("duration", 0) or 0), 1),
            }
            for index, item in enumerate(transcript)
            if isinstance(item, dict)
        ]
        if len(translation_items) != len(transcript):
            raise ValueError("Every transcript segment must be an object")
        remaining_items = [item for item in translation_items if item["id"] not in done]

        batches = (
            self._translation_batches(remaining_items, 30, 2500)
            if provider == "groq"
            else self._translation_batches(remaining_items)
        )
        # Groq's free tier counts the requested output tokens against a small
        # per-minute budget, so send smaller batches one at a time.
        semaphore = asyncio.Semaphore(1 if provider == "groq" else TRANSLATION_CONCURRENCY)
        max_output_tokens = 3000 if provider == "groq" else 8192
        attempts = 4 if provider == "groq" else 2
        context_lines = 4
        total_segments = len(translation_items)
        model_label = f"{PROVIDER_LABELS[provider]} {selected_model}"
        abort = {"error": None}
        finished_batches = {"count": 0}

        def report(message):
            if not on_progress:
                return
            try:
                on_progress({
                    "completed_segments": len(done),
                    "total_segments": total_segments,
                    "completed_batches": finished_batches["count"],
                    "total_batches": len(batches),
                    "provider": PROVIDER_LABELS[provider],
                    "model": selected_model,
                    "elapsed_seconds": round(time.monotonic() - started_at, 1),
                    "message": message,
                    "usage": translation_usage(requests),
                })
            except Exception as error:
                print(f"Translation progress callback failed: {error}")

        report(
            f"Translating {len(remaining_items)} lines in {len(batches)} batches with {model_label}"
            + (f" ({len(done)} lines already done)" if done else "")
        )

        async def translate_batch(batch, batch_number):
            async with semaphore:
                if abort["error"] is not None:
                    return
                start = batch[0]["position"]
                end = batch[-1]["position"] + 1
                context_before = [
                    item["text"]
                    for item in translation_items[max(0, start - context_lines):start]
                ]
                context_after = [
                    item["text"]
                    for item in translation_items[end:end + context_lines]
                ]

                async def request_translation(
                    requested_items, request_label, retry_invalid_response=True
                ):
                    payload = {
                        "context_before": context_before,
                        "lines": [
                            {
                                "id": item["id"],
                                "text": item["text"],
                                "seconds": item["seconds"],
                            }
                            for item in requested_items
                        ],
                        "context_after": context_after,
                    }
                    marker_rule = (
                        "Preserve every [[VTT_n]] marker exactly once, in its original order; these protect "
                        "caption boundaries, line breaks and formatting. Translate text between markers "
                        "without moving it across them. "
                    ) if any("[[VTT_" in item["text"] for item in requested_items) else ""
                    prompt = (
                        f"Translate every item in lines into language code {target_language}. "
                        "Use context_before and context_after only to understand the surrounding "
                        "meaning; do not translate or return those context lines. Preserve terminology "
                        "and fit each translation naturally within its approximate speaking duration. "
                        "Return every line ID exactly once. Never merge or split lines. "
                        + marker_rule + "Return only a "
                        "JSON object with a translations array containing objects with exactly these "
                        "fields: id and text.\n"
                        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    )
                    raw_result = None
                    last_error = None
                    for attempt in range(attempts):
                        try:
                            request_usage = {"provider": PROVIDER_LABELS[provider], "model": selected_model}
                            requests.append(request_usage)
                            def complete(request_usage=request_usage):
                                token = _COMPLETION_USAGE.set(request_usage)
                                try:
                                    return self._complete(
                                        provider, selected_model, prompt, 0.2, True, max_output_tokens, True,
                                    )
                                finally:
                                    _COMPLETION_USAGE.reset(token)
                            raw_result = await asyncio.wait_for(
                                asyncio.to_thread(complete),
                                timeout=120,
                            )
                            break
                        except Exception as error:
                            last_error = error
                            if self._is_model_unavailable(error):
                                break
                            if attempt < attempts - 1:
                                delay = self._retry_delay(error) if provider == "groq" else 1
                                report(f"{model_label}: batch {batch_number} request failed; "
                                       f"retrying in {delay:.1f}s: {str(error)[:180]}")
                                await asyncio.sleep(delay)
                    if raw_result is None:
                        raise RuntimeError(
                            f"Translation batch {batch_number} {request_label} request failed "
                            f"after retry: {last_error}"
                        ) from last_error

                    try:
                        parsed = json.loads(raw_result)
                    except (TypeError, json.JSONDecodeError) as error:
                        if retry_invalid_response:
                            report(f"{model_label}: batch {batch_number} returned invalid JSON; retrying in 1s")
                            await asyncio.sleep(1)
                            return await request_translation(
                                requested_items,
                                f"{request_label}-response-retry",
                                False,
                            )
                        raise ValueError(
                            f"Translation batch {batch_number} returned invalid JSON"
                        ) from error
                    if isinstance(parsed, dict):
                        parsed = parsed.get("translations")
                    if not isinstance(parsed, list):
                        if retry_invalid_response:
                            report(f"{model_label}: batch {batch_number} returned an invalid response; retrying in 1s")
                            await asyncio.sleep(1)
                            return await request_translation(
                                requested_items,
                                f"{request_label}-response-retry",
                                False,
                            )
                        raise ValueError(
                            f"Translation batch {batch_number} returned an invalid response"
                        )
                    expected = {item["id"] for item in requested_items}
                    translations = {
                        str(item["id"]): str(item["text"]).strip()
                        for item in parsed
                        if (
                            isinstance(item, dict)
                            and str(item.get("id", "")) in expected
                            and str(item.get("text", "")).strip()
                        )
                    }
                    for item in requested_items:
                        text = translations.get(item["id"])
                        markers = re.findall(r"\[\[VTT_[0-9]+\]\]", item["text"])
                        if text is not None and markers:
                            translated_markers = re.findall(r"\[\[VTT_[0-9]+\]\]", text)
                            if markers != translated_markers:
                                translations.pop(item["id"])
                                continue
                            source_parts = re.split(r"\[\[VTT_[0-9]+\]\]", item["text"])
                            result_parts = re.split(r"\[\[VTT_[0-9]+\]\]", text)
                            if any(source.strip() and not translated.strip()
                                   for source, translated in zip(source_parts, result_parts)):
                                translations.pop(item["id"])
                    return translations

                try:
                    translated_by_id = await request_translation(batch, "initial")
                    missing = [
                        item for item in batch
                        if item["id"] not in translated_by_id
                    ]
                    if missing:
                        report(f"{model_label}: retrying {len(missing)} missing or invalid lines "
                               f"in batch {batch_number}")
                        translated_by_id.update(
                            await request_translation(missing, "missing-segment")
                        )
                    still_missing = [
                        item["id"] for item in batch
                        if item["id"] not in translated_by_id
                    ]
                    if still_missing:
                        raise RuntimeError(
                            f"Translation batch {batch_number} omitted "
                            f"{len(still_missing)} segment(s) after a targeted retry"
                        )
                except Exception as error:
                    if abort["error"] is None:
                        abort["error"] = error
                    raise
                done.update(translated_by_id)
                finished_batches["count"] += 1
                report(
                    f"Translated batch {finished_batches['count']} of {len(batches)} "
                    f"with {model_label} \u2014 {len(done)} of {total_segments} lines done"
                )

        results = await asyncio.gather(
            *(
                translate_batch(batch, batch_number)
                for batch_number, batch in enumerate(batches, start=1)
            ),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            error = abort["error"] or errors[0]
            unavailable = self._is_model_unavailable(error)
            if unavailable:
                self._mark_unavailable(provider, selected_model)
            elif self._is_quota_error(error) and "resource_exhausted" in str(error).lower():
                self._mark_quota_exhausted(provider, selected_model)
            if len(_chain) < 2 or not (unavailable or self._is_quota_error(error)):
                raise error
            next_provider, next_model = _chain[1]
            print(
                f"{PROVIDER_LABELS[provider]} {selected_model} translation unavailable; "
                f"trying {PROVIDER_LABELS[next_provider]} {next_model} for the "
                f"{total_segments - len(done)} remaining lines: {error}"
            )
            report(
                f"{model_label} is busy or out of quota; continuing the remaining "
                f"{total_segments - len(done)} lines with {PROVIDER_LABELS[next_provider]} {next_model}"
            )
            result = await self.translate_transcript(
                transcript, target_language, _chain=_chain[1:],
                _chain_offset=_chain_offset + 1, on_progress=on_progress,
                _done=done, _started_at=started_at,
                allow_fallback=allow_fallback, _requests=requests,
            )
            result.setdefault("fallback_reason", str(error)[:300])
            return result
        translated = [
            {**item, "text": done[f"segment-{index}"]}
            for index, item in enumerate(transcript)
        ]
        source_text = "\n".join(str(item.get("text", "")).strip() for item in transcript)
        translated_joined = "\n".join(
            str(item.get("text", "")).strip() for item in translated
        )
        if source_text and translated_joined == source_text:
            raise ValueError(
                "Translation model returned the original text unchanged. "
                "Choose another model or confirm that the selected target language differs from the source."
            )
        return {
            "transcript": translated,
            "provider": self._provider_label(provider, _chain_offset),
            "model": selected_model,
            "batch_count": len(batches),
            "context_lines": context_lines,
            "elapsed_seconds": round(time.monotonic() - started_at, 1),
            "usage": translation_usage(requests),
        }

    @staticmethod
    def _translation_batches(items, maximum_items=60, maximum_characters=6000):
        batches = []
        current = []
        current_characters = 0
        for item in items:
            item_characters = len(item["text"])
            if current and (
                len(current) >= maximum_items
                or current_characters + item_characters > maximum_characters
            ):
                batches.append(current)
                current = []
                current_characters = 0
            current.append(item)
            current_characters += item_characters
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _retry_delay(error: Exception) -> float:
        """Use the wait time a rate-limited API asks for (e.g. 'try again in 7.5s')."""
        match = re.search(r"try again in\s+(?:(\d+)m)?([\d.]+)(ms|s)", str(error), re.IGNORECASE)
        if match:
            minutes = int(match.group(1) or 0)
            seconds = float(match.group(2)) / (1000 if match.group(3).lower() == "ms" else 1)
            return min(65.0, minutes * 60 + seconds + 1)
        text = str(error).lower()
        return 20.0 if ("429" in text or "rate" in text) else 2.0

    @staticmethod
    def _is_model_unavailable(error: Exception) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in MODEL_UNAVAILABLE_MARKERS)

    def _mark_unavailable(self, provider, model):
        if not hasattr(self, "unavailable_models"):
            self.unavailable_models = set()
        self.unavailable_models.add((provider, model))
        print(f"{PROVIDER_LABELS[provider]} {model} is not available for this API key; skipping it")

    @staticmethod
    def _is_quota_error(error: Exception) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in (
            "429", "503", "resource_exhausted", "quota", "rate limit", "rate_limit",
            "too many requests", "unavailable", "high demand", "overloaded", "request too large"
        ))

    @staticmethod
    def _fallback_chapters(transcript: list) -> list:
        """Create usable chapters without an external model."""
        chapters = []
        last_start = None
        for item in transcript:
            start = float(item.get("start", 0))
            text = " ".join(str(item.get("text", "")).split())
            if not text:
                continue
            if last_start is None or start - last_start >= 180:
                title = " ".join(text.split()[:6]).strip(".,!?")
                chapters.append({
                    "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
                    "title": title or f"Chapter {len(chapters) + 1}"
                })
                last_start = start
        return chapters or [{"timestamp": "00:00", "title": "Lecture"}]

    @staticmethod
    def _timestamp_or_none(value):
        try:
            return SummaryProcessor._timestamp_seconds(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_chapters(chapters: list, transcript: list, end=None, sectioned=True) -> list:
        """Tidy timestamps and sections without changing topic boundaries by duration."""
        if not isinstance(chapters, list):
            raise ValueError("The AI returned chapters in an invalid format")

        entries = []
        for index, chapter in enumerate(chapters):
            if not isinstance(chapter, dict):
                continue
            raw_title = " ".join(str(chapter.get("title", "")).split()).strip()
            total_seconds = SummaryProcessor._timestamp_or_none(chapter.get("timestamp", "00:00"))
            if total_seconds is None:
                total_seconds = int(float(transcript[index].get("start", 0))) if index < len(transcript) else 0

            if not raw_title:
                matching_text = next(
                    (
                        " ".join(str(item.get("text", "")).split())
                        for item in transcript
                        if abs(float(item.get("start", 0)) - total_seconds) < 30
                    ),
                    f"Chapter {index + 1}",
                )
                raw_title = " ".join(matching_text.split()[:6]).strip(".,!?") or f"Chapter {index + 1}"

            sections = []
            for section in chapter.get("sections") or [] if isinstance(chapter.get("sections"), list) else []:
                if not isinstance(section, dict):
                    continue
                title = " ".join(str(section.get("title", "")).split()).strip(" .,:;")
                start = SummaryProcessor._timestamp_or_none(section.get("timestamp"))
                if not title or start is None:
                    continue
                point = SummaryProcessor._filler_free(section.get("point"))
                sections.append({"start": start, "title": title, **({"point": point} if point else {})})
            summary = SummaryProcessor._filler_free(chapter.get("summary"))
            entries.append({
                "start": total_seconds, "title": raw_title, "sections": sections,
                **({"summary": summary} if summary else {}),
            })
        if not entries:
            raise ValueError("The AI returned no valid chapters")

        entries.sort(key=lambda entry: entry["start"])
        merged = []
        for entry in entries:
            if merged and entry["start"] == merged[-1]["start"]:
                merged[-1]["sections"].extend(entry["sections"])
                continue
            merged.append(entry)

        normalized = []
        for index, entry in enumerate(merged):
            following = merged[index + 1]["start"] if index + 1 < len(merged) else None
            sections, seen = [], set()
            for section in sorted(entry["sections"], key=lambda item: item["start"]):
                if section["start"] < entry["start"] - 5 or (following is not None and section["start"] >= following):
                    continue
                if section["start"] in seen:
                    continue
                seen.add(section["start"])
                sections.append(section)
            if sections and sections[0]["start"] != entry["start"]:
                sections[0] = {**sections[0], "start": entry["start"]}
            item = {
                "timestamp": f"{entry['start'] // 60:02d}:{entry['start'] % 60:02d}",
                "title": entry["title"],
            }
            if entry.get("summary"):
                item["summary"] = entry["summary"]
            if sections:
                item["sections"] = [
                    {
                        "timestamp": f"{section['start'] // 60:02d}:{section['start'] % 60:02d}",
                        "title": section["title"],
                        **({"point": section["point"]} if section.get("point") else {}),
                    }
                    for section in sections
                ]
            normalized.append(item)
        return normalized

    @staticmethod
    def _save_chapters(video_id: str, chapters: list, transcript_language: str = "", transcript=None):
        if not video_id:
            return
        if transcript_language:
            # Chapters of a translated transcript belong to that translation.
            summary_path = SUMMARIES_DIR / f"{video_id}_summary_{transcript_language}.json"
            summary_path.write_text(json.dumps(chapters, ensure_ascii=False), encoding="utf-8")
            if transcript:
                from processors import transcript_versions
                transcript_versions.save_version(
                    video_id, "translation", transcript, language=transcript_language, chapters=chapters,
                )
            return
        summary_path = SUMMARIES_DIR / f"{video_id}.json"
        with summary_path.open("w", encoding="utf-8") as file:
            json.dump(chapters, file)

    @staticmethod
    def _looks_like_legacy_fallback(chapters: list) -> bool:
        if not isinstance(chapters, list) or not chapters:
                return True
        titles = [str(item.get("title", "")).strip().lower() for item in chapters if isinstance(item, dict)]
        if len(titles) != len(chapters) or any(not title for title in titles):
                return True
        if all(title == f"chapter {index + 1}" for index, title in enumerate(titles)):
                return True
        if all(title == f"interval {index + 1}" for index, title in enumerate(titles)):
                return True
        timestamps = []
        for item in chapters:
                try:
                    timestamps.append(SummaryProcessor._timestamp_seconds(str(item["timestamp"])))
                except (KeyError, TypeError, ValueError):
                    return True
        gaps = [right - left for left, right in zip(timestamps, timestamps[1:])]
        return len(gaps) >= 2 and all(gap == 180 for gap in gaps)

    @staticmethod
    def _timestamp_seconds(timestamp: str) -> int:
        parts = [int(float(part)) for part in timestamp.split(":")]
        if len(parts) == 2:
                return parts[0] * 60 + parts[1]
        if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        raise ValueError("Invalid timestamp")

    async def get_summary(self, video_id: str, language: str = ""):
        """Get saved chapter summary for a video.

        When ``language`` matches an existing translation, its chapters are
        returned instead of the original-language ones, so the timeline and
        scene titles keep matching whichever transcript language is active.
        """
        summary_path = str(SUMMARIES_DIR / f"{video_id}.json")
        if language:
            translated_summary_path = str(SUMMARIES_DIR / f"{video_id}_summary_{language}.json")
            if os.path.exists(translated_summary_path):
                summary_path = translated_summary_path
        if os.path.exists(summary_path):
            try:
                with open(summary_path, 'r', encoding='utf-8') as f:
                    chapters = json.load(f)
                if isinstance(chapters, list) and chapters and isinstance(chapters[0], list):
                    chapters = [item for sublist in chapters for item in sublist]
                if self._looks_like_legacy_fallback(chapters):
                    return JSONResponse({
                        "success": True,
                        "chapters": [],
                        "exists": False,
                        "stale": True
                    })
                chapters = self._normalize_chapters(chapters, [])
                return JSONResponse({
                    "success": True,
                    "chapters": chapters,
                    "exists": True
                })
            except Exception as e:
                return JSONResponse({
                    "success": False,
                    "error": str(e)
                })
        return JSONResponse({
            "success": True,
            "chapters": [],
            "exists": False
        })