"""
merge_scripts.py
Merges per-chapter scripts.json files into a single pipeline/scripts.json
that the upload workflow expects. Called by generate.yml after video build.
"""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manhwa",   required=True)
    parser.add_argument("--episode",  required=True, type=int)
    parser.add_argument("--chapters", required=True, type=int)
    parser.add_argument("--output",   required=True)
    args = parser.parse_args()

    combined = {
        "manhwa":  args.manhwa,
        "episode": args.episode,
        "shorts":  [],
        "full_episode": {
            "narration": "",
            "metadata": {
                "title":       f"{args.manhwa} Episode {args.episode} | Full Episode",
                "description": (
                    f"Watch {args.manhwa} Episode {args.episode} — Full Episode!\n\n"
                    "#manhwa #anime #webtoon"
                ),
                "tags": [
                    args.manhwa, "manhwa", "anime", "webtoon",
                    f"episode{args.episode}", "full episode",
                ],
            },
        },
    }

    narration_parts = []
    for ch in range(1, args.chapters + 1):
        p = Path(f"pipeline/ch{ch}/scripts.json")
        if not p.exists():
            print(f"[MergeScripts] ⚠️  pipeline/ch{ch}/scripts.json not found — skipping")
            continue
        with open(p, encoding="utf-8") as f:
            ch_scripts = json.load(f)

        for s in ch_scripts.get("shorts", []):
            combined["shorts"].append({**s, "chapter": ch})

        chunk = ch_scripts.get("full_episode_chunk", {})
        if chunk.get("narration"):
            narration_parts.append(chunk["narration"])

    combined["full_episode"]["narration"] = " ".join(narration_parts)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    print(
        f"[MergeScripts] ✅ {out} written — "
        f"{args.chapters} chapters, {len(combined['shorts'])} shorts"
    )


if __name__ == "__main__":
    main()