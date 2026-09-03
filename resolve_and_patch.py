#!/usr/bin/env python3
"""
resolve_and_patch.py
─────────────────────────────────────────────────────────────────────────────
Resolves the current IP of one or more Docker containers on a custom
network (via `docker inspect`) and rewrites any `url:` field in a given
YAML config so its host matches the resolved IP — matched by PORT NUMBER,
not by where the field lives in the file. This means it works unmodified
across upgradinatorr.yml, renameinatorr.yml, blocklist_cleaner.yml, and
asset_cleanup.yml, even though they each structure their config
differently (flat instance lists vs. nested radarr/sonarr/plex blocks).

No hardcoded IPs, ever. No port mapping/exposure required — this works
for containers on a custom Docker network reachable directly from the
Unraid host, which is your setup.

Usage:
    resolve_and_patch.py <config.yml> <container:port> [<container:port> ...]

Example:
    resolve_and_patch.py upgradinatorr.yml radarr:7878 sonarr:8989

This edits the file in place. Run it immediately before the target
script, every time — cheap, and always correct even if a container's
IP has changed since the last run.
"""

import re
import subprocess
import sys


def resolve_ip(container_name: str) -> str | None:
    """Returns the container's current IP on whichever network(s) it's
    attached to, or None if the container isn't running / doesn't exist."""
    try:
        result = subprocess.run(
            [
                "docker", "inspect", "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                container_name,
            ],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        print("ERROR: 'docker' command not found on this host.", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"ERROR: docker inspect failed for '{container_name}': "
              f"{result.stderr.strip()}", file=sys.stderr)
        return None

    ips = [ip for ip in result.stdout.strip().split() if ip]
    if not ips:
        print(f"ERROR: '{container_name}' has no IP (is it running? "
              f"is it on host network mode?)", file=sys.stderr)
        return None

    return ips[0]


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <config.yml> <container:port> [<container:port> ...]",
              file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]
    pairs = sys.argv[2:]

    # port -> resolved IP
    port_to_ip = {}
    had_error = False

    for pair in pairs:
        if ":" not in pair:
            print(f"ERROR: '{pair}' is not in container:port form.", file=sys.stderr)
            had_error = True
            continue
        container, port = pair.rsplit(":", 1)
        ip = resolve_ip(container)
        if ip is None:
            had_error = True
            continue
        port_to_ip[port] = ip
        print(f"Resolved {container} -> {ip}:{port}")

    if had_error:
        print("One or more containers could not be resolved; leaving config "
              "unpatched for those entries and aborting to avoid pointing "
              "the script at a stale/wrong address.", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "r") as f:
        content = f.read()

    # Matches http(s)://HOST:PORT and rewrites HOST only, for any PORT we
    # were asked to resolve. Leaves everything else (indentation, quoting,
    # comments, unrelated urls like Discord webhooks) untouched.
    def replace(match):
        scheme, _host, port, rest = match.group(1), match.group(2), match.group(3), match.group(4)
        if port in port_to_ip:
            return f"{scheme}{port_to_ip[port]}:{port}{rest}"
        return match.group(0)

    pattern = re.compile(r'(https?://)([^:/\s"\']+):(\d+)([/\s"\']|$)')
    new_content = pattern.sub(replace, content)

    if new_content != content:
        with open(config_path, "w") as f:
            f.write(new_content)
        print(f"Patched {config_path}")
    else:
        print(f"No matching url: fields found for ports {list(port_to_ip)} "
              f"in {config_path} — check the file already has the right "
              f"ports configured.")


if __name__ == "__main__":
    main()
