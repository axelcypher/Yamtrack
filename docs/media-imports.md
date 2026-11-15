# Media Imports

Most media import sources can be used directly by entering your username or importing via CSV without any additional configuration. This guide covers only those services that may require special setup for certain scenarios.

!!! note
    The import process matches your data with the IDs from the sources that Yamtrack uses. Each media type uses different sources, so make sure to have them properly configured. See [Media Sources](env-variables.md#media-sources) for more information.

## Trakt

### Public Profile Import

If you have a public Trakt profile, you can import your media by simply entering your public username.

**Optional Configuration:**

| Environment Variable | Description                                                                                                             |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `TRAKT_API`          | Your Trakt Client ID. A default value is provided, but you may want to use your own if you encounter rate limit issues. |

### Private Profile Import (OAuth)

For private Trakt profiles, you'll need to set up OAuth authentication:

1. Create a Trakt application at [Trakt API Apps](https://trakt.tv/oauth/applications)
2. Set the **Redirect URI** to: `https://your_domain.com/import/trakt/private`
3. Configure the following environment variables:

| Environment Variable | Description              |
| -------------------- | ------------------------ |
| `TRAKT_API`          | Your Trakt Client ID     |
| `TRAKT_API_SECRET`   | Your Trakt Client Secret |

---

## Simkl

Create a SIMKL application: <https://simkl.com/settings/developer/new/custom-search/>

### Configuration

| Environment Variable | Description                                                                                                                                                  |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `SIMKL_ID`           | Simkl API key for importing media. A default key is provided, but you can obtain your own at [Simkl Developer](https://simkl.com/settings/developer/).       |
| `SIMKL_SECRET`       | Simkl API secret for importing media. A default secret is provided, but you can obtain your own at [Simkl Developer](https://simkl.com/settings/developer/). |

---

## AniList

Entries are imported with their MyAnimeList ID when AniList links one, otherwise they are imported from AniList.

### Public Profile Import

If you have a public AniList profile, you can import your media by simply entering your username.

### Private Profile Import (OAuth)

For private AniList profiles, you'll need to set up OAuth authentication:

1. Go to <https://anilist.co/settings/developer> and create a new application
2. Set the **Redirect URI** to: `https://your_domain.com/import/anilist/private`
3. Configure the following environment variables:

| Environment Variable | Description                |
| -------------------- | -------------------------- |
| `ANILIST_ID`         | Your AniList Client ID     |
| `ANILIST_SECRET`     | Your AniList Client Secret |

## Steam

### Configuration

Steam import requires a Steam API key and your Steam ID 64.

1. Get your Steam API key at <https://steamcommunity.com/dev/apikey>
2. Configure the following environment variable:

| Environment Variable | Description        |
| -------------------- | ------------------ |
| `STEAM_API_KEY`      | Your Steam API key |

### Import Requirements

- **Steam ID 64**: Instead of your Steam username, you must provide your Steam ID 64. You can find it on your Steam account details page under your username.
- **Profile Visibility**: Your Steam profile must be public if the API key you are using is not linked to the Steam ID you are requesting.

## Twitch data export (yamtrack-plus)

Imports your Twitch watch time once, as history. Request your data on twitch.tv
(Settings, Security and Privacy, "Download your data") and upload the zip
archive, or just its `request/site_history/minute_watched.csv`.

- Both known layouts of `minute_watched.csv` are read: the per-minute rows
  (`time`/`time_utc`, `channel`, `channel_id`, `minutes_logged`, `game`,
  `platform`, ...) and the daily rows (`day`, `channel_name`,
  `minutes_watched_unadjusted`, `platform`, `player`, `game_name`,
  `content_mode`).
- Minutes are summed per channel, day, category and platform. Each sum becomes
  a usage event with its category (shown in the usage statistics and on the
  channel page) and is added to the channel's Twitch entry.
- Only watching **before your first Twitch session tracked by a client**
  (Cavea or the browser extension) is imported, so nothing is counted twice.
  Rows that only carry a day are imported when the day lies before the day of
  that first session.
- Importing again replaces what the previous Twitch import added. The import
  mode (new/overwrite) makes no difference.
- Rows of the daily layout only name the channel login. Logins are matched to
  channels you already track; to look up other channels, set
  `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` (a Twitch app from
  <https://dev.twitch.tv/console/apps>). Channels that can't be resolved are
  listed in the import result.

## YouTube (yamtrack-plus)

There is no YouTube import: the Google Takeout watch history lists the videos
you opened, but not how long you watched them. YouTube watch time is tracked
per channel by the browser extension (yamtrack-tracker) from the moment it is
installed. No YouTube API key is needed; channel names and avatars come from
the extension, and the channel search only finds channels you have watched.

## Yamtrack CSV format

Use this format to bulk-import media into YamTrack. Every row represents a single media instance.

The order of rows matters for TV data: import `tv` rows first, then `season` rows, then `episode` rows. Episodes depend on their parent season, and seasons depend on their parent TV show.

| Column Title   | Required? | Example Value                                  | Notes                                                                                                                                                                           |
| -------------- | --------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| media_id       | No        | 12345                                          | ID of the item on the chosen provider. Must be unique per `(source, media_type)`. Leave blank to let YamTrack fetch the title from the provider automatically with the `title`. |
| source         | **Yes**   | tmdb                                           | One of: `tmdb`, `mal`, `anilist`, `mangaupdates`, `igdb`, `openlibrary`, `hardcover`, `comicvine`, `manual`.                                                                    |
| media_type     | **Yes**   | movie                                          | One of: `tv`, `season`, `episode`, `movie`, `anime`, `manga`, `game`, `book`, `comic`. For TV data, rows must be ordered as `tv`, then `season`, then `episode`.                |
| title          | No        | Inception                                      | Leave blank to let YamTrack fetch the title from the provider automatically with the `media_id`.                                                                                |
| image          | No        | <https://image.tmdb.org/t/p/w500/qmDpIH...jpg> | Public poster/cover URL. Leave blank to auto-fetch from the provider automatically with the `media_id` or `title`.                                                              |
| season_number  | Cond.     | 2                                              | Required when `media_type = season`; also required (together with `episode_number`) when `media_type = episode`.                                                                |
| episode_number | Cond.     | 5                                              | Required when `media_type = episode`; ignored otherwise.                                                                                                                        |
| score          | No        | 8.5                                            | Decimal 0–10 (stored as a 100-point integer internally).                                                                                                                        |
| status         | **Yes**   | Completed                                      | One of: `Completed`, `In progress`, `Planning`, `Paused`, `Dropped`. Capitalisation must match.                                                                                 |
| notes          | No        | Watched in cinema                              | Free-text.                                                                                                                                                                      |
| start_date     | No        | 2023-01-16 03:56:13+00:00                      | Full ISO-8601 **timestamp with timezone** `YYYY-MM-DD HH:MM:SS±HH:MM`.                                                                                                          |
| end_date       | No        | 2023-02-10 22:15:00+00:00                      | Same format as `start_date`.                                                                                                                                                    |
| progress       | No        | 10                                             | Numeric progress (e.g., chapters, pages, minutes). Ignored for tv and season because progress is tracked with episode rows.                                                     |
