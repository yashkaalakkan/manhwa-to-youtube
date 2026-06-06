"""
generate_script.py
Generates narration scripts for ONE chapter:
  - 1 short script  (entire chapter = 1 YouTube Short)
  - 1 full_episode narration chunk (combined later across all chapters)

AI provider priority:
  1. Gemini 2.5 Flash-Lite (free, 250k TPM, 1000 RPD) — primary
  2. Groq llama-3.1-8b-instant (free, multiple keys)   — fallback

SETUP — add these secrets in GitHub → Settings → Secrets → Actions:
  GEMINI_API_KEY   (get free at aistudio.google.com)   ← fixes everything
  GROQ_API_KEY     (existing)
  GROQ_API_KEY_2   (existing)
  GROQ_API_KEY_3   (existing)
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.memory import load_memory, build_context_prompt

# ── Constants ──────────────────────────────────────────────────────────────────
GEMINI_MODEL    = "gemini-2.5-flash-lite-preview-06-17"
GROQ_MODEL      = "llama-3.1-8b-instant"

WORDS_PER_SEC   = 2.5
INTER_CALL_S    = 4
TARGET_SHORT_S  = 58.0
COVER_S         = 3.0
WORDS_PER_SHORT = int((TARGET_SHORT_S - COVER_S) * WORDS_PER_SEC)   # ~137 words

# Groq: single request must stay under 6 000 TPM on free tier.
# ~1 token ≈ 0.75 words.  6000 tokens ≈ 4500 words — but Groq enforces this
# as a *per-minute* bucket, so one large request drains the whole minute.
# Keep prompt under ~3 000 tokens (≈2 250 words) so the bucket refills fast.
GROQ_MAX_PROMPT_WORDS = 2000   # hard cap on OCR text sent to Groq
GROQ_RETRIES          = 30     # rounds = retries // n_keys  → 30÷3 = 10 rounds
GROQ_MAX_WAIT         = 70     # seconds per sleep cycle


# ══════════════════════════════════════════════════════════════════════════════
# Gemini (primary)
# ══════════════════════════════════════════════════════════════════════════════

def build_gemini_client():
    """Return a configured Gemini model, or None with a clear reason printed."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("[Gemini] ⚠️  GEMINI_API_KEY not set — skipping (Groq fallback will be used)")
        print("[Gemini]    → Get a free key at https://aistudio.google.com and add it as a")
        print("[Gemini]      GitHub secret named GEMINI_API_KEY to avoid Groq rate limits.")
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        client = genai.GenerativeModel(GEMINI_MODEL)
        print(f"[Gemini] ✓ Client ready  model={GEMINI_MODEL}")
        return client
    except ImportError:
        print("[Gemini] ⚠️  google-generativeai not installed — skipping")
        print("[Gemini]    → Make sure requirements.txt contains: google-generativeai>=0.8.0")
        return None
    except Exception as e:
        print(f"[Gemini] ⚠️  Init failed: {e} — skipping")
        return None


def gemini_call(client, prompt: str, max_tokens: int = 600, retries: int = 5) -> str:
    """Call Gemini with retry/backoff on 429. Raises RuntimeError to trigger Groq fallback."""
    try:
        import google.generativeai as genai
        from google.api_core.exceptions import ResourceExhausted, GoogleAPIError
    except ImportError:
        raise RuntimeError("google-generativeai not installed")

    delay = 15
    for attempt in range(1, retries + 1):
        try:
            resp = client.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    max_output_tokens=max_tokens,
                    temperature=0.7,
                ),
            )
            return resp.text.strip()
        except ResourceExhausted as e:
            m    = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)", str(e))
            wait = int(m.group(1)) + 2 if m else delay
            print(f"  [Gemini] Rate limited (attempt {attempt}/{retries}) — sleeping {wait}s")
            time.sleep(wait)
            delay = min(delay * 2, 120)
        except Exception as e:
            raise RuntimeError(f"Gemini error: {e}")

    raise RuntimeError(f"Gemini failed after {retries} attempts")


# ══════════════════════════════════════════════════════════════════════════════
# Groq (fallback)
# ══════════════════════════════════════════════════════════════════════════════

def build_groq_clients() -> list:
    """Return Groq clients for all configured keys. Empty list = none set."""
    try:
        from groq import Groq
    except ImportError:
        print("[Groq] ⚠️  groq package not installed")
        return []

    keys = []
    for env in ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(2, 6)]:
        k = os.environ.get(env, "").strip()
        if k:
            keys.append(k)
    if not keys:
        return []

    from groq import Groq
    clients = [Groq(api_key=k) for k in keys]
    print(f"[Groq] {len(clients)} fallback key(s) loaded")
    return clients


def _trim_for_groq(text: str) -> str:
    """
    Groq free tier: 6 000 TPM.  One request uses (prompt_tokens + output_tokens).
    Keep the OCR body under GROQ_MAX_PROMPT_WORDS so the full prompt stays
    safely below the 6 000-token per-minute bucket.
    """
    words = text.split()
    if len(words) <= GROQ_MAX_PROMPT_WORDS:
        return text
    trimmed = " ".join(words[:GROQ_MAX_PROMPT_WORDS])
    print(f"  [Groq] OCR text trimmed {len(words)} → {GROQ_MAX_PROMPT_WORDS} words to fit TPM limit")
    return trimmed


def groq_call(clients: list, prompt: str, max_tokens: int = 500) -> str:
    """Key-rotation + sleep-after-full-rotation retry loop."""
    n      = len(clients)
    rounds = max(1, GROQ_RETRIES // n)
    delay  = 65

    for round_num in range(1, rounds + 1):
        for key_idx in range(n):
            client = clients[key_idx]
            label  = f"round {round_num}/{rounds}, key {key_idx + 1}/{n}"
            try:
                resp = client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                err = str(e)
                if "429" in err or "rate_limit" in err.lower():
                    if key_idx < n - 1:
                        print(f"  [Groq] Rate limited ({label}) — trying key {key_idx + 2}/{n}")
                    else:
                        m    = re.search(r"try again in (\d+(?:\.\d+)?)s", err)
                        wait = float(m.group(1)) + 3 if m else delay
                        wait = min(wait, GROQ_MAX_WAIT)
                        print(f"  [Groq] All {n} key(s) exhausted — sleeping {wait:.0f}s ({label})")
                        time.sleep(wait)
                        delay = min(delay * 2, GROQ_MAX_WAIT)
                elif "413" in err or "too large" in err.lower() or "6000" in err:
                    raise RuntimeError(f"Groq prompt too large even after trimming: {err}")
                else:
                    raise

    raise RuntimeError(
        f"Groq failed after {rounds} round(s) × {n} key(s). "
        "Add GEMINI_API_KEY secret to avoid this — get a free key at aistudio.google.com"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Unified call — Gemini first, Groq fallback
# ══════════════════════════════════════════════════════════════════════════════

def llm_call(gemini_client, groq_clients: list, prompt: str,
             max_tokens: int = 500, groq_prompt: str = None) -> str:
    """
    Try Gemini (full prompt, no trimming needed).
    If unavailable/failing, fall back to Groq with a word-trimmed prompt.
    groq_prompt: pass a pre-trimmed version for Groq; if None, trims prompt automatically.
    """
    # ── Gemini ────────────────────────────────────────────────────────────
    if gemini_client is not None:
        try:
            result = gemini_call(gemini_client, prompt, max_tokens=max_tokens)
            print("  [LLM] ✓ Gemini succeeded")
            return result
        except RuntimeError as e:
            print(f"  [LLM] Gemini failed ({e}) — falling back to Groq...")

    # ── Groq ──────────────────────────────────────────────────────────────
    if groq_clients:
        safe_prompt = groq_prompt if groq_prompt is not None else prompt
        result = groq_call(groq_clients, safe_prompt, max_tokens=max_tokens)
        print("  [LLM] ✓ Groq fallback succeeded")
        return result

    raise RuntimeError(
        "No LLM available. Set GEMINI_API_KEY (primary) and/or GROQ_API_KEY (fallback) "
        "in GitHub → Settings → Secrets → Actions."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Script generation helpers
# ══════════════════════════════════════════════════════════════════════════════

def _build_narration_prompt(pages, manhwa, episode, chapter, language,
                             memory_context, is_full, text_override=None):
    text = text_override or "\n\n".join(
        f"[Page {p['page_index']}]\n{p['raw_text']}"
        for p in pages if p.get("raw_text", "").strip()
    )
    ctx  = (f"chapter {chapter} full-episode chunk" if is_full
            else f"chapter {chapter} short (under 60s)")
    lang = f"Respond in {language}." if language != "en" else "Respond in English."
    return f"""{memory_context}You are a professional third-person manhwa narrator.
Your voice is like a documentary storyteller — calm, cinematic, immersive.

STRICT RULES:
- ALWAYS third-person: use character names and he/she/they — NEVER "I", "me", "we", "you"
- Describe what characters do, see, feel, and think from the OUTSIDE
- Reported thoughts: write "Lloyd wonders..." not "I wonder..."
- Add "..." only for genuine dramatic pauses — max 2 per narration
- SHORT: aim for {WORDS_PER_SHORT} words (do not cut short — use full budget), end on a hook
- FULL: continuous flowing narration, no cuts, no part labels
- Use memory context for continuity — do NOT re-introduce already-known characters
- Output ONLY the narration text. No labels, no preamble.

Manhwa: "{manhwa}" | Episode: {episode} | Chapter: {chapter} | Type: {ctx}
{lang}

PANEL TEXT:
{text}

NARRATION:"""


def narrate(gemini, groq_clients, pages, manhwa, episode, chapter, language,
            memory_context="", is_full=False):
    full_text = "\n\n".join(
        f"[Page {p['page_index']}]\n{p['raw_text']}"
        for p in pages if p.get("raw_text", "").strip()
    )
    if not full_text.strip():
        return {"narration": "", "estimated_duration": 0}

    # Full prompt for Gemini (no trimming — 250k TPM handles anything)
    gemini_prompt = _build_narration_prompt(
        pages, manhwa, episode, chapter, language, memory_context, is_full,
        text_override=full_text
    )
    # Trimmed prompt for Groq fallback (stays under 6k TPM)
    groq_text   = _trim_for_groq(full_text)
    groq_prompt = _build_narration_prompt(
        pages, manhwa, episode, chapter, language, memory_context, is_full,
        text_override=groq_text
    )

    narration = llm_call(gemini, groq_clients, gemini_prompt,
                         max_tokens=400, groq_prompt=groq_prompt)
    words = narration.split()
    return {
        "narration":          narration,
        "estimated_duration": round(len(words) / WORDS_PER_SEC, 1),
    }


def metadata(gemini, groq_clients, manhwa, episode, chapter, preview, language,
             memory_context="", is_full=False):
    video_type = "Full Episode" if is_full else f"Chapter {chapter} Short"
    title_hint = (f"Episode {episode} Full" if is_full
                  else f"Episode {episode} Ch{chapter}")

    prompt = f"""{memory_context}YouTube SEO expert. Generate metadata for a manhwa narration video.

Manhwa: "{manhwa}" | Episode: {episode} | Chapter: {chapter} | Type: {video_type} | Lang: {language}
Preview: {preview[:250]}

Rules:
- Title: max 70 chars, catchy, includes manhwa + episode info. Add #Shorts for shorts.
- Description: 150-200 words, strong opening hook, episode summary, CTA, 8 hashtags at end
- Tags: 20 tags — broad (manhwa, anime, webtoon), specific (title, episode), format tags

JSON only, no markdown:
{{"title":"...","description":"...","tags":[...]}}}}"""

    raw = llm_call(gemini, groq_clients, prompt, max_tokens=600)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "title":       f"{manhwa} {title_hint} | Manhwa {'#Shorts' if not is_full else ''}",
            "description": f"Watch {manhwa} Ep {episode} Ch {chapter}!\n\n#manhwa #anime #webtoon",
            "tags":        [manhwa, "manhwa", "anime", "webtoon", f"episode{episode}", "shorts"],
        }


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-text",  required=True)
    parser.add_argument("--manhwa",    required=True)
    parser.add_argument("--episode",   required=True, type=int)
    parser.add_argument("--chapter",   required=True, type=int)
    parser.add_argument("--language",  default="en")
    parser.add_argument("--output",    required=True)
    parser.add_argument("--manifest",  default="")
    parser.add_argument("--panels-per-short", default="auto")  # backward-compat
    parser.add_argument("--pages-per-short",  default="auto")  # backward-compat
    args = parser.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(args.raw_text, encoding="utf-8") as f:
        pages = json.load(f)

    # ── Series memory ──────────────────────────────────────────────────────
    memory         = load_memory(args.manhwa)
    memory_context = build_context_prompt(memory)
    if memory_context:
        print(f"[Script] Series memory loaded — continuing from ep {memory['episodes_done']}")
    else:
        print(f"[Script] No prior memory — this is episode 1")

    # ── Build clients ──────────────────────────────────────────────────────
    gemini       = build_gemini_client()
    groq_clients = build_groq_clients()

    if gemini is None and not groq_clients:
        raise EnvironmentError(
            "No API keys found.\n"
            "  Primary : set GEMINI_API_KEY  (free at aistudio.google.com)\n"
            "  Fallback: set GROQ_API_KEY    (free at console.groq.com)\n"
            "Add them in GitHub → Settings → Secrets → Actions."
        )

    print(f"[Script] Chapter {args.chapter}: {len(pages)} pages → 1 short")
    print(f"[Script] Provider: {'Gemini (primary)' if gemini else 'Groq only'}"
          f"{' + Groq fallback' if gemini and groq_clients else ''}")
    print(f"[Script] Inter-call delay: {INTER_CALL_S}s")

    # ── Short narration ────────────────────────────────────────────────────
    print(f"\n[Script] Generating short narration for chapter {args.chapter}...")
    nar = narrate(gemini, groq_clients, pages, args.manhwa, args.episode,
                  args.chapter, args.language, memory_context, is_full=False)
    time.sleep(INTER_CALL_S)

    # ── Metadata ───────────────────────────────────────────────────────────
    print(f"[Script] Generating metadata for chapter {args.chapter}...")
    meta = metadata(gemini, groq_clients, args.manhwa, args.episode, args.chapter,
                    nar["narration"], args.language, memory_context, is_full=False)
    time.sleep(INTER_CALL_S)

    # ── Full episode chunk ─────────────────────────────────────────────────
    print(f"[Script] Generating full-episode chunk for chapter {args.chapter}...")
    full_nar = narrate(gemini, groq_clients, pages, args.manhwa, args.episode,
                       args.chapter, args.language, memory_context, is_full=True)
    time.sleep(INTER_CALL_S)

    result = {
        "manhwa":   args.manhwa,
        "episode":  args.episode,
        "chapter":  args.chapter,
        "language": args.language,
        "shorts": [{
            "part":               1,
            "page_start":         pages[0]["page_index"] if pages else 1,
            "page_end":           pages[-1]["page_index"] if pages else 1,
            "pages":              [p["page_index"] for p in pages],
            "narration":          nar["narration"],
            "estimated_duration": nar["estimated_duration"],
            "metadata":           meta,
        }],
        "full_episode_chunk": {
            "narration":          full_nar["narration"],
            "estimated_duration": full_nar["estimated_duration"],
        },
    }

    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n[Script] ✅ Chapter {args.chapter}: 1 short + full-ep chunk → {out}")


if __name__ == "__main__":
    main()