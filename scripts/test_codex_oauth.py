"""Test that the Codex OAuth proxy is working with ChatOpenAI.

Prerequisites:
  1. codex login          (authenticate with your ChatGPT subscription)
  2. npx openai-oauth     (start the proxy at http://127.0.0.1:10531/v1)
  3. python scripts/test_codex_oauth.py
"""

import os
import sys

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

PROXY_URL = os.environ.get("CODEX_PROXY_URL", "http://127.0.0.1:10531/v1")
MODEL = os.environ.get("CODEX_MODEL", "gpt-5.4")


def test_basic_invoke():
    """Test a simple invoke through the proxy."""
    llm = ChatOpenAI(
        model=MODEL,
        base_url=PROXY_URL,
        api_key="codex-oauth",  # proxy handles auth
    )
    resp = llm.invoke([HumanMessage(content="Say hello in exactly 3 words.")])
    print(f"[OK] basic invoke: {resp.content}")


def test_streaming():
    """Test streaming through the proxy."""
    llm = ChatOpenAI(
        model=MODEL,
        base_url=PROXY_URL,
        api_key="codex-oauth",
    )
    print("[..] streaming: ", end="", flush=True)
    for chunk in llm.stream([HumanMessage(content="Count from 1 to 5.")]):
        print(chunk.content, end="", flush=True)
    print("\n[OK] streaming complete")


def test_list_models():
    """Check what models are available through the proxy."""
    import urllib.request
    import json

    try:
        req = urllib.request.Request(f"{PROXY_URL}/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            print(f"[OK] available models: {', '.join(models)}")
    except Exception as e:
        print(f"[FAIL] list models: {e}")


if __name__ == "__main__":
    print(f"Proxy: {PROXY_URL}")
    print(f"Model: {MODEL}\n")

    tests = [test_list_models, test_basic_invoke, test_streaming]
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            sys.exit(1)

    print("\nAll tests passed!")
