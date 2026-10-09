#!/usr/bin/env node

import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import puppeteer from "puppeteer-core";

const execFileAsync = promisify(execFile);
const HOST = "127.0.0.1";
const PORT = Number(process.env.YOUTUBE_MUSIC_BROWSER_PORT || 43829);
const SERVICE_ID = "fredrik.youtube-music-browser";
const STANDBY_RETRY_MS = 2000;
const HOME = os.homedir();
const CONFIG_HOME =
  process.env.XDG_CONFIG_HOME || path.join(HOME, ".config");
const STATE_HOME =
  process.env.XDG_STATE_HOME || path.join(HOME, ".local", "state");
const CONFIG_DIR = path.join(CONFIG_HOME, "fredrik.youtube-music");
const STATE_DIR = path.join(STATE_HOME, "fredrik.youtube-music");
const PROFILE_DIR = path.join(STATE_DIR, "chrome-profile");
const BROWSER_PATH = path.join(CONFIG_DIR, "browser.json");
const PLAYBACK_STATE_PATH = path.join(STATE_DIR, "browser-playback.json");

let browser;
let page;
let starting;
let playGeneration = 0;
let sourceUrl = "";
let queueVideoIds = [];
let server;
let standbyTimer;
let standbyCheckRunning = false;
let shuttingDown = false;

try {
  const state = JSON.parse(fs.readFileSync(PLAYBACK_STATE_PATH, "utf8"));
  if (typeof state.sourceUrl === "string") sourceUrl = state.sourceUrl;
  if (Array.isArray(state.queueVideoIds))
    queueVideoIds = state.queueVideoIds.filter(
      (videoId) =>
        typeof videoId === "string" && /^[A-Za-z0-9_-]{6,}$/.test(videoId),
    );
} catch {}

let queueCount = queueVideoIds.length;

function chromePath() {
  const candidates = [
    process.env.CHROME_PATH,
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean);
  const result = candidates.find((candidate) => fs.existsSync(candidate));
  if (!result) throw new Error("Google Chrome or Chromium is not installed");
  return result;
}

function browserCookies() {
  const headers = JSON.parse(fs.readFileSync(BROWSER_PATH, "utf8"));
  const raw = String(headers.Cookie || headers.cookie || "");
  if (!raw) throw new Error("YouTube Music browser cookies are missing");
  return raw
    .split(";")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const separator = entry.indexOf("=");
      return {
        name: entry.slice(0, separator),
        value: entry.slice(separator + 1),
        domain: ".youtube.com",
        path: "/",
        secure: true,
      };
    })
    .filter((cookie) => cookie.name && cookie.value);
}

async function focusSnapshot() {
  try {
    const [workspaceResult, windowResult] = await Promise.all([
      execFileAsync("hyprctl", ["activeworkspace", "-j"]),
      execFileAsync("hyprctl", ["activewindow", "-j"]),
    ]);
    const workspace = JSON.parse(workspaceResult.stdout);
    const activeWindow = JSON.parse(windowResult.stdout);
    return {
      workspace: String(workspace.name || ""),
      address: String(activeWindow.address || ""),
    };
  } catch {
    return { workspace: "", address: "" };
  }
}

async function restoreFocus(snapshot) {
  const commands = [];
  if (/^[A-Za-z0-9_:+.-]+$/.test(snapshot.workspace)) {
    commands.push(
      `hl.dispatch(hl.dsp.focus({workspace='${snapshot.workspace}'}))`,
    );
  }
  if (/^0x[0-9a-f]+$/i.test(snapshot.address)) {
    commands.push(
      "hl.dispatch(hl.dsp.focus({window=" +
        `hl.get_window('address:${snapshot.address}')}))`,
    );
  }
  if (commands.length)
    await execFileAsync("hyprctl", [
      "repl",
      `${commands.join("; ")}; return true`,
    ]);
}

async function hideWindow(snapshot) {
  await new Promise((resolve) => setTimeout(resolve, 500));
  try {
    const clients = JSON.parse(
      (await execFileAsync("hyprctl", ["clients", "-j"])).stdout,
    );
    const client = clients.find((candidate) =>
      String(candidate.class || "").startsWith("chrome-music.youtube.com"),
    );
    if (!client?.address) throw new Error("YouTube Music window not found");
    if (!/^0x[0-9a-f]+$/i.test(client.address))
      throw new Error("YouTube Music window has an invalid address");
    const code = [
      "return hl.dispatch(hl.dsp.window.move({",
      "workspace='special:youtube-music',",
      `window=hl.get_window('address:${client.address}'),`,
      "silent=true}))",
    ].join("");
    await execFileAsync("hyprctl", ["repl", code]);
  } catch (error) {
    console.error(`Could not move browser to background: ${error.message}`);
  } finally {
    try {
      await restoreFocus(snapshot);
    } catch (error) {
      console.error(`Could not restore desktop focus: ${error.message}`);
    }
  }
}

async function connectExistingBrowser() {
  try {
    const [port, endpointPath] = fs
      .readFileSync(path.join(PROFILE_DIR, "DevToolsActivePort"), "utf8")
      .trim()
      .split("\n");
    if (!/^\d+$/.test(port) || !endpointPath?.startsWith("/devtools/browser/"))
      return undefined;
    return await puppeteer.connect({
      browserWSEndpoint: `ws://127.0.0.1:${port}${endpointPath}`,
    });
  } catch {
    return undefined;
  }
}

async function startBrowser() {
  const previousFocus = await focusSnapshot();
  fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  browser = await connectExistingBrowser();
  const launched = !browser;
  if (launched) {
    browser = await puppeteer.launch({
      executablePath: chromePath(),
      headless: false,
      userDataDir: PROFILE_DIR,
      defaultViewport: null,
      args: [
        "--app=https://music.youtube.com/",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--no-first-run",
        "--no-default-browser-check",
        "--ozone-platform=wayland",
      ],
    });
  }
  const pages = await browser.pages();
  page =
    pages.find((candidate) => candidate.url().includes("music.youtube.com")) ||
    pages[0] ||
    (await browser.newPage());
  page.on("dialog", async (dialog) => {
    try {
      await dialog.accept();
    } catch (error) {
      console.error(`Could not close browser dialog: ${error.message}`);
    }
  });
  await browser.defaultBrowserContext().setCookie(...browserCookies());
  if (launched) {
    await page.goto("https://music.youtube.com/", {
      waitUntil: "domcontentloaded",
      timeout: 20000,
    });
    await hideWindow(previousFocus);
  }
  browser.on("disconnected", () => {
    browser = undefined;
    page = undefined;
  });
}

async function ensureBrowser() {
  if (browser?.connected && page && !page.closed) return;
  if (!starting) {
    starting = startBrowser().finally(() => {
      starting = undefined;
    });
  }
  await starting;
}

async function play(url, selectedSourceUrl, selectedQueueVideoIds) {
  const generation = ++playGeneration;
  await ensureBrowser();
  if (generation !== playGeneration)
    throw new Error("Playback selection was superseded");
  const expectedVideoId = new URL(url).searchParams.get("v");
  const previousMedia = await page.evaluate(() => {
    const media = document.querySelector("video");
    const player = document.querySelector("#movie_player");
    return {
      videoId:
        player?.getVideoData?.()?.video_id ||
        player?.getVideoData?.()?.videoId ||
        "",
      source: media?.currentSrc || "",
    };
  });
  await page.evaluate((targetUrl) => {
    const target = new URL(targetUrl);
    const app = document.querySelector("ytmusic-app");
    const navigationManager = document.querySelector("yt-navigation-manager");
    const destination = target.pathname + target.search;
    const watchEndpoint = {
      videoId: target.searchParams.get("v"),
    };
    const playlistId = target.searchParams.get("list");
    if (playlistId) watchEndpoint.playlistId = playlistId;
    if (typeof app?.handleNavigationEndpoint === "function") {
      app.handleNavigationEndpoint({ watchEndpoint });
    } else if (typeof app?.navigator?.navigate === "function") {
      app.navigator.navigate({ watchEndpoint });
    } else if (typeof app?.navigate_ === "function") {
      app.navigate_(destination);
    } else if (typeof navigationManager?.navigate === "function") {
      navigationManager.navigate(destination);
    } else {
      throw new Error("YouTube Music navigation is not ready");
    }
  }, url);
  if (generation !== playGeneration)
    throw new Error("Playback selection was superseded");
  const deadline = Date.now() + 20000;
  let ready = false;
  while (!ready && Date.now() < deadline) {
    if (generation !== playGeneration)
      throw new Error("Playback selection was superseded");
    ready = await page.evaluate(({ videoId, previousVideoId, previousSource }) => {
      const media = document.querySelector("video");
      const player = document.querySelector("#movie_player");
      const selectedVideoId =
        player?.getVideoData?.()?.video_id ||
        player?.getVideoData?.()?.videoId ||
        "";
      const sourceReady =
        previousVideoId === videoId
          ? media?.currentTime <= 1
          : Boolean(media?.currentSrc && media.currentSrc !== previousSource);
      return (
        selectedVideoId === videoId &&
        sourceReady &&
        media &&
        media.readyState >= HTMLMediaElement.HAVE_METADATA &&
        Number.isFinite(media.duration) &&
        media.duration > 0
      );
    }, {
      videoId: expectedVideoId,
      previousVideoId: previousMedia.videoId,
      previousSource: previousMedia.source,
    });
    if (!ready) await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!ready) throw new Error("Timed out waiting for the selected song");
  if (generation !== playGeneration)
    throw new Error("Playback selection was superseded");
  await page.evaluate(async () => {
    const media = document.querySelector("video");
    await media.play();
  });
  sourceUrl = selectedSourceUrl;
  queueVideoIds = selectedQueueVideoIds;
  queueCount = queueVideoIds.length;
  fs.writeFileSync(
    PLAYBACK_STATE_PATH,
    JSON.stringify({ sourceUrl, queueVideoIds }),
    { mode: 0o600 },
  );
  return status();
}

async function cycleLoop() {
  await ensureBrowser();
  await page.evaluate(() => {
    const playerBar = document.querySelector("ytmusic-player-bar");
    if (typeof playerBar?.onRepeatButtonClick !== "function")
      throw new Error("YouTube Music repeat control was not found");
    playerBar.onRepeatButtonClick();
  });
  await new Promise((resolve) => setTimeout(resolve, 100));
  return page.evaluate(() => {
    const mode = document
      .querySelector("ytmusic-player-bar")
      ?.getAttribute("repeat-mode");
    return mode === "ONE" ? "song" : mode === "ALL" ? "playlist" : "none";
  });
}

async function control(action, value) {
  await ensureBrowser();
  if (action === "cycle-loop") return { loopMode: await cycleLoop() };
  const result = await page.evaluate(
    async ({ requestedAction, requestedValue, playbackQueue }) => {
      const media = document.querySelector("video");
      if (!media) throw new Error("YouTube Music player is not ready");
      if (requestedAction === "play-pause") {
        if (media.paused) await media.play();
        else media.pause();
      } else if (requestedAction === "restart") {
        media.currentTime = 0;
      } else if (requestedAction === "seek") {
        media.currentTime = Math.max(
          0,
          Math.min(Number(requestedValue), media.duration || 0),
        );
      } else if (
        requestedAction === "next" ||
        requestedAction === "previous"
      ) {
        const player = document.querySelector("#movie_player");
        const currentVideoId =
          player?.getVideoData?.()?.video_id ||
          player?.getVideoData?.()?.videoId ||
          "";
        const internalItems =
          document.querySelector("#queue")?.queue?.getItems?.() || [];
        const internalQueue = internalItems
          .map((item) => {
            const renderer =
              item?.playlistPanelVideoRenderer ||
              item?.playlistPanelVideoWrapperRenderer?.primaryRenderer
                ?.playlistPanelVideoRenderer;
            return renderer?.videoId || "";
          })
          .filter(Boolean);
        const activeQueue = internalQueue.includes(currentVideoId)
          ? internalQueue
          : playbackQueue;
        const currentIndex = activeQueue.indexOf(currentVideoId);
        const offset = requestedAction === "next" ? 1 : -1;
        const targetIndex = currentIndex + offset;
        if (
          currentIndex < 0 ||
          targetIndex < 0 ||
          targetIndex >= activeQueue.length
        ) {
          throw new Error(
            requestedAction === "next"
              ? "There is no next song"
              : "There is no previous song",
          );
        }
        const previousSource = media.currentSrc || "";
        const selector =
          requestedAction === "next"
            ? ".next-button.ytmusic-player-bar"
            : ".previous-button.ytmusic-player-bar";
        const button = document.querySelector(selector);
        if (!button)
          throw new Error(`YouTube Music ${requestedAction} control is not ready`);
        if (requestedAction === "previous" && media.currentTime > 0.1) {
          await new Promise((resolve) => {
            const timeout = setTimeout(resolve, 500);
            media.addEventListener(
              "seeked",
              () => {
                clearTimeout(timeout);
                resolve();
              },
              { once: true },
            );
            media.currentTime = 0;
          });
        }
        button.click();
        return {
          ok: true,
          previousVideoId: currentVideoId,
          previousSource,
        };
      } else {
        throw new Error(`Unsupported playback action: ${requestedAction}`);
      }
      return { ok: true };
    },
    {
      requestedAction: action,
      requestedValue: value,
      playbackQueue: queueVideoIds,
    },
  );
  if (!result.previousVideoId) return result;

  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const ready = await page.evaluate(({ previousVideoId, previousSource }) => {
      const media = document.querySelector("video");
      const player = document.querySelector("#movie_player");
      const selectedVideoId =
        player?.getVideoData?.()?.video_id ||
        player?.getVideoData?.()?.videoId ||
        "";
      return (
        selectedVideoId !== "" &&
        selectedVideoId !== previousVideoId &&
        media?.currentSrc &&
        media.currentSrc !== previousSource &&
        media.readyState >= HTMLMediaElement.HAVE_METADATA &&
        Number.isFinite(media.duration) &&
        media.duration > 0
      );
    }, {
      previousVideoId: result.previousVideoId,
      previousSource: result.previousSource,
    });
    if (ready) {
      await page.evaluate(() => document.querySelector("video")?.play());
      return { ok: true };
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out waiting for the queue to change songs");
}

async function status() {
  await ensureBrowser();
  const state = await page.evaluate(() => {
    const media = document.querySelector("video");
    const player = document.querySelector("#movie_player");
    const currentVideoId =
      player?.getVideoData?.()?.video_id ||
      player?.getVideoData?.()?.videoId ||
      "";
    const internalItems =
      document.querySelector("#queue")?.queue?.getItems?.() || [];
    const internalQueue = internalItems
      .map((item) => {
        const renderer =
          item?.playlistPanelVideoRenderer ||
          item?.playlistPanelVideoWrapperRenderer?.primaryRenderer
            ?.playlistPanelVideoRenderer;
        return renderer?.videoId || "";
      })
      .filter(Boolean);
    const metadata = navigator.mediaSession?.metadata;
    const repeatMode = document
      .querySelector("ytmusic-player-bar")
      ?.getAttribute("repeat-mode");
    return {
      running: Boolean(media),
      playing: Boolean(media && !media.paused),
      position: media?.currentTime || 0,
      duration:
        media && Number.isFinite(media.duration) ? media.duration : 0,
      canSeek: Boolean(media && Number.isFinite(media.duration)),
      title: metadata?.title || "",
      artist: metadata?.artist || "",
      album: metadata?.album || "",
      artUrl: metadata?.artwork?.at(-1)?.src || "",
      loopMode:
        repeatMode === "ONE"
          ? "song"
          : repeatMode === "ALL"
            ? "playlist"
            : "none",
      videoId:
        currentVideoId ||
        new URL(location.href).searchParams.get("v") ||
        "",
      internalQueueCount: internalQueue.length,
      internalQueuePosition: internalQueue.indexOf(currentVideoId),
    };
  });
  const explicitPosition = queueVideoIds.indexOf(state.videoId);
  const playlistPosition =
    state.internalQueuePosition >= 0
      ? state.internalQueuePosition
      : explicitPosition;
  const effectiveQueueCount =
    state.internalQueuePosition >= 0 ? state.internalQueueCount : queueCount;
  delete state.videoId;
  delete state.internalQueueCount;
  delete state.internalQueuePosition;
  return {
    ...state,
    playlistCount: effectiveQueueCount,
    playlistPosition,
    canGoPrevious: playlistPosition > 0,
    canGoNext:
      playlistPosition < 0
        ? effectiveQueueCount > 1
        : playlistPosition < effectiveQueueCount - 1,
    sourceUrl,
  };
}

function jsonResponse(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  response.end(body);
}

async function requestBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

async function handleRequest(request, response) {
  try {
    if (request.url === "/service") {
      jsonResponse(response, 200, { service: SERVICE_ID });
      return;
    }
    if (request.url === "/health") {
      await ensureBrowser();
      jsonResponse(response, 200, {
        service: SERVICE_ID,
        ready: true,
      });
      return;
    }
    if (request.url === "/play" && request.method === "POST") {
      const body = await requestBody(request);
      if (
        typeof body.url !== "string" ||
        !body.url.startsWith("https://music.youtube.com/watch?")
      ) {
        jsonResponse(response, 400, { error: "Invalid playback URL" });
        return;
      }
      if (
        typeof body.sourceUrl !== "string" ||
        !body.sourceUrl.startsWith("https://music.youtube.com/")
      ) {
        jsonResponse(response, 400, { error: "Invalid source URL" });
        return;
      }
      if (
        !Array.isArray(body.queueVideoIds) ||
        body.queueVideoIds.length < 1 ||
        body.queueVideoIds.some(
          (videoId) =>
            typeof videoId !== "string" ||
            !/^[A-Za-z0-9_-]{6,}$/.test(videoId),
        )
      ) {
        jsonResponse(response, 400, { error: "Invalid playback queue" });
        return;
      }
      jsonResponse(
        response,
        200,
        await play(body.url, body.sourceUrl, body.queueVideoIds),
      );
      return;
    }
    if (request.url === "/cycle-loop" && request.method === "POST") {
      jsonResponse(response, 200, { loopMode: await cycleLoop() });
      return;
    }
    if (request.url === "/control" && request.method === "POST") {
      const body = await requestBody(request);
      jsonResponse(response, 200, await control(body.action, body.value));
      return;
    }
    if (request.url === "/status") {
      jsonResponse(response, 200, await status());
      return;
    }
    jsonResponse(response, 404, { error: "Not found" });
  } catch (error) {
    jsonResponse(response, 500, {
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

function matchingServiceIsRunning() {
  return new Promise((resolve) => {
    const request = http.get(
      {
        host: HOST,
        port: PORT,
        path: "/service",
        timeout: 1000,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            resolve(
              response.statusCode === 200 && payload.service === SERVICE_ID,
            );
          } catch {
            resolve(false);
          }
        });
      },
    );
    request.on("error", () => resolve(false));
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
  });
}

function enterStandby() {
  if (standbyTimer || shuttingDown) return;
  console.log(
    `YouTube Music browser controller already running on ${HOST}:${PORT}`,
  );
  standbyTimer = setInterval(async () => {
    if (standbyCheckRunning || shuttingDown) return;
    standbyCheckRunning = true;
    try {
      if (!(await matchingServiceIsRunning())) {
        clearInterval(standbyTimer);
        standbyTimer = undefined;
        listen();
      }
    } finally {
      standbyCheckRunning = false;
    }
  }, STANDBY_RETRY_MS);
}

function listen() {
  if (shuttingDown) return;
  const candidate = http.createServer(handleRequest);
  server = candidate;
  candidate.once("error", async (error) => {
    if (server === candidate) server = undefined;
    if (error.code === "EADDRINUSE" && (await matchingServiceIsRunning())) {
      enterStandby();
      return;
    }
    console.error(
      `Could not start YouTube Music browser controller: ${error.message}`,
    );
    process.exitCode = 1;
  });
  candidate.listen(PORT, HOST, () => {
    console.log(`YouTube Music browser controller listening on ${HOST}:${PORT}`);
  });
}

function shutdown() {
  shuttingDown = true;
  if (standbyTimer) clearInterval(standbyTimer);
  if (server?.listening) server.close();
  browser?.disconnect();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

listen();
