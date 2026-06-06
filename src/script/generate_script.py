"""
generate_script.py
Generates narration scripts and SEO metadata via Groq.
Narration is strict THIRD-PERSON — an external narrator describes the story.
Loads per-series memory for narrative continuity across episodes.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import List

from groq import Groq

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.memory import load_memory, save_memory, build_context_prompt, update_memory

GROQ_MODEL       = "llama-3.1-8b-instant"
WORDS_PER_SEC    = 2.5
INTER_CALL_S     = 12
TARGET_SHORT_S   = 55.0   # must match build_shorts.py
COVER_S          = 3.0
MIN_PANEL_S      = 1.5    # must match build_shorts.py
WORDS_PER_SHORT  = int((TARGET_SHORT_S - COVER_S) * WORDS_PER_SEC)   # ~130 words


def panels_per_short_dynamic() -> int:
    return max(1, int((TARGET_SHORT_S - COVER_S) / MIN_PANEL_S))


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

    ctx  = "full episode" if is_full else f"part {part} short (under 60s)"
    lang = f"Respond in {language}." if language != "en" else "Respond in English."

    # ── THIRD-PERSON NARRATOR RULES ─────────────────────────────────────────
    # The narrator is an omniscient outside observer — never "I" / "we" / "you".
    # Describes characters by name or "he/she/they".
    # Shares character thoughts in reported speech: "Lloyd wonders if..." not "I wonder..."
    # Example style: "Lloyd tries to recall how he arrived here, but his memory
    #   is a blank. Then a sound — footsteps. He turns to find a stranger watching
    #   him, someone who somehow knows his name..."
    prompt = f"""{memory_context}You are a professional third-person manhwa narrator.
Your voice is like a documentary storyteller — calm, cinematic, immersive.

STRICT RULES:
- ALWAYS third-person: use character names and he/she/they — NEVER "I", "me", "we", "you"
- Describe what characters do, see, feel, and think from the OUTSIDE
- Reported thoughts: write "Lloyd wonders..." not "I wonder..."
- Add "..." only for genuine dramatic pauses — max 2 per narration
- SHORT: max {WORDS_PER_SHORT} words, end on a hook that makes viewers want part 2
- FULL: continuous flowing narration, no cuts, no part labels
- Use memory context for continuity — do NOT re-introduce characters already known
- Output ONLY the narration text. No labels, no preamble.

Manhwa: "{manhwa}" | Episode: {episode} | Type: {ctx}
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


def metadata(client, manhwa, episode, part, preview, language, memory_context=""):
    video_type = f"Part {part} Short" if part else "Full Episode"
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
            "title":       f"{manhwa} Episode {episode}{pt} | Manhwa #Shorts",
            "description": f"Watch {manhwa} Ep {episode}{pt}!\n\n#manhwa #anime #webtoon",
            "tags":        [manhwa, "manhwa", "anime", "webtoon", f"episode{episode}", "shorts"],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-text",         required=True)
    parser.add_argument("--manhwa",           required=True)
    parser.add_argument("--episode",          required=True, type=int)
    parser.add_argument("--language",         default="en")
    parser.add_argument("--panels-per-short", default="auto")
    parser.add_argument("--pages-per-short",  default="auto")
    parser.add_argument("--output",           required=True)
    args = parser.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(args.raw_text, encoding="utf-8") as f:
        pages = json.load(f)

    # ── Load series memory ─────────────────────────────────────────────────
    memory         = load_memory(args.manhwa)
    memory_context = build_context_prompt(memory)
    if memory_context:
        print(f"[Script] Series memory loaded — continuing from ep {memory['episodes_done']}")
    else:
        print(f"[Script] No prior memory — this is episode 1")

    client = build_client()

    # Mirror build_shorts.py manifest logic exactly — same panel count, same shorts count
    manifest_path = Path("pipeline/manifest.json")
    if manifest_path.exists():
        with open(manifest_path) as mf:
            mfdata = json.load(mf)
        if mfdata.get("use_panels") and mfdata.get("panels"):
            n_panels = len(mfdata["panels"])
        else:
            skip     = set(mfdata.get("skip_pages", []))
            n_panels = len([p for p in mfdata.get("pages", []) if p["index"] not in skip])
    else:
        n_panels = len(pages)

    pps        = panels_per_short_dynamic()
    num_shorts = max(1, math.ceil(n_panels / pps))
    chunks     = split_evenly(pages, num_shorts)
    print(f"[Script] {len(pages)} pages | {n_panels} panels → {num_shorts} shorts (~{pps} panels/short)")
    print(f"[Script] Narrator: strict third-person | Inter-call delay: {INTER_CALL_S}s")

    result = {
        "manhwa":      args.manhwa,
        "episode":     args.episode,
        "language":    args.language,
        "total_parts": len(chunks),
        "shorts":      [],
        "full_episode": None,
    }

    # ── Shorts ────────────────────────────────────────────────────────────
    for i, chunk in enumerate(chunks, start=1):
        print(f"\n[Script] Short {i}/{len(chunks)}: pages {chunk[0]['page_index']}–{chunk[-1]['page_index']}")
        nar  = narrate(client, chunk, args.manhwa, args.episode, i, args.language, memory_context)
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

    # Normalise join — no double-spaces, no blank chunks
    combined = " ".join(part.strip() for part in full_parts if part.strip())
    combined = re.sub(r" {2,}", " ", combined).strip()

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