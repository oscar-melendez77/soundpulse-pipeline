import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", "script:soundpulse:v1.0 (by u/soundpulse)")

HEADERS = {"User-Agent": REDDIT_USER_AGENT}

SUBREDDITS = [
    "worldnews",
    "news",
    "AskReddit",
    "offmychest",
    "anxiety",
    "depression",
    "happy",
    "UpliftingNews",
    "rant",
    "TrueOffMyChest",
]

POSTS_LIMIT   = 50
REQUEST_DELAY = 2


def _to_record(subreddit_name, post_id, title, body, score, upvote_ratio, num_comments, created):
    return {
        "post_id":      post_id,
        "subreddit":    subreddit_name,
        "title":        title,
        "body":         body,
        "score":        score,
        "upvote_ratio": upvote_ratio,
        "num_comments": num_comments,
        "created_utc":  datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
    }


def _in_window(created, after_epoch, before_epoch):
    if after_epoch and created < after_epoch:
        return False
    if before_epoch and created > before_epoch:
        return False
    return True


def _get_praw_client():
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    import praw
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )
    reddit.read_only = True
    return reddit


def _fetch_with_praw(reddit, subreddit_name, after_epoch, before_epoch):
    sub    = reddit.subreddit(subreddit_name)
    source = sub.new(limit=POSTS_LIMIT * 2) if (after_epoch or before_epoch) else sub.hot(limit=POSTS_LIMIT)
    posts  = []
    for p in source:
        if not _in_window(p.created_utc, after_epoch, before_epoch):
            continue
        posts.append(_to_record(subreddit_name, p.id, p.title, p.selftext, p.score,
                                p.upvote_ratio, p.num_comments, p.created_utc))
    return posts


def _fetch_public_json(subreddit_name, after_epoch, before_epoch):
    listing = "new" if (after_epoch or before_epoch) else "hot"
    url     = f"https://www.reddit.com/r/{subreddit_name}/{listing}.json"
    resp    = requests.get(url, headers=HEADERS, params={"limit": POSTS_LIMIT}, timeout=10)
    resp.raise_for_status()
    posts = []
    for child in resp.json()["data"]["children"]:
        p       = child["data"]
        created = p.get("created_utc", 0)
        if not _in_window(created, after_epoch, before_epoch):
            continue
        posts.append(_to_record(subreddit_name, p.get("id"), p.get("title"), p.get("selftext"),
                                p.get("score"), p.get("upvote_ratio"), p.get("num_comments"), created))
    return posts


def fetch_subreddit_posts(subreddit_name: str,
                          after_epoch: int | None = None,
                          before_epoch: int | None = None,
                          reddit=None) -> list[dict]:
    """Fetch posts from a subreddit. Uses the official API (OAuth via praw) when
    REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set; otherwise falls back to the
    public JSON endpoint, which Reddit blocks from most cloud IPs."""
    logger.info(f"Fetching r/{subreddit_name}" +
                (f" [window {after_epoch}-{before_epoch}]" if after_epoch or before_epoch else ""))
    if reddit is not None:
        posts = _fetch_with_praw(reddit, subreddit_name, after_epoch, before_epoch)
    else:
        posts = _fetch_public_json(subreddit_name, after_epoch, before_epoch)
    logger.info(f"Fetched {len(posts)} posts from r/{subreddit_name}")
    return posts


def run_reddit_ingestion(date_str: str | None = None) -> list[dict]:
    """Fetch Reddit posts. Pass date_str (YYYY-MM-DD) to backfill a specific day."""
    if date_str:
        d            = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        after_epoch  = int(d.timestamp())
        before_epoch = int((d + timedelta(days=1)).timestamp())
        logger.info(f"Starting Reddit ingestion [{date_str}]")
    else:
        after_epoch  = None
        before_epoch = None
        logger.info("Starting Reddit ingestion [current]")

    reddit = None
    try:
        reddit = _get_praw_client()
    except Exception as e:
        logger.error(f"Could not create Reddit API client: {e}")

    if reddit is not None:
        logger.info("Using official Reddit API (OAuth)")
    else:
        logger.warning("REDDIT_CLIENT_ID/SECRET not set - using public JSON (often blocked from cloud)")

    all_posts = []
    for subreddit_name in SUBREDDITS:
        try:
            all_posts.extend(fetch_subreddit_posts(subreddit_name, after_epoch, before_epoch, reddit))
        except Exception as e:
            logger.error(f"Failed r/{subreddit_name}: {e}")
        finally:
            time.sleep(REQUEST_DELAY)

    logger.info(f"Ingestion complete. Total posts: {len(all_posts)}")
    return all_posts


def save_to_local(posts: list[dict]) -> str:
    output_dir = "data/raw/reddit"
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    filename = f"{output_dir}/reddit_{date_str}.json"
    with open(filename, "w") as f:
        json.dump(posts, f, indent=2)
    logger.info(f"Saved {len(posts)} posts to {filename}")
    return filename


if __name__ == "__main__":
    posts = run_reddit_ingestion()
    save_to_local(posts)
    from upload_helper import upload_to_gcs
    upload_to_gcs(posts, 'reddit')
