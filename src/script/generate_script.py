"""
generate_script.py
Generates narration scripts for ONE chapter:
  - 1 short script  (entire chapter = 1 YouTube Short)
  - 1 full_episode narration chunk (combined later across all chapters)

The full episode is assembled by build_full_episode.py after all chapters
are processed — this script just contributes this chapter's chunk to it.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from groq import Groq

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.memory import load_memory, save_memory, build_context_prompt, update_memory

GROQ_MODEL      = "llama-3.1-8b-instant"
WORDS_PER_SEC   = 2.5
INTER_CALL_S    = 12
TARGET_SHORT_S  = 55.0
COVER_S         = 3.0
WORDS_PER_SHORT = int((TARGET_SHORT_S - COVER_S) * WORDS_PER_SEC)   # ~130 words


def build_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set")
    return Groq(api_key=api_key)


def groq_call(client: Groq, prompt: str, max_tokens: int = 500, retries: int = 6) -> str:
    delay = INTER_CALL_S
    for attempt in range(1, retries + 1):
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
                m = re.search(r"try again in (\d+(?:\.\d+)?)s", err)
                wait = float(m.group(1)) + 2 if m else delay
                print(f"  [Groq] Rate limited — waiting {wait:.1f}s (attempt {attempt}/{retries})")
                time.sleep(wait)
                delay = min(delay * 2, 90)
            elif "413" in err or "too large" in err.lower():
                raise RuntimeError(f"Prompt too large: {err}")
            else:
                raise
    raise RuntimeError(f"Groq failed after {retries} retries")


def narrate(client, pages, manhwa, episode, chapter, language, memory_context="", is_full=False):
    text = "\n\n".join(
        f"[Page {p['page_index']}]\n{p['raw_text']}"
        for p in pages if p.get("raw_text", "").strip()
    )
    if not text.strip():
        return {"narration": "", "estimated_duration": 0}

    ctx  = f"chapter {chapter} full-episode chunk" if is_full else f"chapter {chapter} short (under 60s)"
    lang = f"Respond in {language}." if language != "en" else "Respond in English."

    prompt = f"""{memory_context}You are a professional third-person manhwa narrator.
Your voice is like a documentary storyteller — calm, cinematic, immersive.

STRICT RULES:
- ALWAYS third-person: use character names and he/she/they — NEVER "I", "me", "we", "you"
- Describe what characters do, see, feel, and think from the OUTSIDE
- Reported thoughts: write "Lloyd wonders..." not "I wonder..."
- Add "..." only for genuine dramatic pauses — max 2 per narration
- SHORT: max {WORDS_PER_SHORT} words, end on a hook that makes viewers want the next chapter
- FULL: continuous flowing narration, no cuts, no part labels
- Use memory context for continuity — do NOT re-introduce characters already known
- Output ONLY the narration text. No labels, no preamble.

Manhwa: "{manhwa}" | Episode: {episode} | Chapter: {chapter} | Type: {ctx}
{lang}

PANEL TEXT:
{text}

NARRATION:"""

    narration = groq_call(client, prompt, max_tokens=400)
    words     = narration.split()
    return {
        "narration":          narration,
        "estimated_duration": round(len(words) / WORDS_PER_SEC, 1),
    }


def metadata(client, manhwa, episode, chapter, preview, language, memory_context="", is_full=False):
    if is_full:
        video_type = "Full Episode"
        title_hint = f"Episode {episode} Full"
    else:
        video_type = f"Chapter {chapter} Short"
        title_hint = f"Episode {episode} Ch{chapter}"

    prompt = f"""{memory_context}YouTube SEO expert. Generate metadata for a manhwa narration video.

Manhwa: "{manhwa}" | Episode: {episode} | Chapter: {chapter} | Type: {video_type} | Lang: {language}
Preview: {preview[:250]}

Rules:
- Title: max 70 chars, catchy, includes manhwa + episode info. Add #Shorts for shorts.
- Description: 150-200 words, strong opening hook, episode summary, CTA, 8 hashtags at end
- Tags: 20 tags — broad (manhwa, anime, webtoon), specific (title, episode), format tags

JSON only, no markdown:
{{"title":"...","description":"...","tags":[...]}}}"""

    raw = groq_call(client, prompt, max_tokens=600)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "title":       f"{manhwa} {title_hint} | Manhwa {'#Shorts' if not is_full else ''}",
            "description": f"Watch {manhwa} Ep {episode} Ch {chapter}!\n\n#manhwa #anime #webtoon",
            "tags":        [manhwa, "manhwa", "anime", "webtoon", f"episode{episode}", "shorts"],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-text",  required=True)
    parser.add_argument("--manhwa",    required=True)
    parser.add_argument("--episode",   required=True, type=int)
    parser.add_argument("--chapter",   required=True, type=int)
    parser.add_argument("--language",  default="en")
    parser.add_argument("--output",    required=True)
    parser.add_argument("--manifest",  default="")
    # kept for backward-compat
    parser.add_argument("--panels-per-short", default="auto")
    parser.add_argument("--pages-per-short",  default="auto")
    args = parser.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(args.raw_text, encoding="utf-8") as f:
        pages = json.load(f)

    # ── Load series memory ─────────────────────────────────────────────────
    # Only chapter 1 loads/updates memory — others just read it for context
    memory         = load_memory(args.manhwa)
    memory_context = build_context_prompt(memory)
    if memory_context:
        print(f"[Script] Series memory loaded — continuing from ep {memory['episodes_done']}")
    else:
        print(f"[Script] No prior memory — this is episode 1")

    client = build_client()

    print(f"[Script] Chapter {args.chapter}: {len(pages)} pages → 1 short")
    print(f"[Script] Narrator: strict third-person | Inter-call delay: {INTER_CALL_S}s")

    # ── Short (1 per chapter — all pages of this chapter) ─────────────────
    print(f"\n[Script] Generating short narration for chapter {args.chapter}...")
    nar  = narrate(client, pages, args.manhwa, args.episode, args.chapter,
                   args.language, memory_context, is_full=False)
    time.sleep(INTER_CALL_S)
    meta = metadata(client, args.manhwa, args.episode, args.chapter,
                    nar["narration"], args.language, memory_context, is_full=False)
    time.sleep(INTER_CALL_S)

    # ── Full episode chunk (this chapter's contribution to the full video) ─
    print(f"[Script] Generating full-episode narration chunk for chapter {args.chapter}...")
    full_nar  = narrate(client, pages, args.manhwa, args.episode, args.chapter,
                        args.language, memory_context, is_full=True)
    time.sleep(INTER_CALL_S)

    # Series memory is updated in the final build step after all chapters
    # are processed, so the full episode narration can be used for context.

    result = {
        "manhwa":       args.manhwa,
        "episode":      args.episode,
        "chapter":      args.chapter,
        "language":     args.language,
        # part=1 always — each chapter is exactly 1 short
        "shorts": [{
            "part":               1,
            "page_start":         pages[0]["page_index"] if pages else 1,
            "page_end":           pages[-1]["page_index"] if pages else 1,
            "pages":              [p["page_index"] for p in pages],
            "narration":          nar["narration"],
            "estimated_duration": nar["estimated_duration"],
            "metadata":           meta,
        }],
        # This chapter's narration chunk for the combined full episode
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