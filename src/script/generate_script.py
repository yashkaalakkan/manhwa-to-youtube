"""
generate_script.py
Generates narration scripts and SEO metadata via Groq.
Always produces exactly 4 shorts per episode (pages split evenly).
Loads per-series memory for narrative continuity across episodes.
Updates and saves memory after generation.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List

from groq import Groq

# Add src to path so memory.py is importable
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.memory import load_memory, save_memory, build_context_prompt, update_memory

GROQ_MODEL    = "llama-3.1-8b-instant"
WORDS_PER_SEC = 2.5
INTER_CALL_S  = 12
NUM_SHORTS    = 4


def build_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set")
    return Groq(api_key=api_key)


def split_evenly(pages: List[dict], n: int) -> List[List[dict]]:
    size   = max(len(pages) // n, 1)
    chunks = []
    for i in range(n):
        start = i * size
        end   = start + size if i < n - 1 else len(pages)
        chunk = pages[start:end]
        if chunk:
            chunks.append(chunk)
    return chunks


def groq_call(client: Groq, prompt: str, max_tokens: int = 500, retries: int = 6) -> str:
    """Groq call with exponential backoff on rate limit."""
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


def narrate(client, pages, manhwa, episode, part, language, memory_context="", is_full=False):
    text = "\n\n".join(
        f"[Page {p['page_index']}]\n{p['raw_text']}"
        for p in pages if p.get("raw_text", "").strip()
    )
    if not text.strip():
        return {"narration": "", "estimated_duration": 0}

    ctx  = "full episode" if is_full else f"part {part} of 4 short (under 60s)"
    lang = f"Respond in {language}." if language != "en" else "Respond in English."

    prompt = f"""{memory_context}You are a professional manhwa narrator.

Manhwa: "{manhwa}" | Episode: {episode} | Type: {ctx}
{lang}

PANEL TEXT:
{text}

Rules:
- Smooth, natural spoken narration only
- Add "..." for dramatic pauses
- SHORT: end with a cliffhanger hook, max 130 words
- FULL: continuous narration, no cuts
- Use series context above for continuity — don't re-introduce known characters
- Output ONLY the narration. No labels.

NARRATION:"""

    narration = groq_call(client, prompt, max_tokens=400)
    words     = narration.split()
    return {
        "narration": narration,
        "estimated_duration": round(len(words) / WORDS_PER_SEC, 1),
    }


def metadata(client, manhwa, episode, part, preview, language, memory_context=""):
    video_type = f"Part {part} of 4 (Short)" if part else "Full Episode"
    prompt = f"""{memory_context}YouTube SEO expert. Generate metadata for a manhwa narration video.

Manhwa: "{manhwa}" | Episode: {episode} | Type: {video_type} | Lang: {language}
Preview: {preview[:250]}

Rules:
- Title: max 70 chars, catchy, includes manhwa + episode. Add #Shorts for shorts.
- Description: 150-200 words, strong opening hook, episode summary, CTA, 8 hashtags at end
- Tags: 20 tags — broad (manhwa, anime, webtoon), specific (title, episode), format tags

JSON only, no markdown:
{{"title":"...","description":"...","tags":[...]}}"""

    raw = groq_call(client, prompt, max_tokens=600)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pt = f" Part {part}" if part else ""
        return {
            "title": f"{manhwa} Episode {episode}{pt} | Manhwa #Shorts",
            "description": f"Watch {manhwa} Ep {episode}{pt}!\n\n#manhwa #anime #webtoon",
            "tags": [manhwa, "manhwa", "anime", "webtoon", f"episode{episode}", "shorts"],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-text",        required=True)
    parser.add_argument("--manhwa",          required=True)
    parser.add_argument("--episode",         required=True, type=int)
    parser.add_argument("--language",        default="en")
    parser.add_argument("--pages-per-short", default="auto", help="Ignored — always 4 parts")
    parser.add_argument("--output",          required=True)
    args = parser.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(args.raw_text, encoding="utf-8") as f:
        pages = json.load(f)

    # ── Load series memory ─────────────────────────────────────────────────
    memory          = load_memory(args.manhwa)
    memory_context  = build_context_prompt(memory)
    if memory_context:
        print(f"[Script] Series memory loaded — continuing from ep {memory['episodes_done']}")
    else:
        print(f"[Script] No prior memory — this is episode 1")

    client = build_client()
    chunks = split_evenly(pages, NUM_SHORTS)
    print(f"[Script] {len(pages)} pages → {len(chunks)} shorts")
    print(f"[Script] Inter-call delay: {INTER_CALL_S}s")

    result = {
        "manhwa":       args.manhwa,
        "episode":      args.episode,
        "language":     args.language,
        "total_parts":  len(chunks),
        "shorts":       [],
        "full_episode": None,
    }

    # ── Generate 4 shorts ─────────────────────────────────────────────────
    for i, chunk in enumerate(chunks, start=1):
        print(f"\n[Script] Short {i}/{len(chunks)}: pages {chunk[0]['page_index']}–{chunk[-1]['page_index']}")
        nar = narrate(client, chunk, args.manhwa, args.episode, i, args.language, memory_context)
        time.sleep(INTER_CALL_S)
        meta = metadata(client, args.manhwa, args.episode, i, nar["narration"], args.language, memory_context)
        time.sleep(INTER_CALL_S)
        result["shorts"].append({
            "part":               i,
            "page_start":         chunk[0]["page_index"],
            "page_end":           chunk[-1]["page_index"],
            "pages":              [p["page_index"] for p in chunk],
            "narration":          nar["narration"],
            "estimated_duration": nar["estimated_duration"],
            "metadata":           meta,
        })

    # ── Full episode ───────────────────────────────────────────────────────
    print(f"\n[Script] Full episode narration (chunked)...")
    full_chunks = [pages[i:i+5] for i in range(0, len(pages), 5)]
    full_parts  = []
    for ci, chunk in enumerate(full_chunks, start=1):
        print(f"[Script]   Chunk {ci}/{len(full_chunks)}...")
        nd = narrate(client, chunk, args.manhwa, args.episode, 0,
                     args.language, memory_context, is_full=True)
        if nd["narration"]:
            full_parts.append(nd["narration"])
        time.sleep(INTER_CALL_S)

    combined  = " ".join(full_parts)
    full_meta = metadata(client, args.manhwa, args.episode, None,
                         combined[:250], args.language, memory_context)
    time.sleep(INTER_CALL_S)

    result["full_episode"] = {
        "narration":          combined,
        "estimated_duration": round(len(combined.split()) / WORDS_PER_SEC, 1),
        "metadata":           full_meta,
    }

    # ── Update and save memory ─────────────────────────────────────────────
    print(f"\n[Script] Updating series memory...")
    memory = update_memory(memory, args.episode, combined, client, groq_call)
    time.sleep(INTER_CALL_S)
    save_memory(args.manhwa, memory)

    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n[Script] ✅ {len(chunks)} shorts + full episode → {out}")


if __name__ == "__main__":
    main()