<p align="center">
  <img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/filmhound.png" width="96" alt="">
  <br>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/filmhound-wordmark.svg">
    <img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/icons/filmhound-wordmark-light-bg.svg" height="64" alt="Filmhound">
  </picture>
</p>

Self-hosted movie and TV recommendations built from **your** Plex library and watch history.
Browse picks, see why each one was picked, and send what you want to Radarr, Sonarr or Seerr.
Nothing is ever requested automatically.

Sibling app for music: [Recordhound](https://github.com/ShadeRF/recordhound).

<p align="center"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/filmhound-for-you.jpg" alt="For You: top picks with the titles that led to each, then rows like Because you watched Parasite"></p>

<table>
  <tr>
    <td width="50%"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/filmhound-details.jpg" alt="A title page: ratings, why it was picked, what it shares with your taste, and where to watch"><br><sub>Every title: why it was picked, what it shares with your taste, and one click to add it to Radarr or Sonarr.</sub></td>
    <td width="50%"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/filmhound-all-picks.jpg" alt="All Picks: every pick with filters"><br><sub>All Picks: everything, with filters for genre, decade, rating, runtime, language and your services.</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/filmhound-rate.jpg" alt="The Rate page: rate what you've watched, one title at a time"><br><sub>Rate what you've watched; ratings on titles in Plex are saved to Plex.</sub></td>
    <td width="50%" align="center"><img src="https://raw.githubusercontent.com/ShadeRF/unraid-templates/main/screenshots/filmhound-phone.jpg" width="45%" alt="For You on a phone"><br><sub>Works on your phone.</sub></td>
  </tr>
</table>

<sub>Screenshots show a demo library.</sub>

## Why Filmhound?

I tried a lot of recommendation apps, and none of them gave me suggestions that truly matched what was in my library or what I'd been watching. They leaned on what's popular, what a service wants to promote, or what everyone else is watching.

So I built my own. Filmhound starts from your Plex library and watch history and works outward from there:

- **Your library comes first.** What you own, watch and rate, in Plex and on Letterboxd or Trakt, shapes your taste. Ratings count most.
- **Every pick explains itself:** the titles that led to it, and what it shares with your taste.
- **You can tune it.** In Settings you choose how much titles you own but haven't watched count, how obscure picks can be and how many candidates get scored. You can also stop whole libraries, genres or single titles from shaping your picks.

## Install

**Unraid:** search for **Filmhound** in Community Apps. Setup is covered in [the Unraid guide](https://github.com/ShadeRF/unraid-templates/blob/main/docs/filmhound.md).

**Docker anywhere else:**

```yaml
services:
  filmhound:
    image: ghcr.io/shaderf/filmhound:latest
    container_name: filmhound
    restart: unless-stopped
    ports:
      - "8788:8788"
    volumes:
      - ./filmhound:/data
    environment:
      TZ: America/New_York   # used for the nightly sync time
```

Open `http://YOUR-SERVER:8788`. A short setup walkthrough opens on first start: connect Plex, add a TMDB key, and the first sync begins. A Getting started checklist on the home page then points to the optional extras.
The first sync looks up every title on TMDB; a few thousand titles take a few minutes, and later syncs are quick.
The container runs as `99:100` by default (`PUID`, `PGID`, `UMASK` change that).

**Versions:** the end of the image name picks which builds you get.

| Tag | What you get |
|---|---|
| `:latest` | Stable releases. Recommended. |
| `:nightly` | Built each night from the newest code, when there's anything new. New features first, and occasionally a bug. |
| `:0.10.0` or `:0.10` | One exact release (or the newest fix in that line). Use it to stay put or to roll back if a release causes trouble. |
| `:edge` | Test builds, published whenever a change is being tried out, sometimes several times a day. Expect rough edges; only use it if you're helping test. |

Your settings and data carry over when you switch, so you can move between them any time.

## What you need

- A Plex server with movie or TV libraries, and its token (Plex Web → any title → ⋯ → Get Info → View XML; the token is the `X-Plex-Token=` value in the address)
- A free TMDB API key from [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
- Optional: Seerr (Overseerr or Jellyseerr), Radarr, Sonarr, Letterboxd, Trakt

## What's in it

- **For You:** *Top picks*, *Because you watched…*, *Because you have…*, themed rows (*More neo-noir*, a director you return to), *Streaming on your services*, *Already in your library, not watched yet*, *Finish what you started* (missing films from collections and directors you own), *Coming soon for you* (unreleased movies and shows that fit your taste), *Hidden gems*, *Recent releases* and *Classics*. Movies and TV each get their own.
- **All Picks:** every scored pick with filters for genres (in or out), years, rating, runtime, language, network and your streaming services. It keeps scoring deeper as you scroll.
- **Up next:** *Collections* (films missing from collections and directors you own) and *Coming soon* (unreleased movies and shows that fit your taste).
- **Your Library:** what you own but haven't watched, ranked by your taste. One click sends the top 50 to a Plex collection.
- **Watchlist:** titles you've saved, with what you already own filtered out on the home page.
- **Rate:** rate what you've watched, one title at a time (keys 1–5; hold Shift for a half star, so Shift+4 is 3½). Ratings on titles in Plex are saved to Plex.
- **History:** what you've seen and rated, hidden, requested in Seerr or added to Radarr and Sonarr. Restore anything from here.
- **Title pages:** trailer, where to stream (JustWatch data via TMDB), IMDb and TMDB ratings, cast and crew with photos, and why it was picked. Add to Radarr or Sonarr, request through Seerr (which respects your Seerr blocklist), save to your watchlist, mark seen, hide, or open *More like this*.
- **People:** any actor or director's work, with options to hide what you own or have seen.
- **Search** for any movie, show or person.
- **Watch history from elsewhere:** Letterboxd and Trakt, and their ratings can optionally be copied into Plex. For Letterboxd, upload your export once (letterboxd.com → Settings → Data → Export your data) for your full history; after that, your diary feed is read on every sync. The feed alone only has your latest ~50 entries.
- **Don't learn from:** keep kids' movies, someone else's shows, whole libraries (set them to *Own only* on the Plex card) or any genre in your library without them shaping your picks.
- **Settings page** for every connection, with a **Test** button for each, and optional sign-in.

## How it picks

1. **Your taste:** everything in your Plex library. Star ratings count most (4–5 stars pull toward, 1–2 push away), watched counts more than owned, recent watches get a boost, and owned-but-unwatched still counts (adjustable). Ratings from Filmhound, Letterboxd and Trakt count too.
2. **Candidates:** each title's TMDB recommendations, plus similar titles at half weight.
3. **Taste match:** a profile of the directors, writers, cinematographers, composers, cast, keywords, genres, decades, languages, networks and franchises you return to.
4. **Quality:** IMDb ratings from IMDb's official dataset, weighted by vote count, falling back to TMDB.

Score = 45% "your titles point here" + 35% taste match + 20% quality. Every pick lists the titles that led to it.

## What it connects to

| Service | Needed? | Used for |
|---|---|---|
| Plex | Yes | Your libraries, watch history and star ratings; ratings and the *Watch Next* collection are written back |
| TMDB | Yes, free key | Recommendations, credits, keywords, trailers, posters, where to stream |
| IMDb datasets | Automatic, no key | Ratings and vote counts (can be turned off) |
| Seerr, Radarr, Sonarr | Optional | Requesting and adding titles |
| Letterboxd, Trakt | Optional | Watch history and ratings from outside Plex |

Your data stays in `/data` (`filmhound.db`, `settings.json`). Filmhound only talks to the services above.

## HTTPS

Run it behind a reverse proxy (Nginx Proxy Manager, SWAG, Caddy, Traefik or `tailscale serve`) at the root of a domain or subdomain. Proxies on your own network are trusted automatically; set `TRUSTED_PROXIES` to narrow that.
Or serve HTTPS directly: set `TLS_CERT` and `TLS_KEY` to a certificate and key inside the container, such as `/data/certs/cert.pem` and `/data/certs/key.pem`.
Turn on sign-in in Settings before reaching it from outside your home.

## Tips

- Rating things is the biggest lever. The **Rate** page makes it quick.
- Too many obscure picks? Raise *Minimum TMDB votes*. Too mainstream? Lower it and sort by *Deep cuts*.
- Every setting can also come from an environment variable with the same name (`PLEX_URL`, `TMDB_API_KEY`…). Anything saved on the Settings page wins.
- Delete `filmhound.db` and `settings.json` in your data folder to start fresh.

## About the name

Filmhound and Recordhound are named for Daisy, my beagle and the hound of my life. She's gone now, but she always had a nose for good things, and I like to think these apps carry a little of that.

## Source and license

The source code is at [github.com/ShadeRF/filmhound](https://github.com/ShadeRF/filmhound). Bug reports and ideas are welcome as [issues](https://github.com/ShadeRF/filmhound/issues); see [CONTRIBUTING](https://github.com/ShadeRF/filmhound/blob/main/CONTRIBUTING.md) before sending code.

Filmhound is free software under the [GNU Affero General Public License v3.0](https://github.com/ShadeRF/filmhound/blob/main/LICENSE). You can use, study, change and share it. Anything built from it must stay open under the same license, including versions offered to other people over a network.

The name *Filmhound* and its logo and icons are not covered by that license. If you publish a changed version, give it a different name and its own logo, and don't present it as the original.

Copyright © 2026 ShadeRF.
