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


# -- the same surface, but the endpoint is a fixture instead of a gateway ----

def replay_client(mode, candidates, vacuous_reply=None, model="replay-fixture"):
    """A client that answers from recorded candidates, in order.

    Why this exists: the loop's two feedback paths are only observable when the
    first candidate is wrong, and a live model is not reproducible -- the same
    intent produces a different assertion on the next call, so an ablation over a
    live endpoint measures the endpoint's variance as much as the loop. A fixture
    makes the comparison exact and, more importantly, free: the headline table in
    the README must be re-derivable on a machine with no API key at all.

    `mode` is the arm of the experiment, and it is what makes the arms differ for
    a reason rather than by temperature:

      oneshot   the model's text is taken verbatim (what a naive wrapper does)
      raw       the text is returned unchanged for the loop to run the linter on
      repaired  what `repair.normalise` produces (the model is *told* the
                accepted subset, which is the only difference from `raw`)
    """
    class ReplayClient(Client):
        def __init__(self):
            self.base_url = "fixture://replay"
            self.model = model
            self.key = "-"
            self.timeout = 0
            self.max_retries = 1
            self.temperature = 0.0
            self.calls = 0
            self.seconds = 0.0
            self._queue = list(candidates)

        def chat(self, prompt, system=None, max_tokens=1200, temperature=None):
            self.calls += 1
            idx = min(self.calls - 1, len(self._queue) - 1)
            src = self._queue[idx] if self._queue else ""
            if mode == "repaired":
                from . import repair
                src = repair.normalise(src).source
            return "```systemverilog\n%s\n```" % src

    return ReplayClient()


def queue_after_first(first, rest):
    """Fixture helper: the first reply, then `rest` for every later call."""
    return [first] + list(rest)
