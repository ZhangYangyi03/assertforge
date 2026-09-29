"""Minimal OpenAI-compatible chat client, for any endpoint the operator has keys for.

Deliberately not a framework. The assertion loop calls this a few hundred times
per benchmark run and needs three things only: a timeout that is short enough to
keep the loop moving, a retry that survives a 503, and the raw text back.
"""

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("ASSERTFORGE_BASE_URL", "https://aiping.cn/api/v1")
DEFAULT_MODEL = os.environ.get("ASSERTFORGE_MODEL", "Qwen3.5-Flash")
KEY_PATHS = [
    os.path.expandvars(r"%LOCALAPPDATA%\hermes\aiping_key.txt"),
    os.path.expanduser("~/.assertforge/key.txt"),
]


class LLMError(RuntimeError):
    pass


def load_key(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("ASSERTFORGE_API_KEY")
    if env:
        return env.strip()
    for p in KEY_PATHS:
        if os.path.exists(p):
            return open(p, encoding="utf-8", errors="ignore").read().strip()
    raise LLMError(
        "no API key: set ASSERTFORGE_API_KEY or put one in "
        + " or ".join(KEY_PATHS)
    )


class Client:
    def __init__(self, base_url=None, model=None, key=None, timeout=90,
                 max_retries=3, temperature=0.2):
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.key = load_key(key)
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.calls = 0
        self.seconds = 0.0

    def chat(self, prompt, system=None, max_tokens=1200, temperature=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.key,
            },
        )
        last = None
        for attempt in range(self.max_retries):
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                self.calls += 1
                self.seconds += time.time() - t0
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last = "HTTP %s: %s" % (e.code, e.read()[:200])
                # 503 from a shared gateway is the ordinary case, not the exception.
                if e.code in (429, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception as e:  # timeout, connection reset, bad json
                last = "%s: %s" % (type(e).__name__, e)
                time.sleep(2 * (attempt + 1))
        raise LLMError("chat failed after %d attempts (%s)" % (self.max_retries, last))

    def stats(self):
        avg = (self.seconds / self.calls) if self.calls else 0.0
        return {"model": self.model, "calls": self.calls,
                "seconds": round(self.seconds, 2), "avg_s": round(avg, 2)}
