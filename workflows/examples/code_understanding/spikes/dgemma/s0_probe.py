"""S0 exit predicate: can an L40S-served DiffusionGemma answer a one-token
decision, and does it come back with a usable logprob distribution?

The Red Hat decision server at /v1/systemone is an example, not a vLLM route,
so the questions go over standard completions with max_tokens=1. What we want
back is the distribution over the answer position, which is the same thing
/v1/systemone would have reported as calibrated confidence.

Two things the first attempt got wrong, both worth keeping written down:

  - Diffusion models reject temperature, min_p, seed, min_tokens, logit_bias,
    bad_words and allowed_token_ids. Denoising a canvas has no per-token
    sampling step for those to act on.
  - Straight off the chat template the first token is <|channel> at p=1.0,
    because the model opens a thought channel before answering. There is no
    answer mass at position 0 at all. So we render the chat prompt through
    /tokenize, append an empty closed thought channel, and send token ids to
    /v1/completions. The answer then lands at position 0 where we can read it.

The questions are the real ones from the extraction work, not toys: layer
membership for a name, granularity, and whether an attribute read is a
declared entity.
"""

import json
import math
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
MODEL = "dgemma"

# Closes the thought channel the model would otherwise open, so the very next
# token it emits is the answer.
PREFILL = "<|channel>thought\n<channel|>"

# Each case is (label, question, expected). Expectations come from the celery
# 2.5.5 checkout and the Python stdlib, not from the model, so a miss is real.
CASES = [
    ("stdlib/time", "Is `time` a module of the Python standard library? Answer yes or no.", "yes"),
    ("stdlib/socket", "Is `socket` a module of the Python standard library? Answer yes or no.", "yes"),
    ("pkg/kombu", "Is `kombu` a module of the Python standard library? Answer yes or no.", "no"),
    ("pkg/anyjson", "Is `anyjson` a module of the Python standard library? Answer yes or no.", "no"),
    ("granularity", "A Python file contains `from kombu.clocks import LamportClock`. "
                    "Is `kombu.clocks` the name of a third-party distribution? Answer yes or no.", "no"),
    ("attr-read", "In Python source, `self.conf.EMAIL_HOST` appears. Is `self.conf.EMAIL_HOST` "
                  "a name declared or imported by this file? Answer yes or no.", "no"),
    ("builtin", "Is `isinstance` a module? Answer yes or no.", "no"),
    # The shadowing case the prompt calls out by name: a stdlib-looking name
    # that a manifest declares as a distribution.
    ("shadow", "A requirements file declares `multiprocessing==2.6.2.1`. In that project, is "
               "`multiprocessing` a third-party distribution rather than the standard library "
               "module? Answer yes or no.", "yes"),
]


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def build_prompt_ids(question: str) -> list[int]:
    """Chat-render the question, then append the closed thought channel."""
    head = post("/tokenize", {
        "model": MODEL,
        "messages": [{"role": "user", "content": question}],
        "add_generation_prompt": True,
    })["tokens"]
    tail = post("/tokenize", {
        "model": MODEL, "prompt": PREFILL, "add_special_tokens": False,
    })["tokens"]
    return head + tail


def ask(question: str, n_logprobs: int = 20) -> tuple[dict, float]:
    ids = build_prompt_ids(question)
    t = time.monotonic()
    payload = post("/v1/completions", {
        "model": MODEL,
        "prompt": ids,
        "max_tokens": 1,
        "logprobs": n_logprobs,
    })
    return payload, time.monotonic() - t


def main() -> None:
    with urllib.request.urlopen(BASE + "/v1/models", timeout=30) as r:
        print("models:", [m["id"] for m in json.loads(r.read())["data"]])

    rows = []
    for label, question, expected in CASES:
        try:
            payload, dt = ask(question)
        except urllib.error.HTTPError as exc:  # the failure is the finding
            print(f"{label:14s} FAILED HTTP {exc.code}: {exc.read().decode()[:400]}")
            continue

        lp = payload["choices"][0]["logprobs"]
        raw = payload["choices"][0]["text"]
        top = lp["top_logprobs"][0]

        probs = {}
        for tok, val in top.items():
            probs[tok.strip().lower()] = probs.get(tok.strip().lower(), 0.0) + math.exp(val)
        y = probs.get("yes", 0.0) + probs.get("y", 0.0)
        n = probs.get("no", 0.0) + probs.get("n", 0.0)
        mass = y + n
        if mass == 0:
            print(f"{label:14s} NO-ANSWER-MASS raw={raw!r} top5=" +
                  ", ".join(f"{t!r}:{math.exp(v):.3f}" for t, v in list(top.items())[:5]))
            rows.append((label, "NONE", dt))
            continue

        conf = y / mass
        said = "yes" if y >= n else "no"
        ok = "OK " if said == expected else "MISS"
        rows.append((label, ok, dt))
        print(f"{label:14s} {ok} said={said:3s} exp={expected:3s} "
              f"P(yes)={conf:.3f} answer_mass={mass:.3f} {dt * 1000:.0f}ms")

    scored = [r for r in rows if r[1] in ("OK ", "MISS")]
    if scored:
        hit = sum(1 for r in scored if r[1] == "OK ")
        lat = sorted(r[2] for r in rows)
        print(f"\n{hit}/{len(scored)} correct ({len(rows) - len(scored)} with no answer mass), "
              f"median latency {lat[len(lat) // 2] * 1000:.0f}ms")


if __name__ == "__main__":
    main()
