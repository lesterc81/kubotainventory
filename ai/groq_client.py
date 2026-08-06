import os
from openai import OpenAI

_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ")
        if not api_key:
            raise RuntimeError("GROQ API key not found in environment")
        _client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key,
        )
    return _client

def chat(prompt, system="You are an IT asset management assistant.", model="llama-3.3-70b-versatile", max_tokens=1024):
    client = get_client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()
