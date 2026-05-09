import os
from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = 8096


def complete(system: str, messages: list[dict]) -> tuple[str, int, int]:
    """
    Call the configured LLM.
    messages: [{"role": "user"|"assistant", "content": str}, ...]
    Returns: (response_text, input_tokens, output_tokens)
    """
    if PROVIDER == "anthropic":
        return _anthropic(system, messages)
    elif PROVIDER in ("openai", "xai", "meta"):
        return _openai_compatible(system, messages)
    elif PROVIDER == "google":
        return _google(system, messages)
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{PROVIDER}'. Choose from: anthropic, openai, google, xai, meta"
        )


def _anthropic(system, messages):
    try:
        import anthropic
    except ImportError:
        raise ImportError("Run: pip install anthropic")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
    )
    return (
        response.content[0].text,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )


def _openai_compatible(system, messages):
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Run: pip install openai")

    base_urls = {
        "openai": None,
        "xai": "https://api.x.ai/v1",
        "meta": "https://api.llama.com/compat/v1/",
    }
    api_key_vars = {
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
        "meta": "META_API_KEY",
    }

    kwargs = {"api_key": os.getenv(api_key_vars[PROVIDER])}
    if base_urls[PROVIDER]:
        kwargs["base_url"] = base_urls[PROVIDER]

    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "system", "content": system}] + messages,
    )
    return (
        response.choices[0].message.content,
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
    )


def _google(system, messages):
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError("Run: pip install google-generativeai")

    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    model = genai.GenerativeModel(model_name=MODEL, system_instruction=system)

    history = [
        {
            "role": "user" if m["role"] == "user" else "model",
            "parts": [m["content"]],
        }
        for m in messages[:-1]
    ]
    chat = model.start_chat(history=history)
    response = chat.send_message(messages[-1]["content"])

    return (
        response.text,
        response.usage_metadata.prompt_token_count,
        response.usage_metadata.candidates_token_count,
    )
