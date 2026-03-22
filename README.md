# scripts
I have no idea what I'm doing. Grok made these.

random scripts i use in unraid

I use Unraid

# radarr_cf_report.py:

mkdir /mnt/user/appdata/scripts/reports

put the script and conf in: /mnt/user/appdata/scripts/

chmod +x /mnt/user/appdata/radarr_cf_report.py

unraid terminal: python3 radarr_cf_report.py 
Alternative (if you're like me and have your containers on custom docker network and completely isolated from LAN or even unraids host): 
python3 radarr_cf_report.py --url "http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' radarr):7878"

If this doesn't work ask Grok, re-read the first line above.
