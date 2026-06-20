from flask import Flask, render_template, request, Response
import sqlite3
import random
import urllib.request
import re
import html
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import os

app = Flask(__name__)

DB_FILE = "./output/immigration_watch.db"

CRIME_KEYWORDS = (
    "crime", "murder", "assault", "theft", "stabbing",
    "gang", "police", "arrest",
)
IMMIGRATION_KEYWORDS = (
    "immigration", "migrant", "refugee", "asylum",
    "border", "deportation", "illegal",
)
TIER_WEIGHTS = {
    "T1": 1.0,
    "T2": 1.15,
    "T3": 1.45,
    "T4": 0.75,
}
BIAS_PENALTIES = {
    "low": 1.0,
    "medium": 0.9,
    "high": 0.75,
}
T1_SOURCE_TYPES = {
    "mainstream_news", "government", "news",
}
T2_SOURCE_TYPES = {
    "investigative_media",
}
T3_SOURCE_TYPES = {
    "local_news", "police_report", "city_news",
    "regional_media", "aggregator",
}
T4_SOURCE_TYPES = {
    "reddit", "forum", "blog", "x", "twitter",
    "sns", "social_media",
}
SNS_SCORE_MULTIPLIER = 0.7
PHASE7_SOURCES = (
    (
        "bbc_news", "BBC News", "bbc.com", "UK", "en",
        "mainstream_news", "national", 95,
    ),
    (
        "le_monde", "Le Monde", "lemonde.fr", "FR", "fr",
        "mainstream_news", "national", 92,
    ),
    (
        "cnn", "CNN", "cnn.com", "US", "en",
        "mainstream_news", "national", 90,
    ),
    (
        "der_spiegel", "Der Spiegel", "spiegel.de", "DE", "de",
        "mainstream_news", "national", 93,
    ),
    (
        "nhk", "NHK", "nhk.or.jp", "JP", "ja",
        "mainstream_news", "national", 98,
    ),
    (
        "reuters", "Reuters", "reuters.com", "INT", "en",
        "mainstream_news", "global", 97,
    ),
    (
        "euronews", "Euronews", "euronews.com", "EU", "en",
        "mainstream_news", "regional", 85,
    ),
)
_PHASE7_MIGRATED_DATABASES = set()

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


def classify_article(title):
    text = (title or "").lower()

    if any(keyword in text for keyword in CRIME_KEYWORDS):
        return "crime"
    if any(keyword in text for keyword in IMMIGRATION_KEYWORDS):
        return "immigration"
    return "general"


def calculate_article_score(
    reliability_score,
    source_type,
    bias_risk,
    category,
    region_scope=None,
):
    reliability = float(reliability_score or 0)
    tier = get_source_tier(source_type, region_scope)
    tier_weight = TIER_WEIGHTS[tier]
    bias_penalty = BIAS_PENALTIES.get(bias_risk, 1.0)
    keyword_bonus = {
        "crime": 12,
        "immigration": 10,
    }.get(category, 0)
    score = (
        reliability
        * tier_weight
        * bias_penalty
        + keyword_bonus
    )
    if tier == "T4":
        score *= SNS_SCORE_MULTIPLIER
    return max(0.0, min(score, 100.0))


def get_source_tier(source_type, region_scope=None):
    normalized_type = (source_type or "").strip().lower()
    normalized_scope = (region_scope or "").strip().lower()

    if normalized_type in T4_SOURCE_TYPES:
        return "T4"
    if (
        normalized_scope == "regional"
        or normalized_type in T3_SOURCE_TYPES
    ):
        return "T3"
    if normalized_type in T2_SOURCE_TYPES:
        return "T2"
    return "T1"


def get_article_group(source_type, region_scope=None):
    tier = get_source_tier(source_type, region_scope)
    if tier in ("T1", "T2"):
        return "NEWS"
    if tier == "T3":
        return "LOCAL"
    if tier == "T4":
        return "SNS"
    return "NEWS"


def _add_missing_columns(cur, table_name, columns):
    cur.execute(f"PRAGMA table_info({table_name})")
    existing = {row["name"] for row in cur.fetchall()}

    for column_name, column_type in columns:
        if column_name not in existing:
            cur.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_type}"
            )


def _phase7_category_sql(title_sql):
    crime_sql = " OR ".join(
        f"instr(lower(COALESCE({title_sql}, '')), '{keyword}') > 0"
        for keyword in CRIME_KEYWORDS
    )
    immigration_sql = " OR ".join(
        f"instr(lower(COALESCE({title_sql}, '')), '{keyword}') > 0"
        for keyword in IMMIGRATION_KEYWORDS
    )
    return (
        f"CASE WHEN ({crime_sql}) THEN 'crime' "
        f"WHEN ({immigration_sql}) THEN 'immigration' "
        "ELSE 'general' END"
    )


def migrate_phase7(conn):
    database_key = os.path.abspath(DB_FILE)
    if database_key in _PHASE7_MIGRATED_DATABASES:
        return

    cur = conn.cursor()
    _add_missing_columns(
        cur,
        "news_posts",
        (
            ("category", "TEXT"),
            ("source_id", "TEXT"),
            ("score", "REAL"),
        ),
    )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            source_name TEXT,
            domain TEXT,
            country TEXT,
            language TEXT,
            source_type TEXT,
            region_scope TEXT,
            reliability_score INTEGER,
            update_frequency TEXT,
            access_type TEXT,
            content_type TEXT,
            bias_risk TEXT,
            notes TEXT
        )
    """)
    _add_missing_columns(
        cur,
        "sources",
        (
            ("source_id", "TEXT"),
            ("domain", "TEXT"),
            ("language", "TEXT"),
            ("region_scope", "TEXT"),
            ("reliability_score", "INTEGER"),
            ("update_frequency", "TEXT"),
            ("access_type", "TEXT"),
            ("content_type", "TEXT"),
            ("bias_risk", "TEXT"),
        ),
    )
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        ux_sources_source_id
        ON sources(source_id)
        WHERE source_id IS NOT NULL
    """)

    for source in PHASE7_SOURCES:
        cur.execute("""
            INSERT OR REPLACE INTO sources (
                source_id,
                source_name,
                domain,
                country,
                language,
                source_type,
                region_scope,
                reliability_score,
                update_frequency,
                access_type,
                content_type,
                bias_risk,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            *source,
            None,
            None,
            None,
            None,
            None,
        ))

    category_sql = _phase7_category_sql("NEW.title")
    score_category_sql = _phase7_category_sql("NEW.title")
    source_match_sql = """
        FROM sources
        WHERE domain IS NOT NULL
          AND domain != ''
          AND instr(
              lower(COALESCE(NEW.url, '')),
              lower(domain)
          ) > 0
        ORDER BY length(domain) DESC
        LIMIT 1
    """
    cur.execute("DROP TRIGGER IF EXISTS trg_news_posts_phase7_insert")
    cur.execute("DROP TRIGGER IF EXISTS trg_news_posts_phase8_insert")
    cur.execute("DROP TRIGGER IF EXISTS trg_news_posts_phase8_sns_dedupe")
    cur.execute("""
        CREATE TRIGGER trg_news_posts_phase8_sns_dedupe
        BEFORE INSERT ON news_posts
        WHEN (
            lower(COALESCE(NEW.source_type, '')) IN (
                'reddit', 'forum', 'blog', 'x', 'twitter',
                'sns', 'social_media'
            )
            OR EXISTS (
                SELECT 1
                FROM sources
                WHERE lower(COALESCE(source_type, '')) IN (
                    'reddit', 'forum', 'blog', 'x', 'twitter',
                    'sns', 'social_media'
                )
                  AND domain IS NOT NULL
                  AND domain != ''
                  AND instr(
                      lower(COALESCE(NEW.url, '')),
                      lower(domain)
                  ) > 0
            )
        )
        AND EXISTS (
            SELECT 1
            FROM news_posts
            WHERE (
                COALESCE(NEW.url, '') != ''
                AND lower(COALESCE(url, ''))
                    = lower(COALESCE(NEW.url, ''))
            )
            OR (
                COALESCE(NEW.title, '') != ''
                AND lower(COALESCE(title, ''))
                    = lower(COALESCE(NEW.title, ''))
                AND lower(COALESCE(source_name, ''))
                    = lower(COALESCE(NEW.source_name, ''))
            )
        )
        BEGIN
            SELECT RAISE(IGNORE);
        END
    """)
    cur.execute(f"""
        CREATE TRIGGER trg_news_posts_phase8_insert
        AFTER INSERT ON news_posts
        BEGIN
            UPDATE news_posts
            SET
                category = {category_sql},
                source_id = (
                    SELECT source_id
                    {source_match_sql}
                ),
                score = COALESCE((
                    SELECT
                        MIN(
                            100,
                            MAX(
                                0,
                                (
                                    reliability_score
                                    * CASE
                                        WHEN lower(COALESCE(source_type, ''))
                                            IN (
                                                'reddit', 'forum', 'blog',
                                                'x', 'twitter', 'sns',
                                                'social_media'
                                            )
                                            THEN 0.75
                                        WHEN lower(COALESCE(region_scope, ''))
                                            = 'regional'
                                            OR lower(COALESCE(source_type, ''))
                                            IN (
                                                'local_news',
                                                'police_report',
                                                'city_news',
                                                'regional_media',
                                                'aggregator'
                                            )
                                            THEN 1.45
                                        WHEN lower(COALESCE(source_type, ''))
                                            = 'investigative_media'
                                            THEN 1.15
                                        ELSE 1.0
                                      END
                                    * CASE lower(COALESCE(bias_risk, 'low'))
                                        WHEN 'medium' THEN 0.9
                                        WHEN 'high' THEN 0.75
                                        ELSE 1.0
                                      END
                                    + CASE {score_category_sql}
                                        WHEN 'crime' THEN 12
                                        WHEN 'immigration' THEN 10
                                        ELSE 0
                                      END
                                )
                                * CASE
                                    WHEN lower(COALESCE(source_type, ''))
                                        IN (
                                            'reddit', 'forum', 'blog',
                                            'x', 'twitter', 'sns',
                                            'social_media'
                                        )
                                        THEN 0.7
                                    ELSE 1.0
                                  END
                            )
                        )
                    {source_match_sql}
                ), 0)
                + CASE
                    WHEN (
                        SELECT source_id
                        {source_match_sql}
                    ) IS NULL
                    THEN CASE {score_category_sql}
                        WHEN 'crime' THEN 12
                        WHEN 'immigration' THEN 10
                        ELSE 0
                      END
                    ELSE 0
                  END
            WHERE id = NEW.id;
        END
    """)

    cur.execute("""
        SELECT
            id,
            title,
            url
        FROM news_posts
    """)
    rows = cur.fetchall()

    cur.execute("""
        SELECT
            source_id,
            domain,
            source_type,
            region_scope,
            reliability_score,
            bias_risk
        FROM sources
        WHERE source_id IS NOT NULL
          AND domain IS NOT NULL
          AND domain != ''
        ORDER BY length(domain) DESC
    """)
    sources = [dict(row) for row in cur.fetchall()]

    for row in rows:
        category = classify_article(row["title"])
        article_url = (row["url"] or "").lower()
        source = next(
            (
                item for item in sources
                if item["domain"].lower() in article_url
            ),
            None,
        )
        source_id = source["source_id"] if source else None
        score = calculate_article_score(
            source["reliability_score"] if source else 0,
            source["source_type"] if source else "",
            source["bias_risk"] if source else "",
            category,
            source["region_scope"] if source else "",
        )
        cur.execute("""
            UPDATE news_posts
            SET category = ?, source_id = ?, score = ?
            WHERE id = ?
        """, (category, source_id, score, row["id"]))

    conn.commit()
    _PHASE7_MIGRATED_DATABASES.add(database_key)


def ensure_columns(cur):
    _add_missing_columns(
        cur,
        "news_posts",
        (
            ("thumbnail_url", "TEXT"),
            ("summary_ja", "TEXT"),
        ),
    )
    migrate_phase7(cur.connection)


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
    row["category"] = row.get("category") or "general"
    row["source_id"] = row.get("source_id")
    row["score"] = float(row.get("score") or 0)
    row["phase7_source_type"] = (
        row.get("phase7_source_type")
        or row.get("source_type")
        or ""
    )
    row["region_scope"] = row.get("region_scope") or ""
    row["tier"] = get_source_tier(
        row["phase7_source_type"],
        row["region_scope"],
    )
    row["display_group"] = get_article_group(
        row["phase7_source_type"],
        row["region_scope"],
    )
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


def deduplicate_items(items):
    results = []
    seen = set()

    for item in items:
        key = (
            (item.get("url") or "").strip().lower()
            or (item.get("display_title") or "").strip().lower()
        )
        if not key or key in seen:
            continue
        seen.add(key)
        results.append(item)

    return results


def get_news_by_country(cur, conn, country, limit=3):
    ensure_columns(cur)

    aliases = get_country_aliases(country)
    placeholders = ",".join(["?"] * len(aliases))

    cur.execute(f"""
        SELECT *
        FROM news_posts
        WHERE country IN ({placeholders})
        ORDER BY id DESC
        LIMIT 300
    """, aliases)

    rows = cur.fetchall()
    articles = [clean_news_row(row) for row in rows]

    articles.sort(
        key=lambda x: parse_news_date(x.get("published_at")),
        reverse=True
    )

    selected_articles = articles[:limit]
    thumb_article = selected_articles[0] if selected_articles else None

    return selected_articles, thumb_article


def get_country_news_list(cur, conn, country, limit=20):
    ensure_columns(cur)

    cur.execute("""
        SELECT *
        FROM news_posts
        WHERE country = ?
        ORDER BY score DESC
    """, (country,))

    rows = cur.fetchall()
    source_ids = {
        row["source_id"]
        for row in rows
        if row["source_id"]
    }
    source_types = {}

    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        cur.execute(f"""
            SELECT source_id, source_type, region_scope
            FROM sources
            WHERE source_id IN ({placeholders})
        """, tuple(source_ids))
        source_types = {
            row["source_id"]: {
                "source_type": row["source_type"],
                "region_scope": row["region_scope"],
            }
            for row in cur.fetchall()
        }

    articles = []
    for row in rows[:limit]:
        item = dict(row)
        source_metadata = source_types.get(
            item.get("source_id"),
            {},
        )
        item["phase7_source_type"] = (
            source_metadata.get("source_type")
            or item.get("source_type")
            or ""
        )
        item["region_scope"] = (
            source_metadata.get("region_scope")
            or ""
        )
        articles.append(clean_news_row(item))

    return articles


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
            LIMIT 300
        """, (source_name,))

        rows = cur.fetchall()
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
    europe_scope_sql = """
        (
            news_posts.country IS NULL
            OR sources.region_scope = 'regional'
        )
    """

    if terms:
        conditions = []
        params = []

        for term in terms:
            like = f"%{term}%"
            conditions.append("""
                (
                    news_posts.title LIKE ?
                    OR news_posts.title_ja LIKE ?
                    OR news_posts.summary_ja LIKE ?
                    OR news_posts.source_name LIKE ?
                )
            """)
            params.extend([like, like, like, like])

        where_sql = " OR ".join(conditions)

        cur.execute(f"""
            SELECT
                news_posts.*,
                sources.source_type AS phase7_source_type,
                sources.region_scope
            FROM news_posts
            LEFT JOIN sources
                ON news_posts.source_id = sources.source_id
            WHERE (
                {europe_scope_sql}
                OR news_posts.country = 'Europe'
            )
              AND ({where_sql})
            ORDER BY news_posts.score DESC
            LIMIT ?
        """, params + [limit])

    else:
        cur.execute(f"""
            SELECT
                news_posts.*,
                sources.source_type AS phase7_source_type,
                sources.region_scope
            FROM news_posts
            LEFT JOIN sources
                ON news_posts.source_id = sources.source_id
            WHERE (
                {europe_scope_sql}
                OR news_posts.country = 'Europe'
            )
            ORDER BY news_posts.score DESC
            LIMIT ?
        """, (limit,))

    rows = cur.fetchall()
    return [clean_news_row(row) for row in rows]


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
        LEFT JOIN sources
            ON news_posts.source_id = sources.source_id
        WHERE news_posts.country IS NULL
           OR sources.region_scope = 'regional'
           OR news_posts.country = 'Europe'
    """)
    return cur.fetchone()[0]


def get_europe_source_counts(cur):
    cur.execute("""
        SELECT news_posts.source_name, COUNT(*)
        FROM news_posts
        LEFT JOIN sources
            ON news_posts.source_id = sources.source_id
        WHERE news_posts.country IS NULL
           OR sources.region_scope = 'regional'
           OR news_posts.country = 'Europe'
        GROUP BY news_posts.source_name
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
    rows = [clean_news_row(row) for row in rows]

    for row in rows:
        row["item_type"] = "sns"
        row["platform"] = "JPN SOCIAL"
        row["source_name"] = row.get("source_name") or "JPN SOCIAL"

    return deduplicate_items(rows)[:limit]


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

    rows = [clean_sns_source(row) for row in cur.fetchall()]
    return deduplicate_items(rows)[:limit]


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
    post_id = request.args.get("post_id")

    conn = get_db_connection()
    cur = conn.cursor()

    ensure_columns(cur)
    conn.commit()

    total_count = get_europe_total_count(cur)
    source_counts = get_europe_source_counts(cur)
    articles = get_europe_monitor_news(cur, conn, q=q, limit=50)

    selected_post = get_selected_item(cur, post_id)

    print("EUROPE_DEBUG_POST_ID =", post_id)
    print("EUROPE_DEBUG_SELECTED =", selected_post)

    conn.close()

    return render_template(
        "europe.html",
        q=q,
        total_count=total_count,
        source_counts=source_counts,
        articles=articles,
        selected_post=selected_post,
    )


@app.route("/country/<country>")
def country_page(country):
    post_id = request.args.get("post_id")

    conn = get_db_connection()
    cur = conn.cursor()

    ensure_columns(cur)
    conn.commit()

    selected_post = get_selected_item(cur, post_id)

    print("DEBUG_POST_ID =", post_id)
    print("DEBUG_SELECTED =", selected_post)

    news_articles = get_country_news_list(cur, conn, country, limit=300)
    grouped_news = [
        article for article in news_articles
        if article["display_group"] == "NEWS"
    ]
    local_articles = [
        article for article in news_articles
        if article["display_group"] == "LOCAL"
    ]
    sns_news_articles = [
        article for article in news_articles
        if article["display_group"] == "SNS"
    ]
    sns_source_articles = get_country_sns_list(
        cur,
        country,
        limit=20,
    )
    sns_articles = deduplicate_items([
        *sns_news_articles,
        *sns_source_articles,
    ])

    mixed_articles = [
        *grouped_news,
        *local_articles,
        *sns_articles,
    ]

    conn.close()

    return render_template(
        "country.html",
        country=country,
        selected_post=selected_post,
        articles=mixed_articles,
        news_articles=grouped_news,
        local_articles=local_articles,
        sns_articles=sns_articles,
    )

@app.route("/debug123")
def debug123():
    return "DEBUG_OK_20260615"


@app.route("/sitemap.xml")
def sitemap():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">

<url>
<loc>https://worldwidesns.onrender.com/</loc>
<changefreq>daily</changefreq>
<priority>1.0</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/europe</loc>
<changefreq>daily</changefreq>
<priority>0.9</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/country/Germany</loc>
<changefreq>daily</changefreq>
<priority>0.8</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/country/USA</loc>
<changefreq>daily</changefreq>
<priority>0.8</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/country/UK</loc>
<changefreq>daily</changefreq>
<priority>0.8</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/country/France</loc>
<changefreq>daily</changefreq>
<priority>0.8</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/country/Italy</loc>
<changefreq>daily</changefreq>
<priority>0.8</priority>
</url>

<url>
<loc>https://worldwidesns.onrender.com/country/Japan</loc>
<changefreq>daily</changefreq>
<priority>0.8</priority>
</url>

</urlset>
"""
    return Response(xml, mimetype="application/xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
