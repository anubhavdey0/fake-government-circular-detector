# Running circverify as a service

On a laptop, `circverify serve` and `circverify watch` are enough. To keep it running for other people, put it
on a small server: the website, the API and the WhatsApp webhook in one process, the crawler in another, both
sharing one data folder. A VPS with 2 CPUs, 4 GB of RAM and 20 GB of disk handles UGC comfortably (the index is
about 8 GB, a check takes a few seconds of CPU).

## With Docker (recommended)

```bash
git clone https://github.com/anubhavdey0/fake-government-circular-detector
cd fake-government-circular-detector
cp .env.example .env          # set CIRCVERIFY_DOMAIN and, if used, the bot settings

docker compose build
docker compose run --rm watcher circverify crawl --wayback --scan-archive   # first index: about an hour
docker compose up -d                                                         # website + crawler every 6 hours
docker compose --profile https up -d                                         # add HTTPS (ports 80 and 443)
```

What runs:

| Service | Command | Job |
|---|---|---|
| `web` | `circverify serve` | Website, `/api/verify`, `/api/search`, receipts, feed, and the WhatsApp webhook when configured |
| `watcher` | `circverify watch --every 6` | Syncs UGC's listings, downloads and indexes new files, links corrigenda and extensions, re-checks old files every 4th run, marks withdrawn notices |
| `https` (profile) | Caddy | HTTPS with automatic Let's Encrypt certificates for `CIRCVERIFY_DOMAIN` |
| `telegram` (profile) | `circverify telegram` | Telegram bot (long polling, no public address needed) |

The index lives in the `data` volume. Back it up by copying the volume (`docker run --rm -v
fake-government-circular-detector_data:/data -v "$PWD":/backup alpine tar czf /backup/index.tgz /data`).

Useful commands:

```bash
docker compose logs -f watcher                     # what the crawler found
docker compose exec web circverify coverage        # how much of UGC's listings is indexed
docker compose exec web circverify reports list    # documents readers reported
docker compose exec web circverify verify --url https://example.org/notice.pdf
```

## WhatsApp

1. Create a Meta app with the WhatsApp product and a WhatsApp Business phone number.
2. Put the access token, phone number ID, app secret and a verify token of your choice in `.env`.
3. Start with the `https` profile so `https://<your domain>/whatsapp/webhook` is reachable.
4. In the Meta app, set that URL as the webhook with the same verify token, and subscribe to `messages`.

Every delivery's `X-Hub-Signature-256` is checked with the app secret; deliveries without a valid signature are
refused. WhatsApp Business messaging is paid; check Meta's current pricing for India before launching.

## Without Docker (systemd)

```bash
sudo useradd --system --create-home circverify
sudo python3 -m venv /opt/circverify
sudo /opt/circverify/bin/pip install "circverify[all] @ git+https://github.com/anubhavdey0/fake-government-circular-detector"
sudo apt install tesseract-ocr tesseract-ocr-hin
sudo mkdir -p /var/lib/circverify && sudo chown circverify /var/lib/circverify
sudo -u circverify CIRCVERIFY_DATA_DIR=/var/lib/circverify /opt/circverify/bin/circverify crawl
sudo cp deploy/circverify-web.service deploy/circverify-watch.service /etc/systemd/system/
sudo systemctl enable --now circverify-web circverify-watch
```

Settings go in `/etc/circverify.env` (same keys as `.env.example`). Put any reverse proxy with HTTPS in front of
`127.0.0.1:8000`; `deploy/Caddyfile` is a working example.

## Keeping it healthy

- `/coverage` shows what is indexed and the operations numbers: dead links, files that could not be read,
  reports waiting for review. A sudden drop in new listing entries usually means UGC changed its site and the
  listing parser (`src/circverify/issuers/ugc.py`) needs updating.
- `/api/stats` is a cheap health check (the Docker image uses it).
- The crawler is polite by design (robots.txt, at most two requests a second, conditional re-checks); keep it so.
