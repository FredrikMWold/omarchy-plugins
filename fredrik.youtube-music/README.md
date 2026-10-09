# YouTube Music

An Omarchy bar widget for YouTube Music playback and account browsing.
It includes the personalized Speed dial surface exposed by YouTube Music as
the authenticated `Listen again` home shelf.

The widget controls a dedicated Chrome YouTube Music player kept on the hidden
`special:youtube-music` workspace. This avoids stream extraction delays while
keeping the browser out of normal workspaces. Playlists, liked songs, albums,
artists, and search use [`ytmusicapi`](https://ytmusicapi.readthedocs.io/) with
the same authenticated YouTube Music session.

## Setup

1. Enable the widget and click it.
2. Select **Start account setup**.
3. Ensure Chrome is signed in at `music.youtube.com`.
4. Setup imports only YouTube-domain cookies from the local Chrome profile and
   keyring, creates the authenticated session, and validates it automatically.

The setup installs Node.js, Puppeteer, `python-ytmusicapi`, and
`python-browser-cookie3` through Omarchy package commands. Google Chrome or
Chromium must also be installed. If automatic Chrome import fails, the terminal
offers manual request-header setup as a fallback. Credentials are stored with
private permissions under:

```text
~/.config/fredrik.youtube-music/
```

They are never stored in the plugin repository.

If the imported browser session expires, the widget marks the account as
**Reconnect** and returns to the setup panel. Run account setup again to import
the current signed-in Chrome session.

## Playback

Choose songs, albums, and playlists in the panel to play them through one
reusable hidden browser. Playlist and Speed Dial radio context is preserved so
previous and next use the selected queue. Repeated selections navigate inside
the existing YouTube Music app instead of reloading the page, keeping song
switches fast. The item selected through the plugin remains highlighted when
moving between tabs; keyboard navigation has a separate cursor and does not
automatically select the first row.
The Now playing panel also includes start-from-beginning and repeat controls;
repeat cycles through off, playlist, and current song. A progress slider shows
elapsed and total time and can seek within the current song. Playback metadata
comes from Chrome's Media Session state. Artist-only entries are shown for
browsing context but are not directly playable.

Audio quality follows YouTube Music's **Playback > Audio quality** account
setting and adaptive delivery. Set that setting to **High** to request the
highest tier available to the account; YouTube can still adapt the delivered
stream when network conditions require it.

This integration relies on YouTube Music's unofficial internal API through
`ytmusicapi`; Google can change that API without notice.
