"""Resolve saved slide title translations without changing slide data."""
def scene_title(scene, language=""):
    translated = scene.get("title_translations") or {}
    if language and isinstance(translated, dict) and language in translated:
        return translated[language]
    if not language or not scene.get("title_language") or scene.get("title_language") == language:
        return scene.get("title") or ""
    return ""


def localized_scenes(scenes, language=""):
    return [{**scene, "title": scene_title(scene, language),
             "title_edited": bool(scene.get("title_edited") or (scene.get("title_translations") or {}).get(language))}
            for scene in scenes]
