"""Resource Finder: analyze a document or topic, search real discovery services, rank and filter.

Local Ollama is used first for content analysis and relevance judgement; Groq is used only when
the caller explicitly allows it, TUTOR_LOCAL_ONLY is not set, and the local model failed.
Resources always come from the providers in resource_providers.py, never from a model.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import resource_providers as providers
from fusion import FusionValidationError, MAX_DOCUMENT_BYTES, _detect_document_kind, extract_document_text
from tutor import LOCAL_TUTOR_HOST, LOCAL_TUTOR_MODEL, OpenAI, _local_ollama_available

PAGE_SIZE = 10
MAX_RESULTS = 200
VALIDATE_TOP = 40
MAX_EXCERPT_CHARS = 4000
STOPWORDS = set(
    "the and for are but not you all any can had her was one our out has have this that with from they been were which "
    "their will would there what when your said each she how its into than then them these some such only over also "
    "more most other about after before between both during through under again further once here very just like "
    "chapter figure table page slide lecture introduction example using used use based".split()
)
DIFFICULTIES = ("beginner", "intermediate", "advanced")
BEGINNER_CUES = re.compile(r"\b(intro(duction)?|beginner|basics?|fundamentals?|crash course|for dummies|101|explained|getting started|overview)\b", re.I)
ADVANCED_CUES = re.compile(r"\b(advanced|graduate|phd|research|state[- ]of[- ]the[- ]art|theorem|proof|rigorous|seminar|survey)\b", re.I)
_SEARCHES: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_LOCK = threading.Lock()
MAX_STORED_SEARCHES = 20


class ResourceFinderError(ValueError):
    """Invalid input or an unrecoverable failure; the message is safe to show to users."""


def local_only() -> bool:
    return os.getenv("TUTOR_LOCAL_ONLY", "false").lower() not in {"0", "false", "no"}


# --- Extraction ---------------------------------------------------------------

def extract_text(filename: str, data: bytes) -> str:
    if not data:
        raise ResourceFinderError("The uploaded file is empty.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ResourceFinderError("The uploaded file exceeds the 20 MB limit.")
    try:
        _detect_document_kind(filename or "", data)
        text = extract_document_text(data, filename=filename)
    except FusionValidationError as error:
        raise ResourceFinderError(str(error)) from error
    except Exception as error:
        raise ResourceFinderError(f"Could not read {filename}: {error}") from error
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) < 20:
        raise ResourceFinderError(f"No readable text was found in {filename}.")
    return text


# --- Content analysis ---------------------------------------------------------

def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9\-+#]{2,}", (text or "").lower()) if t not in STOPWORDS]


def heuristic_analysis(text: str, topic: str = "") -> Dict[str, Any]:
    """Deterministic keyword/phrase extraction that always works offline."""
    tokens = _tokens(text)
    unigrams = Counter(tokens)
    bigrams = Counter(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
    phrases = [p for p, c in bigrams.most_common(30) if c >= 2][:6]
    keywords = [w for w, c in unigrams.most_common(25) if len(w) > 3][:12]
    lines = [l.strip() for l in (text or "").splitlines() if 3 <= len(l.strip()) <= 80 and not l.strip().endswith(".")]
    main = topic.strip() or (lines[0] if lines else (phrases[0] if phrases else " ".join(keywords[:3])))
    subtopics = [p for p in phrases if p.lower() != main.lower()][:5] or keywords[:4]
    return {"topic": main[:120], "subtopics": subtopics, "keywords": keywords, "concepts": phrases or keywords[:6],
            "level": None, "method": "heuristic"}


def _strings(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    seen, out = set(), []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip().lower() not in seen:
            seen.add(item.strip().lower())
            out.append(item.strip()[:80])
    return out[:limit]


def _parse_json(text: str) -> Optional[Any]:
    match = re.search(r"[\[{].*[\]}]", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _ollama(system: str, prompt: str) -> Optional[Any]:
    if not _local_ollama_available():
        return None
    payload = {"model": LOCAL_TUTOR_MODEL, "prompt": f"{system}\n\n{prompt}", "stream": False, "format": "json",
               "options": {"temperature": 0.1}}
    request = Request(f"{LOCAL_TUTOR_HOST.rstrip('/')}/api/generate", method="POST",
                      headers={"Content-Type": "application/json"}, data=json.dumps(payload).encode("utf-8"))
    try:
        with urlopen(request, timeout=90) as response:
            return _parse_json(json.loads(response.read().decode("utf-8", "replace")).get("response", ""))
    except Exception:
        return None


def _groq(system: str, prompt: str) -> Optional[Any]:
    if not os.getenv("GROQ_API_KEY") or OpenAI is None:
        return None
    try:
        client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
        reply = client.chat.completions.create(
            model=os.getenv("TUTOR_GROQ_MODEL", "openai/gpt-oss-20b"),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}], temperature=0.1)
        return _parse_json(reply.choices[0].message.content or "")
    except Exception:
        return None


def ask_llm(system: str, prompt: str, validate: Callable[[Any], Optional[Any]], *, allow_cloud: bool,
            notices: List[str], what: str) -> Tuple[Optional[Any], Optional[str]]:
    """Try local first; cloud only if allowed and local failed or returned an unusable answer."""
    result = validate(_ollama(system, prompt))
    if result is not None:
        return result, "local"
    if allow_cloud and not local_only():
        result = validate(_groq(system, prompt))
        if result is not None:
            notices.append(f"The local model was unavailable or insufficient, so {what} was sent to Groq (cloud) with your consent.")
            return result, "cloud"
    return None, None


ANALYSIS_SYSTEM = (
    "You analyze study material to help find learning resources. Use only the supplied text. Reply with one JSON "
    "object: {\"topic\": main topic (max 8 words), \"subtopics\": up to 6 strings, \"keywords\": up to 12 strings, "
    "\"concepts\": up to 8 key concepts, \"level\": \"beginner\"|\"intermediate\"|\"advanced\"}."
)


def _validate_analysis(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("topic"), str) or not data["topic"].strip():
        return None
    keywords = _strings(data.get("keywords"), 12)
    if not keywords:
        return None
    level = data.get("level")
    return {"topic": data["topic"].strip()[:120], "subtopics": _strings(data.get("subtopics"), 6), "keywords": keywords,
            "concepts": _strings(data.get("concepts"), 8), "level": level if level in DIFFICULTIES else None}


def analyze_content(text: str, topic: str, *, use_llm: bool, allow_cloud: bool, notices: List[str]) -> Dict[str, Any]:
    base = heuristic_analysis(text, topic)
    if not use_llm:
        return base
    excerpt = text[:MAX_EXCERPT_CHARS]
    prompt = (f"Topic given by the user: {topic}\n\n" if topic else "") + "Material:\n" + excerpt
    refined, where = ask_llm(ANALYSIS_SYSTEM, prompt, _validate_analysis, allow_cloud=allow_cloud, notices=notices,
                             what="an excerpt of your document")
    if refined is None:
        notices.append("Local AI analysis was unavailable, so keyword extraction was used." if not allow_cloud or local_only()
                       else "AI analysis was unavailable, so keyword extraction was used.")
        return base
    return {
        "topic": topic.strip() or refined["topic"],
        "subtopics": refined["subtopics"] or base["subtopics"],
        "keywords": list(dict.fromkeys(refined["keywords"] + base["keywords"]))[:14],
        "concepts": refined["concepts"] or base["concepts"],
        "level": refined["level"], "method": f"llm-{where}",
    }


# --- Search, dedupe, validation -----------------------------------------------

def build_queries(analysis: Dict[str, Any]) -> List[str]:
    topic = analysis["topic"]
    queries = [topic]
    for sub in analysis["subtopics"][:2]:
        if sub.lower() not in topic.lower():
            queries.append(f"{topic} {sub}")
    if len(queries) < 3 and analysis["keywords"]:
        extra = " ".join(k for k in analysis["keywords"][:3] if k.lower() not in topic.lower())
        if extra:
            queries.append(f"{topic} {extra}")
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:3]


def search_all(queries: List[str], types: Optional[set], provider_map: Dict[str, Dict[str, Any]],
               warnings: List[str]) -> List[Dict[str, Any]]:
    jobs = []
    for name, spec in provider_map.items():
        enabled = spec.get("enabled")
        if enabled is not None and not enabled():
            if not types or spec["types"] & types:
                warnings.append(f"{name}: not configured (optional; set its API key to enable).")
            continue
        if types and not spec["types"] & types:
            continue
        for index, query in enumerate(queries):
            jobs.append((name, spec, query, 10 if index == 0 else 6))

    def run(job):
        name, spec, query, limit = job
        try:
            return name, spec["run"](query, limit), None
        except providers.ProviderError as error:
            return name, [], str(error)
        except Exception as error:  # a provider bug must not break the whole search
            return name, [], f"unexpected error ({type(error).__name__})"

    results, failed = [], {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, items, error in pool.map(run, jobs):
            for item in items:
                item["found_by"] = name
                item["query"] = len(results)
                results.append(item)
            if error:
                failed.setdefault(name, error)
    for name, error in failed.items():
        warnings.append(f"{name}: {error}")
    return results


def canonical_key(resource: Dict[str, Any]) -> str:
    url = resource.get("url") or ""
    parsed = urlparse(url)
    host = providers.domain_of(url)
    if host in {"youtube.com", "m.youtube.com", "youtu.be"}:
        video = parse_qs(parsed.query).get("v", [None])[0] or (parsed.path.strip("/") if host == "youtu.be" else None)
        playlist = parse_qs(parsed.query).get("list", [None])[0]
        if video:
            return f"yt:{video}"
        if playlist:
            return f"ytlist:{playlist}"
    if host.endswith("arxiv.org"):
        match = re.search(r"(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", parsed.path)
        if match:
            return f"arxiv:{match.group(1)}"
    if "doi.org" in host:
        return "doi:" + parsed.path.strip("/").lower()
    return f"{host}{parsed.path.rstrip('/').lower()}"


def _title_key(resource: Dict[str, Any]) -> str:
    return f"{resource['type']}:" + re.sub(r"[^a-z0-9]+", " ", resource.get("title", "").lower()).strip()


def dedupe(resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    alias: Dict[str, str] = {}
    for resource in resources:
        if not resource.get("title") or not re.match(r"^https?://", resource.get("url") or ""):
            continue
        keys = [canonical_key(resource), _title_key(resource)]
        existing = next((alias[k] for k in keys if k in alias), None)
        if existing is None:
            existing = keys[0]
            by_key[existing] = resource
            resource["found_by_all"] = [resource["found_by"]]
        else:
            kept = by_key[existing]
            kept["found_by_all"] = sorted(set(kept["found_by_all"]) | {resource["found_by"]})
            if len(resource.get("description", "")) > len(kept.get("description", "")):
                for field in ("description", "published", "language", "authors", "hints"):
                    if resource.get(field):
                        kept[field] = resource[field]
        for key in keys:
            alias[key] = existing
    return list(by_key.values())


def validate_links(resources: List[Dict[str, Any]], limit: int = VALIDATE_TOP) -> List[Dict[str, Any]]:
    """Drop links that are definitely gone; mark the rest ok/unverified/unchecked."""
    head = resources[:limit]
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda r: providers.check_link(r["url"]), head))
    kept = []
    for resource, status in zip(head, statuses):
        if status == "broken":
            continue
        resource["link_status"] = status
        kept.append(resource)
    for resource in resources[limit:]:
        resource["link_status"] = "unchecked"
        kept.append(resource)
    return kept


# --- Ranking ------------------------------------------------------------------

def _terms(analysis: Dict[str, Any]) -> List[Tuple[str, float]]:
    weighted: Dict[str, float] = {}
    for weight, items in ((3.0, [analysis["topic"]]), (2.0, analysis["subtopics"]), (1.5, analysis["concepts"]),
                          (1.0, analysis["keywords"])):
        for item in items:
            key = item.lower().strip()
            if key:
                weighted[key] = max(weighted.get(key, 0), weight)
    return sorted(weighted.items(), key=lambda kv: -kv[1])


def _contains(haystack: str, term: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", haystack))


def _topic_words_match(haystack: str, term: str) -> bool:
    words = [w for w in _tokens(term)]
    return bool(words) and all(_contains(haystack, w) for w in words)


def guess_difficulty(resource: Dict[str, Any]) -> Optional[str]:
    text = f"{resource.get('title', '')} {resource.get('description', '')[:300]}"
    if ADVANCED_CUES.search(text):
        return "advanced"
    if BEGINNER_CUES.search(text):
        return "beginner"
    if resource["type"] == "paper":
        return "advanced"
    return None


def normalize_language(code: Any) -> Optional[str]:
    code = str(code or "").strip().lower()
    mapping = {"eng": "en", "ger": "de", "deu": "de", "ara": "ar", "pol": "pl", "fre": "fr", "fra": "fr", "spa": "es"}
    code = mapping.get(code, code)
    return code.split("-")[0][:3] if re.match(r"^[a-z]{2,3}(-[a-z]+)?$", code) else None


def score_resource(resource: Dict[str, Any], terms: List[Tuple[str, float]], analysis: Dict[str, Any]) -> None:
    title = resource["title"].lower()
    body = " ".join([resource.get("description", ""), " ".join(resource.get("hints", []))]).lower()
    total = sum(w for _, w in terms) or 1.0
    matched, covered, in_title = 0.0, [], []
    for term, weight in terms:
        title_hit = _topic_words_match(title, term)
        body_hit = _topic_words_match(body, term)
        if title_hit:
            matched += weight * 1.0
            in_title.append(term)
        elif body_hit:
            matched += weight * 0.6
        if title_hit or body_hit:
            covered.append(term)
    lexical = min(1.0, matched / (total * 0.45))
    trust = 1.0 if providers.is_trusted_domain(resource["url"]) or resource["found_by"] in {"arXiv", "OpenAlex", "Open Library"} else 0.4
    year = int((resource.get("published") or "0000")[:4] or 0)
    recency = max(0.0, min(1.0, 1 - (date.today().year - year) / 20)) if year else 0.3
    popularity = min(1.0, math.log10(1 + (resource.get("popularity") or 0)) / 7)
    agreement = min(1.0, (len(resource.get("found_by_all", [])) - 1) * 0.5)
    resource["lexical"] = round(lexical, 4)
    resource["score"] = round(0.65 * lexical + 0.12 * trust + 0.08 * recency + 0.08 * popularity + 0.07 * agreement, 4)
    resource["covered_concepts"] = covered[:8]
    resource["difficulty"] = guess_difficulty(resource)
    resource["language"] = normalize_language(resource.get("language"))
    resource["explanation"] = (
        "Matches " + ", ".join(in_title[:3]) + " in the title" if in_title else
        ("Mentions " + ", ".join(covered[:3]) + " in its description" if covered else "Related to your topic")
    ) + (f"; found by {len(resource['found_by_all'])} sources." if len(resource.get("found_by_all", [])) > 1 else ".")


RERANK_SYSTEM = (
    "You judge how relevant candidate learning resources are to a study topic. Judge only from the given titles and "
    "descriptions; never add resources. Reply with a JSON array: [{\"index\": number, \"score\": 0..1, "
    "\"reason\": one short sentence}] for the candidates you can judge."
)


def llm_rerank(resources: List[Dict[str, Any]], analysis: Dict[str, Any], *, allow_cloud: bool, notices: List[str],
               top: int = 15) -> str:
    head = resources[:top]
    if not head:
        return "none"
    candidates = "\n".join(f"{i}. [{r['type']}] {r['title']} - {r.get('description', '')[:160]}" for i, r in enumerate(head))
    prompt = (f"Topic: {analysis['topic']}\nSubtopics: {', '.join(analysis['subtopics'])}\n"
              f"Concepts: {', '.join(analysis['concepts'])}\n\nCandidates:\n{candidates}")

    def validate(data: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), None)
        if not isinstance(data, list):
            return None
        rows = [d for d in data if isinstance(d, dict) and type(d.get("index")) is int and 0 <= d["index"] < len(head)
                and isinstance(d.get("score"), (int, float)) and 0 <= d["score"] <= 1]
        return rows if len(rows) >= max(1, len(head) // 2) else None

    rows, where = ask_llm(RERANK_SYSTEM, prompt, validate, allow_cloud=allow_cloud, notices=notices,
                          what="the titles and descriptions of candidate resources")
    if rows is None:
        return "heuristic"
    for row in rows:
        resource = head[row["index"]]
        resource["score"] = round(0.5 * resource["score"] + 0.5 * float(row["score"]), 4)
        if isinstance(row.get("reason"), str) and row["reason"].strip():
            resource["explanation"] = row["reason"].strip()[:240]
        resource["ai_judged"] = where
    return f"llm-{where}"


# --- Orchestration and filtering ----------------------------------------------

def find_resources(*, topic: str = "", filename: str = "", data: Optional[bytes] = None, types: Optional[List[str]] = None,
                   use_llm: bool = True, allow_cloud_fallback: bool = False, validate: bool = True,
                   provider_map: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    topic = (topic or "").strip()[:200]
    if not topic and not data:
        raise ResourceFinderError("Enter a topic or upload a PDF, PowerPoint, or text file.")
    wanted = {t for t in (types or []) if t in providers.RESOURCE_TYPES} or None
    text = extract_text(filename, data) if data else topic
    notices: List[str] = []
    allow_cloud = bool(allow_cloud_fallback) and not local_only()
    if allow_cloud_fallback and not allow_cloud:
        notices.append("Cloud fallback is disabled because TUTOR_LOCAL_ONLY is set.")
    analysis = analyze_content(text, topic, use_llm=use_llm, allow_cloud=allow_cloud, notices=notices)
    queries = build_queries(analysis)
    warnings: List[str] = []
    found = search_all(queries, wanted, provider_map or providers.default_providers(), warnings)
    resources = dedupe(found)
    terms = _terms(analysis)
    for resource in resources:
        score_resource(resource, terms, analysis)
    resources = [r for r in resources if r["lexical"] > 0]
    resources.sort(key=lambda r: -r["score"])
    ranking = llm_rerank(resources, analysis, allow_cloud=allow_cloud, notices=notices) if use_llm else "heuristic"
    resources.sort(key=lambda r: -r["score"])
    resources = resources[:MAX_RESULTS]
    if validate:
        resources = validate_links(resources)
    for position, resource in enumerate(resources, 1):
        resource["rank"] = position
        for private in ("query", "lexical", "found_by_all"):
            resource.pop(private, None)
    if not resources:
        warnings.append("No qualifying resources were found. Try a broader topic or enable more resource types.")
    record = {
        "search_id": uuid.uuid4().hex, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "analysis": analysis,
        "queries": queries, "resources": resources, "ranking_method": ranking, "warnings": warnings,
        "notices": notices, "cloud_used": any("Groq" in n for n in notices), "source_file": filename or None,
    }
    with _LOCK:
        _SEARCHES[record["search_id"]] = record
        while len(_SEARCHES) > MAX_STORED_SEARCHES:
            _SEARCHES.popitem(last=False)
    return record


def apply_filters(resources: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    def year(resource):
        value = (resource.get("published") or "")[:4]
        return int(value) if value.isdigit() else None

    def keep(r):
        if filters.get("type") and r["type"] != filters["type"]:
            return False
        if filters.get("platform") and r["platform"] != filters["platform"]:
            return False
        if filters.get("difficulty") and r.get("difficulty") != filters["difficulty"]:
            return False
        if filters.get("language") and r.get("language") != normalize_language(filters["language"]):
            return False
        if filters.get("topic") and filters["topic"].lower() not in [c.lower() for c in r.get("covered_concepts", [])]:
            return False
        low, high = filters.get("year_from"), filters.get("year_to")
        if low or high:
            published = year(r)
            if published is None or (low and published < low) or (high and published > high):
                return False
        return True

    return [r for r in resources if keep(r)]


def facets(resources: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    def count(values):
        return dict(Counter(v for v in values if v).most_common())
    return {
        "type": count(r["type"] for r in resources), "platform": count(r["platform"] for r in resources),
        "difficulty": count(r.get("difficulty") for r in resources), "language": count(r.get("language") for r in resources),
        "topic": count(c for r in resources for c in r.get("covered_concepts", [])),
    }


def get_page(search_id: str, filters: Optional[Dict[str, Any]] = None, offset: int = 0, limit: int = PAGE_SIZE) -> Dict[str, Any]:
    with _LOCK:
        record = _SEARCHES.get(search_id)
    if record is None:
        raise ResourceFinderError("This search has expired. Run it again.")
    matching = apply_filters(record["resources"], filters or {})
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 50))
    page = matching[offset:offset + limit]
    return {
        "search_id": search_id, "analysis": record["analysis"], "queries": record["queries"],
        "total": len(matching), "overall_total": len(record["resources"]), "offset": offset,
        "has_more": offset + limit < len(matching), "results": page, "facets": facets(record["resources"]),
        "ranking_method": record["ranking_method"], "warnings": record["warnings"], "notices": record["notices"],
        "cloud_used": record["cloud_used"],
    }
