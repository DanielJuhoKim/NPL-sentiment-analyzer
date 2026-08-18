"""Separa os posts de um JSON bruto do Reddit (listing t3) em <nome>-posts.{json,csv}.

Uso: python3 extract_posts.py <arquivo.json> [...]
"""

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "subreddit",
    "id",
    "created_utc",
    "created_iso",
    "author",
    "title",
    "selftext",
    "flair",
    "score",
    "ups",
    "upvote_ratio",
    "num_comments",
    "is_self",
    "over_18",
    "spoiler",
    "stickied",
    "domain",
    "url",
    "permalink",
]


def to_row(post: dict) -> dict:
    created = post.get("created_utc")
    return {
        "subreddit": post.get("subreddit"),
        "id": post.get("id"),
        "created_utc": created,
        "created_iso": (
            datetime.fromtimestamp(created, timezone.utc).isoformat()
            if created
            else None
        ),
        "author": post.get("author"),
        "title": post.get("title", ""),
        "selftext": post.get("selftext", ""),
        "flair": post.get("link_flair_text"),
        "score": post.get("score"),
        "ups": post.get("ups"),
        "upvote_ratio": post.get("upvote_ratio"),
        "num_comments": post.get("num_comments"),
        "is_self": post.get("is_self"),
        "over_18": post.get("over_18"),
        "spoiler": post.get("spoiler"),
        "stickied": post.get("stickied"),
        "domain": post.get("domain"),
        "url": post.get("url"),
        "permalink": "https://www.reddit.com" + post.get("permalink", ""),
    }


def extract(raw: Path) -> None:
    listing = json.loads(raw.read_text(encoding="utf-8"))
    children = listing["data"]["children"]

    rows = [to_row(c["data"]) for c in children if c.get("kind") == "t3"]
    rows.sort(key=lambda r: r["created_utc"] or 0, reverse=True)

    stem = raw.stem.removesuffix("-posts")
    out_json = raw.with_name(f"{stem}-posts.json")
    out_csv = raw.with_name(f"{stem}-posts.csv")

    out_json.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with_text = [r for r in rows if r["selftext"].strip()]
    chars = sum(len(r["selftext"]) for r in with_text)
    print(f"{raw.name}: {len(rows)} posts -> {out_json.name}, {out_csv.name}")
    print(
        f"  com selftext: {len(with_text)} | so titulo/link: {len(rows) - len(with_text)}"
        f" | {chars:,} chars de corpo"
    )
    print(f"  after (proxima pagina): {listing['data'].get('after')}")


def main() -> None:
    paths = [Path(a) for a in sys.argv[1:]]
    if not paths:
        sys.exit(f"uso: python3 {Path(__file__).name} <arquivo.json> [...]")
    for p in paths:
        extract(p)


if __name__ == "__main__":
    main()
