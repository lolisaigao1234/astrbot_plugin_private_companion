"""Remove private file credentials left in SDK-generated provider content."""

from companion_v2.adapters.astrbot.telegram_media import private_token
from astrbot.core.agent.message import AudioURLPart, ImageURLPart, TextPart, ThinkPart


def protect_telegram_request(event, request):
    token = private_token(event)
    if not isinstance(token, str) or not token:
        return

    def clean(value):
        if isinstance(value, str):
            return value.replace(token, "[redacted]")
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            for kind in ("audio_url", "image_url"):
                url = value.get(kind)
                if isinstance(url, dict):
                    url = url.get("url")
                if isinstance(url, str) and token in url:
                    return {"type": "text", "text": "[附件不可用]"}
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (ImageURLPart, AudioURLPart)):
            url = getattr(value, value.type).url
            return TextPart(text="[附件不可用]") if token in url else value
        if isinstance(value, TextPart):
            value.text = clean(value.text)
        elif isinstance(value, ThinkPart):
            value.think = clean(value.think)
        return value

    for name in ("prompt", "system_prompt", "contexts", "extra_user_content_parts"):
        value = getattr(request, name, None)
        if value:
            setattr(request, name, clean(value))
    dropped = False
    for name in ("image_urls", "audio_urls"):
        urls = getattr(request, name, None)
        if urls:
            retained = [url for url in urls if not isinstance(url, str) or token not in url]
            dropped |= len(retained) != len(urls)
            setattr(request, name, retained)
    if dropped:
        parts = list(getattr(request, "extra_user_content_parts", ()) or ())
        if not any(isinstance(part, TextPart) and part.text == "[附件不可用]" for part in parts):
            request.extra_user_content_parts = [*parts, TextPart(text="[附件不可用]")]
