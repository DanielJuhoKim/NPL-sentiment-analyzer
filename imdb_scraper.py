#!/usr/bin/env python3
"""
Scraper de reviews do IMDb.

O www.imdb.com bloqueia requisicoes automatizadas (responde 202 com corpo vazio),
entao este scraper usa o mesmo endpoint GraphQL que o proprio site consome
(caching.graphql.imdb.com), paginando pelo cursor ate atingir a meta de reviews.

Uso:
    python3 imdb_scraper.py                          # 1000 reviews de Spider-Man: Brand New Day
    python3 imdb_scraper.py --limit 500
    python3 imdb_scraper.py --title "Interstellar"   # busca o ID pelo nome
    python3 imdb_scraper.py --id tt22084616 --sort SUBMISSION_DATE

Sem dependencias externas: so a stdlib.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPHQL_URL = "https://caching.graphql.imdb.com/"
SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/x/{}.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

REVIEWS_QUERY = """
query TitleReviews($id: ID!, $first: Int!, $after: ID, $sort: ReviewsSortBy!, $order: SortOrder!) {
  title(id: $id) {
    titleText { text }
    releaseYear { year }
    ratingsSummary { aggregateRating voteCount }
    reviews(first: $first, after: $after, sort: { by: $sort, order: $order }) {
      total
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          summary { originalText }
          text { originalText { plainText } }
          authorRating
          submissionDate
          spoiler
          helpfulness { upVotes downVotes }
          author { nickName userId }
        }
      }
    }
  }
}
"""

SORT_OPTIONS = (
    "HELPFULNESS_SCORE",
    "SUBMISSION_DATE",
    "SUBMITTER_REVIEW_COUNT",
    "TOTAL_VOTES",
    "USER_RATING",
)


def _http_json(url: str, payload: dict | None = None, retries: int = 5) -> dict:
    """POST/GET com retry e backoff exponencial. Devolve o JSON decodificado."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "x-imdb-client-name": "imdb-web-next",
        "Origin": "https://www.imdb.com",
        "Referer": "https://www.imdb.com/",
    }

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            # 4xx que nao seja rate-limit nao melhora com retry
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                break
            wait = 2**attempt
            print(f"  ! tentativa {attempt + 1}/{retries} falhou ({exc}); aguardando {wait}s", file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"requisicao falhou apos {retries} tentativas: {last_error}")


def find_title_id(query: str, limit: int = 6) -> tuple[str, str]:
    """Resolve um nome de filme para (imdb_id, titulo) usando a API de sugestoes.

    O ranking do IMDb e por popularidade atual, nao por match exato: "Dune" devolve
    Dune: Part Two e "Avatar" devolve Avatar Aang. Por isso os outros candidatos sao
    impressos — se o escolhido estiver errado, rode de novo passando --id.
    """
    url = SUGGESTION_URL.format(urllib.parse.quote(query))
    payload = _http_json(url)

    candidatos = [
        item
        for item in payload.get("d", [])
        # entradas editoriais nao tem id comecando com "tt"
        if str(item.get("id", "")).startswith("tt")
    ][:limit]

    if not candidatos:
        raise SystemExit(f"nenhum titulo encontrado para {query!r}")

    escolhido, *outros = candidatos
    if outros:
        print("> outros candidatos (use --id se o escolhido estiver errado):")
        for c in outros:
            print(f"    {c['id']}  {c.get('l')} ({c.get('y')}) [{c.get('q')}]")

    return escolhido["id"], escolhido.get("l", query)


def sentiment_from_rating(rating: int | None) -> str:
    """Rotulo derivado da nota do autor (1-10), para uso como label de treino."""
    if rating is None:
        return "unknown"
    if rating <= 4:
        return "negative"
    if rating <= 6:
        return "neutral"
    return "positive"


def fetch_reviews(title_id: str, limit: int, sort: str, order: str, page_size: int, delay: float) -> tuple[list[dict], dict]:
    """Pagina o GraphQL ate juntar `limit` reviews (ou acabar a lista)."""
    reviews: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = None
    meta: dict = {}
    page = 0

    while len(reviews) < limit:
        page += 1
        variables = {
            "id": title_id,
            "first": min(page_size, limit - len(reviews)),
            "sort": sort,
            "order": order,
        }
        if cursor:
            variables["after"] = cursor

        payload = _http_json(GRAPHQL_URL, {"query": REVIEWS_QUERY, "variables": variables})

        if "errors" in payload:
            raise RuntimeError(f"GraphQL retornou erros: {json.dumps(payload['errors'], indent=2)}")

        title = (payload.get("data") or {}).get("title")
        if not title:
            raise RuntimeError(f"titulo {title_id} nao encontrado na resposta")

        block = title["reviews"]
        if not meta:
            meta = {
                "imdb_id": title_id,
                "title": title["titleText"]["text"],
                "year": (title.get("releaseYear") or {}).get("year"),
                "imdb_rating": (title.get("ratingsSummary") or {}).get("aggregateRating"),
                "imdb_votes": (title.get("ratingsSummary") or {}).get("voteCount"),
                "reviews_available": block["total"],
            }
            print(f"> {meta['title']} ({meta['year']}) — {block['total']} reviews disponiveis")

        for edge in block["edges"]:
            node = edge["node"]
            if node["id"] in seen:
                continue
            seen.add(node["id"])

            rating = node.get("authorRating")
            reviews.append(
                {
                    "review_id": node["id"],
                    "title": (node.get("summary") or {}).get("originalText") or "",
                    "review": ((node.get("text") or {}).get("originalText") or {}).get("plainText") or "",
                    "rating": rating,
                    "sentiment": sentiment_from_rating(rating),
                    "date": node.get("submissionDate"),
                    "author": (node.get("author") or {}).get("nickName"),
                    "author_id": (node.get("author") or {}).get("userId"),
                    "spoiler": node.get("spoiler"),
                    "helpful_up": (node.get("helpfulness") or {}).get("upVotes"),
                    "helpful_down": (node.get("helpfulness") or {}).get("downVotes"),
                    "url": f"https://www.imdb.com/review/{node['id']}/",
                }
            )

        print(f"  pagina {page}: {len(reviews)}/{limit} reviews coletados")

        info = block["pageInfo"]
        if not info["hasNextPage"]:
            print("  fim da lista de reviews do IMDb")
            break
        cursor = info["endCursor"]
        time.sleep(delay)

    return reviews[:limit], meta


def save(reviews: list[dict], meta: dict, prefix: str) -> None:
    csv_path, json_path = f"{prefix}.csv", f"{prefix}.json"

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(reviews[0].keys()), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(reviews)

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "count": len(reviews), "reviews": reviews}, fh, ensure_ascii=False, indent=2)

    print(f"\n> salvo: {csv_path} e {json_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta reviews de um filme no IMDb.")
    parser.add_argument("--id", default=None, help="IMDb ID (ex: tt22084616)")
    parser.add_argument("--title", default="Spider-Man: Brand New Day", help="nome do filme, se --id nao for passado")
    parser.add_argument("--limit", type=int, default=1000, help="quantidade de reviews (default: 1000)")
    parser.add_argument("--sort", default="HELPFULNESS_SCORE", choices=SORT_OPTIONS)
    parser.add_argument("--order", default="DESC", choices=("ASC", "DESC"))
    # o endpoint rejeita first > 50 com BAD_USER_INPUT
    parser.add_argument("--page-size", type=int, default=50, help="reviews por requisicao (max 50)")
    parser.add_argument("--delay", type=float, default=0.5, help="pausa entre requisicoes em segundos")
    parser.add_argument("--out", default=None, help="prefixo dos arquivos de saida")
    args = parser.parse_args()

    if args.id:
        title_id = args.id
    else:
        title_id, resolved = find_title_id(args.title)
        print(f"> '{args.title}' -> {title_id} ({resolved})")

    reviews, meta = fetch_reviews(
        title_id, args.limit, args.sort, args.order, args.page_size, args.delay
    )

    if not reviews:
        raise SystemExit("nenhum review coletado")

    prefix = args.out or f"imdb_{title_id}_reviews"
    save(reviews, meta, prefix)

    with_rating = [r["rating"] for r in reviews if r["rating"] is not None]
    dist: dict[str, int] = {}
    for r in reviews:
        dist[r["sentiment"]] = dist.get(r["sentiment"], 0) + 1

    print(f"> total coletado: {len(reviews)}")
    if with_rating:
        print(f"> nota media dos autores: {sum(with_rating) / len(with_rating):.2f} ({len(with_rating)} com nota)")
    print("> distribuicao de sentimento: " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items())))


if __name__ == "__main__":
    main()
