"""
memory.py
Manages per-series narrative memory across episodes.
Each manhwa gets its own JSON file under series_memory/
Files are committed back to the repo after each episode run.

Memory file structure:
{
  "manhwa": "Solo Leveling",
  "episodes_done": [1, 2, 3],
  "characters": ["Sung Jin-Woo", "Cha Hae-In"],
  "story_so_far": "150-word max summary of the full series so far",
  "last_hook": "The cliffhanger ending of the most recent episode"
}
"""

import json
import re
from pathlib import Path


MEMORY_DIR = Path("series_memory")


def manhwa_slug(manhwa: str) -> str:
    """Convert manhwa name to a safe filename slug."""
    slug = manhwa.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug.strip("_")


def memory_path(manhwa: str) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return MEMORY_DIR / f"{manhwa_slug(manhwa)}.json"


def load_memory(manhwa: str) -> dict:
    """Load existing memory or return a fresh empty memory."""
    path = memory_path(manhwa)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        print(f"[Memory] Loaded memory for '{manhwa}' — {len(data.get('episodes_done', []))} episode(s) done")
        return data
    print(f"[Memory] No existing memory for '{manhwa}' — starting fresh")
    return {
        "manhwa": manhwa,
        "episodes_done": [],
        "characters": [],
        "story_so_far": "",
        "last_hook": "",
    }


def save_memory(manhwa: str, memory: dict) -> None:
    """Save updated memory to disk."""
    path = memory_path(manhwa)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)
    print(f"[Memory] Saved memory for '{manhwa}' → {path}")


def build_context_prompt(memory: dict) -> str:
    """Build a context block to prepend to Groq narration prompts."""
    if not memory["episodes_done"]:
        return ""

    parts = [f'SERIES CONTEXT for "{memory["manhwa"]}":']

    if memory["story_so_far"]:
        parts.append(f"Story so far: {memory['story_so_far']}")

    if memory["characters"]:
        chars = ", ".join(memory["characters"][:10])
        parts.append(f"Characters introduced: {chars}")

    if memory["last_hook"]:
        parts.append(f"Previous episode ended with: {memory['last_hook']}")

    parts.append("Use this context for continuity. Don't re-introduce known characters.\n")
    return "\n".join(parts)


def update_memory(memory: dict, episode: int, narration: str, client, groq_call_fn) -> dict:
    """
    Ask Groq to extract new info from this episode's narration
    and merge it into the existing memory.
    """
    existing_chars = ", ".join(memory["characters"]) if memory["characters"] else "none yet"
    existing_story = memory["story_so_far"] or "This is the first episode."

    prompt = f"""You are a series continuity tracker for the manhwa "{memory['manhwa']}".

EXISTING MEMORY:
Story so far: {existing_story}
Known characters: {existing_chars}

NEW EPISODE {episode} NARRATION:
{narration[:1500]}

Your job:
1. Update "story_so_far" — a single paragraph max 150 words covering the ENTIRE series including this episode
2. Update "characters" — full list of named characters seen across ALL episodes
3. Extract "last_hook" — the cliffhanger or key moment this episode ended on (1-2 sentences)

Return ONLY valid JSON, no markdown:
{{"story_so_far":"...","characters":["name1","name2",...],"last_hook":"..."}}"""

    try:
        raw = groq_call_fn(client, prompt, max_tokens=400)
        raw = raw.replace("```json", "").replace("```", "").strip()
        updates = json.loads(raw)

        memory["story_so_far"] = updates.get("story_so_far", memory["story_so_far"])
        memory["characters"]   = updates.get("characters",   memory["characters"])
        memory["last_hook"]    = updates.get("last_hook",    memory["last_hook"])

        if episode not in memory["episodes_done"]:
            memory["episodes_done"].append(episode)
            memory["episodes_done"].sort()

        print(f"[Memory] Updated — {len(memory['characters'])} characters, ep {memory['episodes_done']}")
    except Exception as e:
        print(f"[Memory] ⚠️  Could not update memory: {e} — keeping existing")

    return memory