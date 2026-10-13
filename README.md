# scripts
I have no idea what I'm doing. Claude made these with my ideas.

Random scripts I use in unraid

I use Unraid

| Script | What it does |
|---|---|
| [radarr_cf_report](radarr_cf_report) | Radarr custom format report (setup below) |
| [queue_manager](queue_manager) | Queue manager for Radarr/Sonarr/qBittorrent via qui |
| [diskio_monitor](diskio_monitor) | Watches array disk I/O, works out what's keeping disks busy (containers, torrents), and reports to Discord. Setup is in [its own README](diskio_monitor/README.md) |

# radarr_cf_report.py:

mkdir /mnt/user/appdata/scripts/reports

put the script and conf in: /mnt/user/appdata/scripts/

chmod +x /mnt/user/appdata/radarr_cf_report.py

unraid terminal: python3 radarr_cf_report.py 

Alternative (if you're like me and have your containers on custom docker network and completely isolated from LAN or even unraids host): 

python3 radarr_cf_report.py --url "http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' radarr):7878"

If this doesn't work ask Grok, re-read the first line above.
