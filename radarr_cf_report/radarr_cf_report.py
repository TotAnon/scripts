#!/usr/bin/env python3
import argparse
import configparser
import requests
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

def load_config(config_path: str) -> dict:
    config = configparser.ConfigParser()
    defaults = {'url': '', 'apikey': '', 'output': 'radarr_cf_report.txt'}
    if Path(config_path).is_file():
        try:
            config.read(config_path)
            if 'radarr' in config:
                for key in defaults:
                    if key in config['radarr']:
                        defaults[key] = config['radarr'][key].strip()
        except Exception as e:
            print(f"Warning: Could not read config file {config_path}: {e}")
    return defaults

def get_quality_string(quality_obj: Dict) -> str:
    """Combines Quality Name and Resolution into a readable string"""
    if not quality_obj:
        return "N/A"

    q_name = quality_obj.get('quality', {}).get('name', 'Unknown')
    # Remove 'p' if it's already in the name to avoid '2160pp'
    q_name = q_name.replace('p', '')

    # Map common names to your preferred shorthand
    q_name = q_name.replace('WebDL', 'WEBDL').replace('WebRip', 'WEBRIP').replace('Bluray', 'BLURAY')

    return q_name

def main():
    parser = argparse.ArgumentParser(description="Radarr CF Reporter - Readable Quality")
    parser.add_argument("--url", help="Radarr internal URL")
    parser.add_argument("--apikey", help="Radarr API key")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--config", default="radarr_cf_report.conf", help="Path to config file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    url = args.url or cfg['url']
    apikey = args.apikey or cfg['apikey']
    output = args.output or cfg['output']

    if not url or not apikey:
        print("Error: --url and --apikey are required.")
        sys.exit(1)

    base_url = url.rstrip("/")
    headers = {"X-Api-Key": apikey, "User-Agent": "Radarr-CF-Reporter-Unraid"}

    print(f"Connecting to Radarr at: {base_url}")

    try:
        # Step 1: Map CF IDs to Names
        cf_defs_res = requests.get(f"{base_url}/api/v3/customformat", headers=headers, timeout=30)
        cf_defs_res.raise_for_status()
        cf_map = {cf['id']: cf['name'] for cf in cf_defs_res.json()}

        # Step 2: Get movie list
        response = requests.get(f"{base_url}/api/v3/movie", headers=headers, timeout=60)
        response.raise_for_status()
        movies = response.json()

        print(f"Found {len(movies)} movies. Deep-scanning for quality and scores...")

    except Exception as e:
        print(f"Failed to retrieve data: {e}")
        sys.exit(1)

    report_data = []

    # Step 3: Deep scan files
    for index, movie in enumerate(movies):
        title = movie.get("title", "Unknown")
        year = movie.get("year", "—")
        movie_file = movie.get("movieFile")

        if index % 100 == 0 and index > 0:
            print(f"Processing... {index}/{len(movies)}")

        file_info = {
            "title": title,
            "year": year,
            "quality": "MISSING",
            "score": 10000,
            "formats": "[No File]"
        }

        if movie_file:
            file_id = movie_file.get("id")
            try:
                f_res = requests.get(f"{base_url}/api/v3/moviefile/{file_id}", headers=headers, timeout=10)
                f_res.raise_for_status()
                full_file = f_res.json()

                # Get the human-readable quality instead of pixel width
                file_info["quality"] = get_quality_string(full_file.get("quality", {}))
                file_info["score"] = full_file.get("customFormatScore", 0)

                cf_list = full_file.get("customFormats", [])
                cf_names = [cf.get("name") or cf_map.get(cf.get("id")) or f"ID:{cf.get('id')}" for cf in cf_list]
                file_info["formats"] = ", ".join(cf_names) if cf_names else "(none)"

            except:
                file_info["quality"] = "ERR"
                file_info["score"] = 9999
                file_info["formats"] = "(Error fetching details)"

        report_data.append(file_info)

    # Step 4: Sort by score (Lowest to Highest)
    report_data.sort(key=lambda x: x['score'])

    # Step 5: Build Output
    lines = [
        "Radarr Custom Format Report (Sorted: Low Score -> High Score -> Missing)",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Movies: {len(movies)}",
        "=" * 140,
        f"{'Title'.ljust(50)} {'Year'.ljust(6)} {'Quality'.ljust(18)} {'Score'.rjust(7)}   Matched Formats",
        "-" * 140
    ]

    for item in report_data:
        t_display = (item['title'][:47] + "...") if len(item['title']) > 50 else item['title'].ljust(50)
        y_display = str(item['year']).ljust(6)
        q_display = item['quality'].ljust(18)

        s_val = item['score']
        s_display = "0".rjust(7) if s_val == 10000 else str(s_val).rjust(7)

        lines.append(f"{t_display} {y_display} {q_display} {s_display}   {item['formats']}")

    try:
        Path(output).write_text("\n".join(lines), encoding="utf-8")
        print(f"\nSuccess! Report with quality strings saved to {output}")
    except Exception as e:
        print(f"Failed to write output: {e}")

if __name__ == "__main__":
    main()
