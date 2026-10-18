"""Post rendered clips to every configured platform for one account language.

    .venv/bin/python -m quote_clips.publish --lang ja --limit 1 --dry-run
    .venv/bin/python -m quote_clips.publish --lang en --limit 2
    .venv/bin/python -m quote_clips.publish auth youtube --lang ja   # one-time browser consent
    .venv/bin/python -m quote_clips.publish delete --lang ja --video 01_a   # takedown: remove from Meta

accounts.json (see accounts.example.json) maps each language to its platform accounts.
A platform missing from the file is skipped (e.g. when Meta's own auto-share covers it).
Progress is recorded in output/posted.json so re-runs never double-post.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from loguru import logger

from quote_clips.publish import meta as meta_api
from quote_clips.publish import tiktok, youtube
from quote_clips.publish.captions import build_captions

# Instagram runs before Threads so Threads can reuse the Reel's public CDN URL.
PLATFORMS = ["youtube", "tiktok", "instagram", "facebook", "threads"]
# Automatic targets: Meta. TikTok is posted by hand in the app (its Content Posting API audit
# rejects own-account upload tools; research/copyright/DECISION.md ④): use
# `--platforms tiktok --dry-run` to print the caption. YouTube stays available via --platforms.
DEFAULT_PLATFORMS = ["instagram", "facebook", "threads"]
DELETABLE = ["instagram", "facebook", "threads"]


def load_ledger(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_ledger(path: Path, ledger: Dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def publishable(video: Path) -> bool:
    """Only text cards and licensed clips may be posted (research/copyright/DECISION.md ①②).

    Older renders without a mode (unlicensed clips from before the decision) are never posted.
    """
    meta_path = video.with_suffix(".json")
    if not meta_path.exists():
        return False
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return meta.get("mode") == "card" or (meta.get("mode") == "clip" and bool(meta.get("license")))


def pending_videos(out_dir: Path, lang: str, ledger: Dict, platforms: List[str]) -> List[Path]:
    """Clips (oldest first) that still miss at least one of the target platforms."""
    videos = sorted((out_dir / lang).glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
    skipped = [v for v in videos if not publishable(v)]
    if skipped:
        logger.warning(f"{len(skipped)} clip(s) without a card/licence record are never posted (e.g. {skipped[0].name})")
    return [v for v in videos if publishable(v) and any(p not in ledger.get(str(v), {}) for p in platforms)]


def post_one(video: Path, cfg: Dict, platforms: List[str], ledger: Dict, public_base: str, dry_run: bool) -> None:
    meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
    caps = build_captions(meta)
    done = ledger.setdefault(str(video), {})
    for platform in platforms:
        if platform in done:
            continue
        if dry_run:
            text = caps["title"] if platform == "youtube" else caps.get(platform, "")
            logger.info(f"[dry-run] {platform:9s} ← {video.name}\n{text}\n")
            continue
        try:
            if platform == "youtube":
                result = youtube.post(str(video), caps, cfg[platform])
            elif platform == "tiktok":
                result = tiktok.post(str(video), caps, cfg[platform])
            elif platform == "instagram":
                result = meta_api.post_instagram(str(video), caps, cfg[platform])
            elif platform == "facebook":
                result = meta_api.post_facebook(str(video), caps, cfg[platform])
            else:
                url = (done.get("instagram") or {}).get("media_url") or (
                    f"{public_base.rstrip('/')}/{video.relative_to(video.parents[2])}" if public_base else "")
                result = meta_api.post_threads(str(video), caps, cfg[platform], url)
        except Exception as e:  # one platform failing must not block the others
            logger.error(f"{platform} failed for {video.name}: {e}")
            continue
        done[platform] = {**result, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def delete_video(name: str, cfg: Dict, ledger: Dict, dry_run: bool) -> List[str]:
    """Delete one clip's posts from every Meta platform it was published to.

    `name` matches the end of the ledger key (file stem or path). TikTok must be deleted in the app.
    """
    keys = [k for k in ledger if Path(k).stem == name or k.endswith(name)]
    if len(keys) != 1:
        raise SystemExit(f"'{name}' matches {len(keys)} posted clips; give a more specific name")
    entry = ledger[keys[0]]
    removed = []
    for platform in DELETABLE:
        post = entry.get(platform)
        if not post or post.get("deleted_at") or not post.get("id"):
            continue
        if dry_run:
            logger.info(f"[dry-run] would delete {platform} {post['id']}")
            continue
        if platform not in cfg:
            logger.error(f"{platform}: not configured in accounts.json; delete it by hand")
            continue
        try:
            meta_api.delete_post(platform, post["id"], cfg[platform])
        except Exception as e:
            logger.error(f"{platform} delete failed: {e}")
            continue
        post["deleted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        removed.append(platform)
    if "tiktok" in entry:
        logger.warning("TikTok: delete this video in the TikTok app (no API delete)")
    return removed


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Publish rendered clips")
    p.add_argument("command", nargs="?", default="post", choices=["post", "auth", "delete"])
    p.add_argument("platform", nargs="?", help="for `auth`: youtube")
    p.add_argument("--lang", required=True, choices=["ja", "en"])
    p.add_argument("--accounts", default="accounts.json")
    p.add_argument("--out-dir", default="output")
    p.add_argument("--platforms", default=",".join(DEFAULT_PLATFORMS))
    p.add_argument("--limit", type=int, default=1, help="max clips to post in this run")
    p.add_argument("--public-base-url", default="", help="optional public URL of the output/ folder (for Threads)")
    p.add_argument("--dry-run", action="store_true", help="show what would be posted; no API calls")
    p.add_argument("--video", help="for `delete`: posted clip file name (stem) to remove")
    args = p.parse_args()

    accounts = json.loads(Path(args.accounts).read_text(encoding="utf-8")) if Path(args.accounts).exists() else {}
    cfg = accounts.get(args.lang, {})

    if args.command == "auth":
        if args.platform != "youtube":
            sys.exit("auth is only needed for youtube; see README for TikTok/Meta tokens")
        youtube._credentials(cfg["youtube"])
        logger.info("YouTube token saved")
        return

    if args.command == "delete":
        if not args.video:
            sys.exit("delete needs --video <clip file name>")
        ledger_path = Path(args.out_dir) / "posted.json"
        ledger = load_ledger(ledger_path)
        removed = delete_video(args.video, cfg, ledger, args.dry_run)
        if removed:
            save_ledger(ledger_path, ledger)
        logger.info(f"Deleted from: {', '.join(removed) or 'nothing'}")
        return

    wanted = [x.strip() for x in args.platforms.split(",") if x.strip()]
    platforms = [x for x in PLATFORMS if x in wanted and (x in cfg or args.dry_run)]
    skipped = [x for x in wanted if x not in platforms]
    if skipped:
        logger.info(f"Not configured for '{args.lang}' (skipped): {', '.join(skipped)}")
    if not platforms:
        sys.exit(f"No platforms configured for '{args.lang}' in {args.accounts}")

    ledger_path = Path(args.out_dir) / "posted.json"
    ledger = load_ledger(ledger_path)
    queue = pending_videos(Path(args.out_dir), args.lang, ledger, platforms)[: args.limit]
    if not queue:
        logger.info("Nothing left to post")
        return
    for video in queue:
        post_one(video, cfg, platforms, ledger if not args.dry_run else {}, args.public_base_url, args.dry_run)
        if not args.dry_run:
            save_ledger(ledger_path, ledger)
    logger.info(f"Processed {len(queue)} clip(s) for '{args.lang}'")


if __name__ == "__main__":
    main()
