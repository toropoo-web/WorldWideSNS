from flask import Flask, render_template, request
import sqlite3
import random
import urllib.request
import re
import html
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

app = Flask(__name__)

DB_FILE = "./output/immigration_watch.db"

COUNTRY_SOURCE_MAP = {
    "Japan": "日本",
    "JPN": "日本",
    "USA": "アメリカ",
    "UK": "イギリス",
    "Italy": "イタリア",
    "France": "フランス",
    "Germany": "ドイツ",
}

MONITOR_COUNTRIES = [
    "Japan",
    "USA",
    "UK",
    "Italy",
    "France",
    "Germany",
]

SEARCH_EXPANSION = {
    "移民": ["移民", "migration", "migrant", "immigration"],
    "難民": ["難民", "refugee", "asylum", "asylum seeker"],
    "国境": ["国境", "border", "frontier"],
    "強制送還": ["強制送還", "deportation", "deport", "removal"],
    "送還": ["送還", "deportation", "deport", "return"],
    "不法移民": ["不法移民", "illegal migration", "illegal migrant", "irregular migration"],
    "犯罪": ["犯罪", "crime", "criminal", "violence"],
    "暴動": ["暴動", "riot", "unrest", "violence"],
    "統合": ["統合", "integration"],
    "移民協定": ["移民協定", "migration pact", "EU migration pact"],
}

def get_country_aliases(country):
    if country in ("Japan", "JPN"):
        return ["Japan", "JPN"]
    return [country]

def expand_search_terms(q):
    q = (q or "").strip()

    if not q:
        return []

    terms = [q]

    for key, values in SEARCH_EXPANSION.items():
        if key in q:
            for value in values:
                if value not in terms:
                    terms.append(value)

    return terms

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_columns(cur):
    cur.execute("PRAGMA table_info(news_posts)")
    columns = [row["name"] for row in cur.fetchall()]

    if "thumbnail_url" not in columns:
        cur.execute("ALTER TABLE news_posts ADD COLUMN thumbnail_url TEXT")

    if "summary_ja" not in columns:
        cur.execute("ALTER TABLE news_posts ADD COLUMN summary_ja TEXT")

def parse_news_date(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)

    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

def fetch_og_image(url):
    if not url:
        return ""

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read(300000).decode("utf-8", errors="ignore")

        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        ]

        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                return html.unescape(match.group(1))

    except Exception:
        return ""

    return ""

def add_thumbnail_if_missing(cur, conn, row):
    if not row:
        return row

    item = dict(row)

    if item.get("thumbnail_url"):
        return item

    thumbnail_url = fetch_og_image(item.get("url"))

    if thumbnail_url:
        cur.execute(
            "UPDATE news_posts SET thumbnail_url = ? WHERE id = ?",
            (thumbnail_url, item["id"])
        )
        conn.commit()
        item["thumbnail_url"] = thumbnail_url

    return item

def clean_news_row(row):
    row = dict(row)

    title = row.get("title") or ""
    title_ja = row.get("title_ja") or ""
    summary_ja = row.get("summary_ja") or ""

    display_title = title_ja or title
    display_title = display_title.replace("翻訳済み_", "")

    row["id"] = row.get("id")
    row["title"] = title
    row["title_ja"] = title_ja
    row["display_title"] = display_title
    row["summary_ja"] = summary_ja
    row["display_summary"] = summary_ja
    row["url"] = row.get("url") or "#"
    row["source_name"] = row.get("source_name") or ""
    row["country"] = row.get("country") or ""
    row["published_at"] = row.get("published_at") or ""
    row["thumbnail_url"] = row.get("thumbnail_url") or ""
    row["item_type"] = "news"
    row["platform"] = "NEWS"
    row["title_short"] = display_title[:42] + "..." if len(display_title) > 42 else display_title

    return row

def clean_selected_news(row):
    if not row:
        return None

    row = clean_news_row(row)
    row["display_source"] = row.get("source_name") or "NEWS"
    row["display_url"] = row.get("url") or "#"
    row["display_type"] = "NEWS"

    return row

def clean_sns_source(row):
    row = dict(row)

    source_id = row.get("id")
    title = row.get("source_name") or ""
    platform = row.get("source_type") or "SNS"
    url = row.get("official_url") or "#"

    return {
        "id": "sns_" + str(source_id),
        "title": title,
        "title_ja": "",
        "display_title": title,
        "title_short": title[:42] + "..." if len(title) > 42 else title,
        "summary_ja": "",
        "display_summary": "",
        "url": url,
        "platform": platform,
        "source_name": platform,
        "published_at": "",
        "thumbnail_url": "",
        "item_type": "sns",
    }

def clean_selected_sns(row):
    if not row:
        return None

    item = clean_sns_source(row)
    item["display_source"] = item["platform"]
    item["display_url"] = item["url"]
    item["display_summary"] = "SNS / Community source"
    item["display_type"] = "SNS"

    return item

def pick_random_items(items, limit):
    if not items:
        return []
    return random.sample(items, min(limit, len(items)))

def get_news_by_country(cur, conn, country, limit=3):
    ensure_columns(cur)

    aliases = get_country_aliases(country)
    placeholders = ",".join(["?"] * len(aliases))

    cur.execute(f"""
        SELECT *
        FROM news_posts
        WHERE country IN ({placeholders})
        ORDER BY published_at DESC
        LIMIT ?
    """, aliases + [limit])

    rows = cur.fetchall()
    # rows = [add_thumbnail_if_missing(cur, conn, row) for row in rows]
    rows = [clean_news_row(row) for row in rows]

    text_articles = rows[:2]
    thumb_article = rows[2] if len(rows) >= 3 else None

    return text_articles, thumb_article

def get_country_news_list(cur, conn, country, limit=20):
    ensure_columns(cur)

    aliases = get_country_aliases(country)
    placeholders = ",".join(["?"] * len(aliases))

    cur.execute(f"""
        SELECT *
        FROM news_posts
        WHERE country IN ({placeholders})
        ORDER BY published_at DESC
        LIMIT ?
    """, aliases + [limit])

    rows = cur.fetchall()
    # rows = [add_thumbnail_if_missing(cur, conn, row) for row in rows]
    rows = [clean_news_row(row) for row in rows]

    return rows

def get_europe_cross_tile(cur, conn):
    ensure_columns(cur)

    cur.execute("""
        SELECT source_name, COUNT(*) AS cnt
        FROM news_posts
        WHERE country = 'Europe'
          AND source_name IS NOT NULL
          AND source_name != ''
        GROUP BY source_name
        ORDER BY cnt DESC
        LIMIT 4
    """)

    source_rows = cur.fetchall()
    results = []

    for source_row in source_rows:
        source_name = source_row["source_name"]

        cur.execute("""
            SELECT *
            FROM news_posts
            WHERE country = 'Europe'
              AND source_name = ?
            ORDER BY id DESC
            LIMIT 100
        """, (source_name,))

        rows = cur.fetchall()
        # rows = [add_thumbnail_if_missing(cur, conn, row) for row in rows]
        rows = [clean_news_row(row) for row in rows]

        rows.sort(
            key=lambda x: parse_news_date(x.get("published_at")),
            reverse=True
        )

        if rows:
            results.append(rows[0])

    return results

def get_europe_monitor_news(cur, conn, q="", limit=50):
    ensure_columns(cur)

    terms = expand_search_terms(q)

    if terms:
        conditions = []
        params = []

        for term in terms:
            like = f"%{term}%"
            conditions.append("""
                (
                    title LIKE ?
                    OR title_ja LIKE ?
                    OR summary_ja LIKE ?
                    OR source_name LIKE ?
                )
            """)
            params.extend([like, like, like, like])

        where_sql = " OR ".join(conditions)

        cur.execute(f"""
            SELECT *
            FROM news_posts
            WHERE country = 'Europe'
              AND ({where_sql})
            ORDER BY id DESC
            LIMIT 800
        """, params)

    else:
        cur.execute("""
            SELECT *
            FROM news_posts
            WHERE country = 'Europe'
            ORDER BY id DESC
            LIMIT 800
        """)

    rows = cur.fetchall()
    # rows = [add_thumbnail_if_missing(cur, conn, row) for row in rows]
    rows = [clean_news_row(row) for row in rows]

    rows.sort(
        key=lambda x: parse_news_date(x.get("published_at")),
        reverse=True
    )

    return rows[:limit]

def get_global_search_results(cur, conn, q="", limit=120):
    ensure_columns(cur)

    terms = expand_search_terms(q)

    if not terms:
        return []

    conditions = []
    params = []

    for term in terms:
        like = f"%{term}%"
        conditions.append("""
            (
                title LIKE ?
                OR title_ja LIKE ?
                OR summary_ja LIKE ?
                OR source_name LIKE ?
                OR country LIKE ?
            )
        """)
        params.extend([like, like, like, like, like])

    where_sql = " OR ".join(conditions)

    cur.execute(f"""
        SELECT *
        FROM news_posts
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT ?
    """, params + [limit])

    rows = cur.fetchall()
    # rows = [add_thumbnail_if_missing(cur, conn, row) for row in rows]
    rows = [clean_news_row(row) for row in rows]

    rows.sort(
        key=lambda x: parse_news_date(x.get("published_at")),
        reverse=True
    )

    return rows

def get_europe_total_count(cur):
    cur.execute("""
        SELECT COUNT(*)
        FROM news_posts
        WHERE country = 'Europe'
    """)
    return cur.fetchone()[0]

def get_europe_source_counts(cur):
    cur.execute("""
        SELECT source_name, COUNT(*)
        FROM news_posts
        WHERE country = 'Europe'
        GROUP BY source_name
        ORDER BY COUNT(*) DESC
    """)
    return cur.fetchall()

def get_sns_sources_by_country(cur, country_ja, limit=3):
    cur.execute("""
        SELECT rowid AS id, source_name, source_type, official_url, country
        FROM sources
        WHERE country = ?
          AND source_type IN ('reddit', 'forum', 'blog')
        ORDER BY source_name
    """, (country_ja,))

    rows = [clean_sns_source(row) for row in cur.fetchall()]
    return pick_random_items(rows, limit)

def get_jpn_social_monitor_items(cur, conn, limit=3):
    ensure_columns(cur)

    cur.execute("""
        SELECT *
        FROM news_posts
        WHERE country = 'Japan'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cur.fetchall()
    # rows = [add_thumbnail_if_missing(cur, conn, row) for row in rows]
    rows = [clean_news_row(row) for row in rows]

    for row in rows:
        row["item_type"] = "sns"
        row["platform"] = "JPN SOCIAL"
        row["source_name"] = row.get("source_name") or "JPN SOCIAL"

    return rows

def get_country_sns_list(cur, country, limit=20):
    if country == "Japan":
        conn = cur.connection
        return get_jpn_social_monitor_items(cur, conn, limit=limit)

    country_ja = COUNTRY_SOURCE_MAP.get(country, country)

    cur.execute("""
        SELECT rowid AS id, source_name, source_type, official_url, country
        FROM sources
        WHERE country = ?
          AND source_type IN ('reddit', 'forum', 'blog')
        ORDER BY source_name
        LIMIT ?
    """, (country_ja, limit))

    return [clean_sns_source(row) for row in cur.fetchall()]

def get_selected_item(cur, post_id):
    if not post_id:
        return None

    post_id = str(post_id)

    if post_id.startswith("sns_"):
        sns_id = post_id.replace("sns_", "", 1)

        cur.execute("""
            SELECT rowid AS id, source_name, source_type, official_url, country
            FROM sources
            WHERE rowid = ?
        """, (sns_id,))

        return clean_selected_sns(cur.fetchone())

    cur.execute("""
        SELECT *
        FROM news_posts
        WHERE id = ?
    """, (post_id,))

    return clean_selected_news(cur.fetchone())

def build_country_payload(cur, conn, country):
    country_ja = COUNTRY_SOURCE_MAP.get(country, country)

    articles, thumb_article = get_news_by_country(cur, conn, country, limit=3)

    if country == "Japan":
        sns_items = get_jpn_social_monitor_items(cur, conn, limit=2)
    else:
        sns_items = get_sns_sources_by_country(cur, country_ja, limit=2)

    return {
        "country": country,
        "country_ja": country_ja,
        "articles": articles,
        "thumb_article": thumb_article,
        "sns": sns_items,
    }

@app.route("/")
def index():
    q = request.args.get("q", "").strip()

    conn = get_db_connection()
    cur = conn.cursor()

    ensure_columns(cur)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM news_posts")
    total_posts = cur.fetchone()[0]

    total_countries = len(MONITOR_COUNTRIES)

    cur.execute("SELECT COUNT(DISTINCT source_name) FROM news_posts")
    total_sources = cur.fetchone()[0]

    country_payloads = {}
    for country in MONITOR_COUNTRIES:
        country_payloads[country.lower()] = build_country_payload(cur, conn, country)

    europe_cross_tile = get_europe_cross_tile(cur, conn)
    search_results = get_global_search_results(cur, conn, q=q, limit=120)

    conn.close()

    return render_template(
        "index.html",
        q=q,
        search_results=search_results,

        total_posts=total_posts,
        total_countries=total_countries,
        total_sources=total_sources,

        japan_articles=country_payloads["japan"]["articles"],
        usa_articles=country_payloads["usa"]["articles"],
        uk_articles=country_payloads["uk"]["articles"],
        italy_articles=country_payloads["italy"]["articles"],
        france_articles=country_payloads["france"]["articles"],
        germany_articles=country_payloads["germany"]["articles"],

        japan_thumb_article=country_payloads["japan"]["thumb_article"],
        usa_thumb_article=country_payloads["usa"]["thumb_article"],
        uk_thumb_article=country_payloads["uk"]["thumb_article"],
        italy_thumb_article=country_payloads["italy"]["thumb_article"],
        france_thumb_article=country_payloads["france"]["thumb_article"],
        germany_thumb_article=country_payloads["germany"]["thumb_article"],

        japan_sns=country_payloads["japan"]["sns"],
        usa_sns=country_payloads["usa"]["sns"],
        uk_sns=country_payloads["uk"]["sns"],
        italy_sns=country_payloads["italy"]["sns"],
        france_sns=country_payloads["france"]["sns"],
        germany_sns=country_payloads["germany"]["sns"],

        europe_cross_tile=europe_cross_tile,
    )

@app.route("/europe")
def europe_page():
    q = request.args.get("q", "").strip()

    conn = get_db_connection()
    cur = conn.cursor()

    ensure_columns(cur)
    conn.commit()

    total_count = get_europe_total_count(cur)
    source_counts = get_europe_source_counts(cur)
    articles = get_europe_monitor_news(cur, conn, q=q, limit=50)

    conn.close()

    return render_template(
        "europe.html",
        q=q,
        total_count=total_count,
        source_counts=source_counts,
        articles=articles,
    )

@app.route("/country/<country>")
def country_page(country):
    post_id = request.args.get("post_id")

    conn = get_db_connection()
    cur = conn.cursor()

    ensure_columns(cur)
    conn.commit()

    selected_post = get_selected_item(cur, post_id)

    news_articles = get_country_news_list(cur, conn, country, limit=20)
    sns_articles = get_country_sns_list(cur, country, limit=20)

    mixed_articles = []
    max_len = max(len(news_articles), len(sns_articles))

    for i in range(max_len):
        if i < len(news_articles):
            mixed_articles.append(news_articles[i])

        if i < len(sns_articles):
            mixed_articles.append(sns_articles[i])

    conn.close()

    return render_template(
        "country.html",
        country=country,
        selected_post=selected_post,
        articles=mixed_articles,
        news_articles=news_articles,
        sns_articles=sns_articles,
    )

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)