#!/usr/bin/env python3
"""
IMDB Episode Rating Visualizer
Scrapes episode ratings from IMDB and generates a GitHub-style heatmap visualization.
"""

import argparse
import json
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

# Constants
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
OUTPUT_DIR = Path(__file__).parent / "output"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def extract_json_ld(soup: BeautifulSoup) -> dict | None:
    """Extract JSON-LD structured data from page."""
    script_tag = soup.find("script", type="application/ld+json")
    if script_tag and script_tag.string:
        try:
            return json.loads(script_tag.string)
        except json.JSONDecodeError:
            return None
    return None


def get_imdb_id(url: str) -> str:
    """Extract IMDB title ID from URL."""
    match = re.search(r"tt\d+", url)
    if not match:
        raise ValueError(f"Could not extract IMDB ID from URL: {url}")
    return match.group()


def get_show_info(imdb_id: str) -> dict:
    """Fetch show name, poster URL, and season count from IMDB."""
    url = f"https://www.imdb.com/title/{imdb_id}/"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Extract JSON-LD data
    json_ld = extract_json_ld(soup)

    # Get show name and season count from JSON-LD (primary)
    name = "Unknown Show"
    number_of_seasons = None

    if json_ld:
        name = json_ld.get("name", name)
        number_of_seasons = json_ld.get("numberOfSeasons")

    # Fallback for name if JSON-LD didn't have it
    if name == "Unknown Show":
        title_elem = soup.select_one('h1[data-testid="hero__pageTitle"] span')
        if not title_elem:
            title_elem = soup.select_one("h1")
        if title_elem:
            name = title_elem.get_text(strip=True)

    # Get poster URL (keep existing logic)
    poster_elem = soup.select_one('img.ipc-image[srcset]')
    if not poster_elem:
        poster_elem = soup.select_one('div[data-testid="hero-media__poster"] img')
    poster_url = poster_elem.get("src") if poster_elem else None

    return {"name": name, "poster_url": poster_url, "number_of_seasons": number_of_seasons}


def get_seasons(imdb_id: str, known_season_count: int | None = None) -> list[int]:
    """Get list of season numbers for a show.

    Args:
        imdb_id: The IMDB title ID
        known_season_count: If provided (from JSON-LD), use this directly

    Returns:
        List of season numbers (1-indexed)
    """
    # If we know the season count from JSON-LD, use it directly
    if known_season_count is not None and known_season_count > 0:
        return list(range(1, known_season_count + 1))

    # Fallback: fetch episodes page and try to extract season info
    url = f"https://www.imdb.com/title/{imdb_id}/episodes/"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Try JSON-LD on episodes page
    json_ld = extract_json_ld(soup)
    if json_ld:
        if "partOfSeries" in json_ld:
            series_data = json_ld["partOfSeries"]
            if "numberOfSeasons" in series_data:
                return list(range(1, series_data["numberOfSeasons"] + 1))
        if "numberOfSeasons" in json_ld:
            return list(range(1, json_ld["numberOfSeasons"] + 1))

    # Fallback: parse season links from HTML
    season_links = soup.select('a[href*="episodes?season="], a[href*="episodes/?season="]')
    if season_links:
        seasons = set()
        for link in season_links:
            match = re.search(r'season=(\d+)', link.get("href", ""))
            if match:
                seasons.add(int(match.group(1)))
        if seasons:
            return sorted(seasons)

    return [1]


def get_episode_ratings(imdb_id: str, season: int) -> list[dict]:
    """Get episode titles and ratings for a specific season using JSON-LD."""
    url = f"https://www.imdb.com/title/{imdb_id}/episodes/?season={season}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    episodes = []

    # Try JSON-LD first (most reliable)
    json_ld = extract_json_ld(soup)

    if json_ld:
        # Look for episode array in the JSON-LD
        episode_list = json_ld.get("episode", [])
        if isinstance(episode_list, list) and episode_list:
            for ep_data in episode_list:
                episode_num = ep_data.get("episodeNumber", len(episodes) + 1)
                title = ep_data.get("name", f"Episode {episode_num}")

                # Extract rating from aggregateRating
                rating = None
                agg_rating = ep_data.get("aggregateRating", {})
                if agg_rating:
                    rating_val = agg_rating.get("ratingValue")
                    if rating_val is not None:
                        try:
                            rating = float(rating_val)
                        except (ValueError, TypeError):
                            pass

                episodes.append({
                    "episode_num": int(episode_num) if episode_num else len(episodes) + 1,
                    "title": title,
                    "rating": rating,
                })

            if episodes:
                return sorted(episodes, key=lambda x: x["episode_num"])

    # Fallback: CSS selectors
    return _get_episode_ratings_css_fallback(soup)


def _get_episode_ratings_css_fallback(soup: BeautifulSoup) -> list[dict]:
    """Fallback CSS-based extraction for when JSON-LD is unavailable."""
    episodes = []

    # Try multiple selector patterns
    episode_items = soup.select('article.episode-item-wrapper')
    if not episode_items:
        episode_items = soup.select('div.list_item')
    if not episode_items:
        episode_items = soup.select('[data-testid="episodes-container"] > div')

    for idx, item in enumerate(episode_items, 1):
        # Get episode title
        title_elem = (
            item.select_one('a[data-testid="episode-title-link"]') or
            item.select_one('a[itemprop="name"]') or
            item.select_one('strong a') or
            item.select_one('a[href*="/title/tt"]')
        )
        title = title_elem.get_text(strip=True) if title_elem else None

        # Fallback: title might be in ipc-title__text div (minus episode number prefix)
        if not title:
            title_div = item.select_one('div.ipc-title__text')
            if title_div:
                title_text = title_div.get_text(strip=True)
                # Remove "S1.E1 ∙ " prefix if present
                match = re.search(r'[ES]\d+\.?[ES]?\d*\s*[∙·]\s*(.+)', title_text)
                if match:
                    title = match.group(1)
                else:
                    title = title_text

        if not title:
            title = f"Episode {idx}"

        # Get rating
        rating_elem = (
            item.select_one('span.ipc-rating-star--rating') or
            item.select_one('span.ipc-rating-star') or
            item.select_one('.ratingValue span') or
            item.select_one('[data-testid="ratingGroup--imdb-rating"]')
        )

        rating = None
        if rating_elem:
            rating_text = rating_elem.get_text(strip=True)
            match = re.search(r'(\d+\.?\d*)', rating_text)
            if match:
                try:
                    rating = float(match.group(1))
                except ValueError:
                    pass

        # Get episode number (look for E followed by number)
        ep_num = idx
        ep_num_elem = item.select_one('div.ipc-title__text')
        if ep_num_elem:
            match = re.search(r'E(\d+)', ep_num_elem.get_text())
            if match:
                ep_num = int(match.group(1))

        episodes.append({
            "episode_num": ep_num,
            "title": title,
            "rating": rating,
        })

    return episodes


def calculate_analytics(seasons_data: list[dict]) -> dict:
    """Calculate min/max episodes and averages."""
    all_episodes = []
    season_averages = []

    for season in seasons_data:
        season_ratings = []
        for ep in season["episodes"]:
            if ep["rating"] is not None:
                all_episodes.append({
                    "season": season["season_num"],
                    "episode": ep["episode_num"],
                    "title": ep["title"],
                    "rating": ep["rating"],
                })
                season_ratings.append(ep["rating"])

        if season_ratings:
            season_averages.append({
                "season_num": season["season_num"],
                "average": sum(season_ratings) / len(season_ratings),
                "episode_count": len(season_ratings),
            })

    if not all_episodes:
        return {
            "min_episode": None,
            "max_episode": None,
            "overall_average": None,
            "season_averages": [],
            "best_season": None,
            "worst_season": None,
            "total_episodes": 0,
        }

    # Find min and max
    min_ep = min(all_episodes, key=lambda x: x["rating"])
    max_ep = max(all_episodes, key=lambda x: x["rating"])

    # Overall average
    overall_avg = sum(ep["rating"] for ep in all_episodes) / len(all_episodes)

    # Best and worst seasons
    best_season = max(season_averages, key=lambda x: x["average"]) if season_averages else None
    worst_season = min(season_averages, key=lambda x: x["average"]) if season_averages else None

    return {
        "min_episode": min_ep,
        "max_episode": max_ep,
        "overall_average": round(overall_avg, 2),
        "season_averages": season_averages,
        "best_season": best_season,
        "worst_season": worst_season,
        "total_episodes": len(all_episodes),
    }


def get_rating_color(rating: float | None) -> str:
    """Get color for rating value (GitHub-style gradient)."""
    if rating is None:
        return "#3d3d3d"  # Gray for no rating

    if rating < 4:
        return "#da3633"  # Red
    elif rating < 5:
        return "#f85149"  # Light red
    elif rating < 6:
        return "#d29922"  # Orange
    elif rating < 7:
        return "#e3b341"  # Yellow
    elif rating < 8:
        return "#7ee787"  # Light green
    elif rating < 9:
        return "#3fb950"  # Green
    else:
        return "#238636"  # Dark green


def scrape_show(url: str) -> dict:
    """Scrape all show data from IMDB."""
    imdb_id = get_imdb_id(url)
    print(f"Scraping IMDB ID: {imdb_id}")

    # Get show info
    print("Fetching show info...")
    show_info = get_show_info(imdb_id)
    print(f"Show: {show_info['name']}")

    # Get seasons (use JSON-LD season count if available)
    print("Fetching seasons...")
    season_nums = get_seasons(imdb_id, show_info.get("number_of_seasons"))
    print(f"Found {len(season_nums)} season(s)")

    # Get episodes for each season
    seasons_data = []
    for season_num in season_nums:
        print(f"Fetching Season {season_num}...")
        episodes = get_episode_ratings(imdb_id, season_num)
        seasons_data.append({
            "season_num": season_num,
            "episodes": episodes,
        })
        print(f"  Found {len(episodes)} episode(s)")

    # Calculate analytics
    print("Calculating analytics...")
    analytics = calculate_analytics(seasons_data)

    # Find max episodes in any season (for grid layout)
    max_episodes = max(len(s["episodes"]) for s in seasons_data) if seasons_data else 0

    return {
        "name": show_info["name"],
        "poster_url": show_info["poster_url"],
        "imdb_id": imdb_id,
        "seasons": seasons_data,
        "analytics": analytics,
        "max_episodes": max_episodes,
    }


def generate_html(data: dict, output_path: Path) -> None:
    """Generate HTML file from scraped data."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    env.globals["get_rating_color"] = get_rating_color

    template = env.get_template("ratings.html")
    html = template.render(show=data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"Generated: {output_path}")


def slugify(text: str) -> str:
    """Convert text to URL-friendly slug."""
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')


def main():
    parser = argparse.ArgumentParser(
        description="Scrape IMDB episode ratings and generate a heatmap visualization"
    )
    parser.add_argument(
        "url",
        help="IMDB show URL (e.g., https://www.imdb.com/title/tt0903747/)"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output filename (default: {show-name}.html)"
    )

    args = parser.parse_args()

    # Scrape the show
    data = scrape_show(args.url)

    # Determine output path
    if args.output:
        output_filename = args.output
        if not output_filename.endswith(".html"):
            output_filename += ".html"
    else:
        output_filename = f"{slugify(data['name'])}.html"

    output_path = OUTPUT_DIR / output_filename

    # Generate HTML
    generate_html(data, output_path)

    # Print summary
    print("\n" + "=" * 50)
    print(f"Show: {data['name']}")
    print(f"Seasons: {len(data['seasons'])}")
    print(f"Total Episodes: {data['analytics']['total_episodes']}")
    if data['analytics']['overall_average']:
        print(f"Overall Average: {data['analytics']['overall_average']}/10")
    if data['analytics']['max_episode']:
        max_ep = data['analytics']['max_episode']
        print(f"Highest Rated: S{max_ep['season']}E{max_ep['episode']} - {max_ep['title']} ({max_ep['rating']})")
    if data['analytics']['min_episode']:
        min_ep = data['analytics']['min_episode']
        print(f"Lowest Rated: S{min_ep['season']}E{min_ep['episode']} - {min_ep['title']} ({min_ep['rating']})")
    print("=" * 50)


if __name__ == "__main__":
    main()
