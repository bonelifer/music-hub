/* music-hub page script.
 *
 * Builds each service's view from the /api/... routes, runs the MPD
 * controls and this browser's own player, and steps the theme button.
 * Text from services is always set with textContent, never as HTML.
 */
"use strict";

const THEME_TEXT = { auto: "Auto (follows your system)", light: "Light", dark: "Dark" };
const THEME_NAME = { auto: "Auto", light: "Light", dark: "Dark" };
const NEXT_THEME = { auto: "light", light: "dark", dark: "auto" };
// Rows shown before a "Show more" button, so big libraries stay quick.
const PAGE_ROWS = 200;
const MPD_POLL_MS = 5000;

let config = null;
let theme = "auto";

/* ---- small helpers ------------------------------------------------- */

function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Music-Hub": "1" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error("music-hub did not answer. Is it still running?");
  }
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `Request failed (HTTP ${response.status})`);
  }
  return data;
}

function say(message, isError = false) {
  const status = document.getElementById("status");
  status.textContent = message;
  status.classList.toggle("error", isError);
  status.hidden = !message;
}

/*
 * Run an action from a button and report success or failure. The button
 * is marked busy with aria-disabled rather than disabled: disabling a
 * focused button throws keyboard focus back to the top of the page.
 */
async function run(button, action, success) {
  if (button) {
    if (button.getAttribute("aria-disabled") === "true") return;
    button.setAttribute("aria-disabled", "true");
  }
  try {
    const result = await action();
    const message = typeof success === "function" ? success(result) : success;
    if (message) say(message);
  } catch (error) {
    say(error.message, true);
  } finally {
    if (button) button.removeAttribute("aria-disabled");
  }
}

function heading(text) {
  document.getElementById("heading").textContent = text;
  document.title = text === "music-hub" ? "music-hub" : `${text} - music-hub`;
}

/* ---- theme ---------------------------------------------------------- */

function showTheme() {
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  const text = `Theme: ${THEME_TEXT[theme]}. Click for ${THEME_NAME[NEXT_THEME[theme]]}.`;
  document.getElementById("theme-label").textContent = text;
  document.getElementById("theme-tip").textContent = text;
}

function setUpTheme() {
  document.getElementById("theme-button").addEventListener("click", (event) => {
    run(event.currentTarget, async () => {
      const result = await api("/api/theme", { theme: NEXT_THEME[theme] });
      theme = result.theme;
      showTheme();
    });
  });
}

/* ---- this browser's player ------------------------------------------ */

const browserPlayer = {
  queue: [],
  current: null,

  /* Replace what is playing with a list of {title, src, artist, artwork}. */
  play(items) {
    this.queue = items.slice(1);
    this.start(items[0]);
  },

  start(item) {
    const audio = document.getElementById("audio");
    this.current = item || null;
    if (!item) {
      audio.removeAttribute("src");
      audio.load();
      this.show();
      return;
    }
    audio.src = item.src;
    audio.play().catch((error) => {
      if (error.name !== "AbortError") {
        say(`This browser could not play "${item.title}": ${error.message}`, true);
      }
    });
    this.show();
  },

  next() {
    this.start(this.queue.shift());
  },

  clear() {
    this.queue = [];
    this.start(null);
  },

  show() {
    const item = this.current;
    // The bar only takes space while something is playing here.
    document.getElementById("browser-player").hidden = !item;
    document.getElementById("browser-now").textContent = item
      ? [item.title, item.artist].filter(Boolean).join(" - ")
      : "Not playing";
    document.getElementById("browser-left").textContent = this.queue.length
      ? `${this.queue.length} more queued` : "";
    document.getElementById("browser-next").disabled = !this.queue.length;
    // Media Session puts the track on phone lock screens and makes
    // headset and keyboard media keys work.
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = item ? new MediaMetadata({
        title: item.title || "",
        artist: item.artist || "",
        artwork: item.artwork ? [{ src: item.artwork }] : [],
      }) : null;
    }
  },
};

function setUpBrowserPlayer() {
  const audio = document.getElementById("audio");
  audio.addEventListener("ended", () => browserPlayer.next());
  audio.addEventListener("error", () => {
    if (!browserPlayer.current) return;
    say(`This browser could not play "${browserPlayer.current.title}". `
      + "Its stream format may not be supported here; try MPD instead.", true);
  });
  document.getElementById("browser-next").addEventListener("click", () => browserPlayer.next());
  document.getElementById("browser-clear").addEventListener("click", () => browserPlayer.clear());
  if ("mediaSession" in navigator) {
    const session = navigator.mediaSession;
    session.setActionHandler("play", () => audio.play());
    session.setActionHandler("pause", () => audio.pause());
    session.setActionHandler("nexttrack", () => browserPlayer.next());
  }
  browserPlayer.show();
}

/* ---- MPD controls ---------------------------------------------------- */

let mpdState = "stop";

/* Title-case a part written in all capitals, as the TuneIn display script does. */
function titleCase(value) {
  return value.split(/(\s+[-–—/]\s+)/u).map((part) => {
    if (part === part.toLowerCase() || part !== part.toUpperCase()) return part;
    return part.toLowerCase().replace(/(^|[^\p{L}\p{N}'’])(\p{L})/gu,
      (_, prefix, first) => prefix + first.toUpperCase());
  }).join("");
}

/*
 * Turn an iHeartRadio stream title into readable text. The same rules as
 * myMPD's iheart-display.js: ads read "Ad Content", station filler reads
 * "Channel Content", and songs read "Artist - Title".
 */
function iheartTitle(value) {
  const track = value.match(/^\s*(?:currently playing track:\s*)?title="(.*?)",\s*artist="([^"]*)",\s*url="/isu);
  const prefix = value.match(/^(?:.*?\s+)?-\s+(?=\w+=")/u)?.[0] ?? "";
  const metadata = track
    ? (track[1] + " " + value.slice(track[0].length)).replace(/\\"/gu, '"')
    : value.slice(prefix.length).trim();
  if (!track && !/^(?:\w+="[^"]*"\s*)+$/u.test(metadata)) return value;
  const fields = new Map(Array.from(metadata.matchAll(/(?:^|\s)(\w+)="([^"]*)"/gu),
    ([, key, field]) => [key.toLowerCase(), field]));
  const title = (fields.get("text") ?? track?.[1] ?? "").trim();
  const artist = track?.[2].trim();
  const spot = fields.get("song_spot")?.toUpperCase();
  if (fields.has("adcontext") || fields.has("adswizzcontext") || spot === "A") return "Ad Content";
  if (/^spot block (?:start|end)$/iu.test(title)) return "Channel Content";
  if (track && title && artist && artist.toLowerCase() !== "null"
      && (!spot || spot === "M" || spot === "F")) return `${artist} - ${title}`;
  if (!track && spot === "T" && title) return title;
  return !track && spot === "M" && title
    ? (prefix.trim() === "-" ? "" : prefix) + title : "Channel Content";
}

/* What the MPD bar shows for the current song. */
function mpdText(status) {
  if (status.state === "stop") return "Stopped";
  // Stations queued by myMPD or music-hub carry their name in the URI.
  const tags = new URLSearchParams(status.file.split("#")[1] || "");
  const station = tags.get("name") || "";
  let title = status.title;
  if (tags.has("mympd-iheart") && title) title = iheartTitle(title);
  if (title && (tags.has("mympd-tunein") || tags.has("mympd-iheart"))) title = titleCase(title);
  const song = [title, status.artist].filter(Boolean).join(" - ");
  if (station) return song && song !== station ? `${station}: ${song}` : station;
  return song || status.name || status.file || "Playing";
}

async function refreshMpd() {
  if (document.hidden) return;
  try {
    const status = await api("/api/mpd/status");
    mpdState = status.state;
    const text = mpdText(status);
    document.getElementById("mpd-now").textContent =
      status.state === "pause" ? `Paused: ${text}` : text;
    const toggle = document.getElementById("mpd-toggle");
    toggle.textContent = status.state === "play" ? "Pause" : "Play";
  } catch (error) {
    document.getElementById("mpd-now").textContent = error.message;
  }
}

function setUpMpd() {
  for (const button of document.querySelectorAll("[data-mpd]")) {
    button.addEventListener("click", () => {
      let action = button.dataset.mpd;
      if (button.id === "mpd-toggle") {
        action = mpdState === "play" ? "pause" : mpdState === "pause" ? "resume" : "play";
      }
      run(button, async () => {
        await api("/api/mpd/control", { action });
        await refreshMpd();
      });
    });
  }
  refreshMpd();
  setInterval(refreshMpd, MPD_POLL_MS);
  document.addEventListener("visibilitychange", refreshMpd);
}

/* ---- play buttons ---------------------------------------------------- */

/*
 * A button whose visible text is followed by the item's name for screen
 * readers ("Play on MPD: Abbey Road"). The visible words stay at the start
 * of the name, so voice control finds the button by what it shows.
 */
function actionButton(text, label, attributes) {
  return el("button", { type: "button", ...attributes }, [
    text, el("span", { class: "sr-only", text: `: ${label}` }),
  ]);
}

/*
 * Buttons for one playable thing. options:
 *   label   - what is played, for screen readers ("Abbey Road")
 *   mpd     - async (play) => message; play=false appends without playing
 *   browser - async () => [{title, src, artist, artwork}]
 * Only the playback modes turned on in music-hub.cfg get buttons.
 */
function playButtons({ label, mpd, browser, queue = true }) {
  const buttons = [];
  if (config.playback.mpd && mpd) {
    buttons.push(actionButton("Play on MPD", label, {
      class: "small primary",
      onclick: (event) => run(event.currentTarget, () => mpd(true), (message) => message),
    }));
    if (queue) {
      buttons.push(actionButton("Add to MPD queue", label, {
        class: "small",
        onclick: (event) => run(event.currentTarget, () => mpd(false), (message) => message),
      }));
    }
  }
  if (config.playback.browser && browser) {
    buttons.push(actionButton("Play here", label, {
      class: "small", title: "Play in this browser",
      onclick: (event) => run(event.currentTarget, async () => {
        const items = await browser();
        if (!items.length) throw new Error("Nothing to play");
        browserPlayer.play(items);
      }),
    }));
  }
  return el("div", { class: "actions" }, buttons);
}

/* A list of rows with a filter box and "Show more" for long lists. */
function rowList(items, makeRow, { filterLabel } = {}) {
  const wrapper = el("div");
  const list = el("ul", { class: "rows" });
  let shown = PAGE_ROWS;
  let matches = items;
  const more = el("button", {
    type: "button", class: "small", text: "Show more",
    onclick: () => { shown += PAGE_ROWS; draw(); },
  });
  const count = el("p", { class: "muted", "aria-live": "polite" });

  function draw() {
    list.replaceChildren(...matches.slice(0, shown).map(makeRow));
    more.hidden = matches.length <= shown;
    count.textContent = matches.length === items.length
      ? `${items.length} items` : `${matches.length} of ${items.length} items`;
  }

  if (filterLabel && items.length > 20) {
    const id = `filter-${Math.random().toString(36).slice(2)}`;
    const input = el("input", {
      id, type: "search", autocomplete: "off",
      oninput: () => {
        const needle = input.value.trim().toLocaleLowerCase();
        matches = needle
          ? items.filter((item) => item.name.toLocaleLowerCase().includes(needle))
          : items;
        shown = PAGE_ROWS;
        draw();
      },
    });
    wrapper.append(el("div", { class: "toolbar" }, [
      el("label", { for: id, text: filterLabel }), input,
    ]));
  }
  if (!items.length) {
    wrapper.append(el("p", { class: "muted", text: "Nothing here." }));
    return wrapper;
  }
  wrapper.append(count, list, more);
  draw();
  return wrapper;
}

/* A search form: label, input, and a button that calls onSearch(text). */
function searchForm(labelText, onSearch, placeholder = "") {
  const id = `search-${Math.random().toString(36).slice(2)}`;
  const input = el("input", { id, type: "search", maxlength: "100", required: true, placeholder });
  const button = el("button", { type: "submit", class: "primary", text: "Search" });
  return el("form", {
    class: "toolbar", role: "search",
    onsubmit: (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (text) run(button, () => onSearch(text));
    },
  }, [el("label", { for: id, text: labelText }), input, button]);
}

/* ---- iBroadcast ------------------------------------------------------ */

const IBROADCAST_TABS = [
  ["artists", "Artists"], ["albums", "Albums"], ["genre", "Genres"], ["year", "Years"],
  ["tag", "Tags"], ["playlist", "Playlists"], ["recent", "Recently added"],
  ["rated", "Top rated"], ["played", "Most played"], ["search", "Search"],
  ["random", "Random playlist"],
];

function ibroadcastStream(id) {
  return `/api/ibroadcast/stream/${encodeURIComponent(id)}`;
}

function trackItems(tracks) {
  return tracks.map((track) => ({ title: track.name, src: ibroadcastStream(track.id) }));
}

function shuffle(items) {
  const copy = items.slice();
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const other = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[other]] = [copy[other], copy[index]];
  }
  return copy;
}

/* Play buttons for an iBroadcast selection (track, album, or a group). */
function ibroadcastButtons(kind, value, label, loadTracks) {
  return playButtons({
    label,
    mpd: async (play) => {
      const result = await api("/api/ibroadcast/queue", { kind, value, play });
      const songs = result.queued === 1 ? "1 song" : `${result.queued} songs`;
      return play ? `Playing ${songs} on MPD` : `Added ${songs} to the MPD queue`;
    },
    browser: async () => {
      if (kind === "track") return [{ title: label, src: ibroadcastStream(value) }];
      const tracks = await loadTracks();
      const ordered = kind === "album" || kind === "playlist" ? tracks : shuffle(tracks);
      return trackItems(ordered);
    },
  });
}

function trackRow(track) {
  return el("li", {}, [
    el("span", { class: "name", text: track.name }),
    ibroadcastButtons("track", String(track.id), track.name),
  ]);
}

function ibroadcastView(container) {
  const tabs = el("ul", { class: "tabs", "aria-label": "iBroadcast views" });
  const crumbs = el("ol", { class: "crumbs", "aria-label": "Where you are" });
  const body = el("div");
  container.append(tabs, el("nav", { "aria-label": "Breadcrumbs" }, crumbs), body);
  // Each level: {title, render(target)}. Clicking a crumb returns to it.
  let stack = [];

  function drawCrumbs() {
    crumbs.replaceChildren(...stack.map((level, index) => el("li", {}, [
      index === stack.length - 1
        ? el("span", { "aria-current": "location", text: level.title })
        : el("button", {
          type: "button", class: "small", text: level.title,
          onclick: () => { stack = stack.slice(0, index + 1); show(); },
        }),
    ])));
  }

  async function show() {
    drawCrumbs();
    const level = stack[stack.length - 1];
    body.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
    try {
      const content = await level.render();
      body.replaceChildren(content);
    } catch (error) {
      body.replaceChildren(el("p", { class: "notice error", text: error.message }));
    }
  }

  function open(level) {
    stack.push(level);
    show();
  }

  function openButton(text, level) {
    return el("button", { type: "button", text, onclick: () => open(level) });
  }

  function albumLevel(album) {
    const load = () => api(`/api/ibroadcast/tracks?album=${encodeURIComponent(album.id)}`);
    return {
      title: album.name,
      render: async () => {
        const tracks = await load();
        return el("div", {}, [
          el("div", { class: "toolbar" }, [
            ibroadcastButtons("album", String(album.id), `the album ${album.name}`, load),
          ]),
          rowList(tracks, trackRow),
        ]);
      },
    };
  }

  function albumRow(album) {
    return el("li", {}, [
      el("span", { class: "name" }, openButton(album.name, albumLevel(album))),
      ibroadcastButtons("album", String(album.id), `the album ${album.name}`,
        () => api(`/api/ibroadcast/tracks?album=${encodeURIComponent(album.id)}`)),
    ]);
  }

  function artistLevel(artist) {
    return {
      title: artist.name,
      render: async () => rowList(
        await api(`/api/ibroadcast/albums?artist=${encodeURIComponent(artist.id)}`),
        albumRow, { filterLabel: "Filter albums" }),
    };
  }

  function groupLevel(mode, group) {
    const load = () => api(`/api/ibroadcast/browse?mode=${mode}&value=${encodeURIComponent(group.id)}`);
    const what = mode === "playlist" ? `the playlist ${group.name}` : group.name;
    return {
      title: group.name,
      render: async () => {
        const tracks = await load();
        return el("div", {}, [
          el("p", { class: "muted", text: mode === "playlist"
            ? "Plays in playlist order." : "Plays shuffled." }),
          el("div", { class: "toolbar" }, [ibroadcastButtons(mode, String(group.id), what, load)]),
          rowList(tracks, trackRow, { filterLabel: "Filter songs" }),
        ]);
      },
    };
  }

  function searchRow(item) {
    const [kind, id] = item.id.split(":");
    const name = item.name.replace(/ \((track|artist|album)\)$/, "");
    if (kind === "tracks") return trackRow({ id, name: item.name });
    if (kind === "albums") return albumRow({ id, name });
    return el("li", {}, [
      el("span", { class: "name" }, openButton(item.name, artistLevel({ id, name }))),
    ]);
  }

  function randomForm() {
    const name = el("input", { id: "random-name", maxlength: "100", required: true });
    const count = el("input", {
      id: "random-count", type: "number", min: "1", max: "100", required: true,
      value: String(config.playlist.size),
    });
    const destinations = config.playback.mpd
      ? [["mpd", "MPD"], ["ibroadcast", "iBroadcast"], ["both", "Both"]]
      : [["ibroadcast", "iBroadcast"]];
    const destination = el("select", { id: "random-destination" }, destinations.map(
      ([value, text]) => el("option", {
        value, text, selected: value === config.playlist.destination,
      })));
    const button = el("button", { type: "submit", class: "primary", text: "Create playlist" });
    return el("form", {
      class: "panel",
      onsubmit: (event) => {
        event.preventDefault();
        run(button, () => api("/api/ibroadcast/random", {
          name: name.value, count: Number(count.value), destination: destination.value,
        }), (result) => `Saved ${result.saved} songs to ${
          { mpd: "MPD", ibroadcast: "iBroadcast", both: "MPD and iBroadcast" }[result.destination]}`);
      },
    }, [
      el("p", { class: "muted", text: "Picks distinct random songs from your library. "
        + "Both destinations get the same songs in the same order." }),
      el("div", { class: "toolbar" }, [el("label", { for: "random-name", text: "Name" }), name]),
      el("div", { class: "toolbar" }, [el("label", { for: "random-count", text: "Songs" }), count]),
      el("div", { class: "toolbar" }, [
        el("label", { for: "random-destination", text: "Save to" }), destination]),
      button,
    ]);
  }

  function tabLevel(key, label) {
    if (key === "artists") {
      return { title: label, render: async () => rowList(await api("/api/ibroadcast/artists"),
        (artist) => el("li", {}, [
          el("span", { class: "name" }, openButton(artist.name, artistLevel(artist))),
        ]), { filterLabel: "Filter artists" }) };
    }
    if (key === "albums") {
      return { title: label, render: async () => rowList(await api("/api/ibroadcast/albums"),
        albumRow, { filterLabel: "Filter albums" }) };
    }
    if (["genre", "year", "tag", "playlist"].includes(key)) {
      return { title: label, render: async () => rowList(
        await api(`/api/ibroadcast/browse?mode=${key}`),
        (group) => el("li", {}, [
          el("span", { class: "name" }, openButton(group.name, groupLevel(key, group))),
          ibroadcastButtons(key, String(group.id), group.name,
            () => api(`/api/ibroadcast/browse?mode=${key}&value=${encodeURIComponent(group.id)}`)),
        ]), { filterLabel: `Filter ${label.toLowerCase()}` }) };
    }
    if (key === "search") {
      return { title: label, render: async () => {
        const results = el("div");
        return el("div", {}, [
          searchForm("Songs, artists, albums", async (text) => {
            const rows = await api(`/api/ibroadcast/browse?mode=search&value=${encodeURIComponent(text)}`);
            results.replaceChildren(rowList(rows, searchRow));
          }),
          results,
        ]);
      } };
    }
    if (key === "random") return { title: label, render: async () => randomForm() };
    return { title: label, render: async () => rowList(
      await api(`/api/ibroadcast/browse?mode=${key}`), trackRow, { filterLabel: "Filter songs" }) };
  }

  const tabButtons = IBROADCAST_TABS.map(([key, label]) => el("button", {
    type: "button", "aria-pressed": "false", text: label,
    onclick: (event) => {
      for (const button of tabButtons) button.setAttribute("aria-pressed", "false");
      event.currentTarget.setAttribute("aria-pressed", "true");
      stack = [tabLevel(key, label)];
      show();
    },
  }));
  tabs.replaceChildren(...tabButtons.map((button) => el("li", {}, button)));
  tabButtons[0].click();
}

/* ---- radio ----------------------------------------------------------- */

/* Buttons that play a station on MPD or resolve it for this browser. */
function stationButtons(service, station) {
  return playButtons({
    label: station.name,
    queue: false,
    mpd: async () => {
      const result = await api(`/api/${service}/play`, { id: station.id });
      return `Playing ${result.playing || station.name} on MPD`;
    },
    browser: async () => {
      const stream = await api(`/api/${service}/resolve/${encodeURIComponent(station.id)}`);
      return [{ title: stream.name || station.name, src: stream.url, artwork: station.image }];
    },
  });
}

function stationCard(service, station, extra = []) {
  return el("li", { class: "card" }, [
    station.image ? el("img", { src: station.image, alt: "", loading: "lazy" }) : null,
    el("span", { class: "name", text: station.name }),
    stationButtons(service, station),
    ...extra,
  ]);
}

function cardsFrom(items, makeCard) {
  if (!items.length) return el("p", { class: "muted", text: "Nothing here yet." });
  return el("ul", { class: "cards" }, items.map(makeCard));
}

async function somafmView(container) {
  container.append(el("h2", { text: "Channels" }));
  const channels = await api("/api/somafm/favorites");
  container.append(cardsFrom(channels, (station) => {
    const recent = el("ol", { class: "recent", hidden: true });
    const toggle = el("button", {
      type: "button", class: "small", text: "Recently played", "aria-expanded": "false",
      onclick: (event) => run(event.currentTarget, async () => {
        const open = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!open));
        recent.hidden = open;
        if (!open) {
          const songs = await api(`/api/somafm/recent/${encodeURIComponent(station.id)}`);
          recent.replaceChildren(...songs.slice(0, 5).map((song) => el("li", {
            text: [song.title, song.artist].filter(Boolean).join(" - "),
          })));
        }
      }),
    });
    return stationCard("somafm", station, [toggle, recent]);
  }));
}

async function tuneinView(container) {
  const favorites = el("div");
  async function loadFavorites() {
    favorites.replaceChildren(el("p", { class: "muted", text: "Loading favorites…" }));
    const stations = await api("/api/tunein/favorites");
    favorites.replaceChildren(cardsFrom(stations, (station) => stationCard("tunein", station, [
      actionButton("Remove", `${station.name} from TuneIn favorites`, {
        class: "small",
        onclick: (event) => {
          if (!window.confirm(`Remove ${station.name} from your TuneIn favorites?`)) return;
          run(event.currentTarget, async () => {
            await api("/api/tunein/favorite", { action: "remove", id: station.id });
            await loadFavorites();
          }, `Removed ${station.name} from TuneIn favorites`);
        },
      }),
    ])));
  }
  const results = el("div");
  container.append(
    el("h2", { text: "Favorites" }),
    favorites,
    el("h2", { text: "Find a station" }),
    el("p", { class: "muted", text: "TuneIn plays stations saved as favorites, "
      + "so save a station first, then play it from your favorites." }),
    searchForm("Station name or call sign", async (text) => {
      const rows = await api(`/api/tunein/search?q=${encodeURIComponent(text)}`);
      results.replaceChildren(rowList(rows, (station) => el("li", {}, [
        el("span", { class: "name", text: station.name }),
        actionButton("Save to favorites", `${station.name}, TuneIn`, {
          class: "small",
          onclick: (event) => run(event.currentTarget, async () => {
            await api("/api/tunein/favorite", { action: "add", id: station.id });
            await loadFavorites();
          }, `Saved ${station.name} to TuneIn favorites`),
        }),
      ])));
    }),
    results,
  );
  await loadFavorites();
}

async function iheartView(container) {
  const stationResults = el("div");
  const podcastResults = el("div");

  function episodeRow(episode) {
    return el("li", {}, [
      el("span", { class: "name", text: episode.name }),
      playButtons({
        label: episode.name,
        queue: false,
        mpd: async () => {
          const result = await api("/api/iheart/play-episode", { id: episode.id });
          return `Playing ${result.playing || episode.name} on MPD`;
        },
        browser: async () => {
          const found = await api(`/api/iheart/episode/${encodeURIComponent(episode.id)}`);
          return [{ title: found.name || episode.name, src: found.url }];
        },
      }),
    ]);
  }

  container.append(el("h2", { text: "Favorites" }));
  const favorites = await api("/api/iheart/favorites");
  container.append(cardsFrom(favorites, (station) => stationCard("iheart", station)));

  container.append(
    el("h2", { text: "Find a station" }),
    searchForm("Station name or call sign", async (text) => {
      const rows = await api(`/api/iheart/search?q=${encodeURIComponent(text)}`);
      stationResults.replaceChildren(rowList(rows, (station) => el("li", {}, [
        el("span", { class: "name", text: station.name }),
        stationButtons("iheart", station),
        actionButton("Save to favorites", `${station.name}, iHeartRadio`, {
          class: "small",
          onclick: (event) => run(event.currentTarget,
            () => api("/api/iheart/favorite", { id: station.id }),
            `Saved ${station.name} to iHeartRadio favorites. Reopen iHeartRadio to see it.`),
        }),
      ])));
    }),
    stationResults,
    el("h2", { text: "Podcasts" }),
    searchForm("Podcast name", async (text) => {
      const rows = await api(`/api/iheart/podcasts?q=${encodeURIComponent(text)}`);
      podcastResults.replaceChildren(rowList(rows, (podcast) => el("li", {}, [
        el("span", { class: "name" }, el("button", {
          type: "button", text: podcast.name,
          onclick: (event) => run(event.currentTarget, async () => {
            const episodes = await api(`/api/iheart/episodes/${encodeURIComponent(podcast.id)}`);
            podcastResults.replaceChildren(
              el("h3", { text: podcast.name }),
              rowList(episodes, episodeRow, { filterLabel: "Filter episodes" }),
            );
          }),
        })),
      ])));
    }),
    podcastResults,
  );
}

/* ---- navigation ------------------------------------------------------ */

const VIEWS = {
  ibroadcast: ibroadcastView,
  tunein: tuneinView,
  iheart: iheartView,
  somafm: somafmView,
};

async function route() {
  const name = location.hash.replace(/^#\/?/, "") || (config.services[0] || {}).name;
  const service = config.services.find((item) => item.name === name);
  for (const link of document.querySelectorAll("#nav a")) {
    if (link.dataset.service === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const view = document.getElementById("view");
  say("");
  if (!service) {
    heading("music-hub");
    view.replaceChildren(el("p", { class: "notice", text: config.services.length
      ? "Pick a service above." : "Every service is turned off in music-hub.cfg." }));
    return;
  }
  heading(service.label);
  const container = el("div");
  view.replaceChildren(container);
  try {
    await VIEWS[service.name](container);
  } catch (error) {
    container.append(el("p", { class: "notice error", text: error.message }));
  }
}

async function start() {
  setUpTheme();
  try {
    config = await api("/api/config");
  } catch (error) {
    say(error.message, true);
    return;
  }
  theme = config.theme;
  showTheme();
  const nav = document.getElementById("nav");
  const themeItem = nav.querySelector(".theme");
  for (const service of config.services) {
    nav.insertBefore(el("li", {}, el("a", {
      href: `#${service.name}`, "data-service": service.name, text: service.label,
    })), themeItem);
  }
  if (config.playback.mpd) {
    document.getElementById("mpd-player").hidden = false;
    setUpMpd();
  }
  if (config.playback.browser) setUpBrowserPlayer();
  window.addEventListener("hashchange", route);
  route();
}

start();
