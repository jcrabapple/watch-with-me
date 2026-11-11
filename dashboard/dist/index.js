/* Watch With Me: the Hermes Dashboard sidebar tab.
 *
 * One compact top bar (live state chip, the resolved LAN/phone URL with a
 * Copy button, and an Open Standalone link) over a full-height iframe of the
 * companion page served at /api/plugins/watch-with-me/app. Same-origin
 * framing is allowed by the page CSP (frame-ancestors 'self').
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const hooks = SDK.hooks;

  const BASE = "/api/plugins/watch-with-me";

  // Delegate to the host SDK's fetchJSON so auth works in BOTH dashboard
  // modes: loopback (X-Hermes-Session-Token header) and gated OAuth
  // (hermes_session_at cookie via credentials:'include'). fetchJSON throws
  // Error("<status>: <body>") on non-2xx; callers catch and degrade.
  function api(path) {
    return SDK.fetchJSON(BASE + path);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        if (document.execCommand("copy")) resolve();
        else reject(new Error("copy command refused"));
      } catch (err) {
        reject(err);
      } finally {
        area.remove();
      }
    });
  }

  function WatchWithMeDashboardPage() {
    const [setup, setSetup] = hooks.useState(null);
    const [session, setSession] = hooks.useState(null);
    const [copyState, setCopyState] = hooks.useState("idle"); // idle | copied | error

    hooks.useEffect(function () {
      // /setup re-probes Plex (sections + ten item metadata fetches), so it
      // is fetched once on mount — not on the poll. Only the lightweight
      // /session state rides the 10-second interval.
      api("/setup").then(setSetup).catch(function () {});
      api("/session").then(setSession).catch(function () {});
      const id = setInterval(function () {
        api("/session").then(setSession).catch(function () {});
      }, 10000);
      return function () { clearInterval(id); };
    }, []);

    const companionUrl = (setup && setup.server && setup.server.companion_url) || "";
    const state = (session && session.state) || "idle";
    const claimed = !!(session && session.claimed);
    const media = session && session.media;
    const stateLabel = state === "idle" ? "Idle"
      : claimed ? "Live" : state === "candidate" ? "Candidate" : "Observed";

    function onCopy() {
      if (!companionUrl) return;
      copyText(companionUrl).then(function () {
        setCopyState("copied");
        setTimeout(function () { setCopyState("idle"); }, 1800);
      }).catch(function () {
        setCopyState("error");
        setTimeout(function () { setCopyState("idle"); }, 2500);
      });
    }

    return React.createElement("div", { className: "wwm-page" },
      React.createElement("header", { className: "wwm-topbar" },
        React.createElement("span", {
          className: "wwm-state wwm-state-" + (claimed ? "live" : state),
          title: "Watcher state",
        }, stateLabel),
        media && media.title
          ? React.createElement("span", { className: "wwm-title" }, media.title)
          : null,
        React.createElement("span", { className: "wwm-url", title: companionUrl },
          companionUrl || "Link Plex to get a phone URL…"),
        React.createElement("button", {
          type: "button",
          className: "wwm-copy",
          onClick: onCopy,
          disabled: !companionUrl,
        }, copyState === "copied" ? "Copied ✓"
           : copyState === "error" ? "Copy failed" : "Copy Phone Link"),
        React.createElement("a", {
          className: "wwm-open",
          href: BASE + "/app",
          target: "_blank",
          rel: "noopener noreferrer",
        }, "Open Standalone")
      ),
      React.createElement("iframe", {
        className: "wwm-frame",
        src: BASE + "/app",
        title: "Watch With Me",
      })
    );
  }

  window.__HERMES_PLUGINS__.register("watch-with-me", WatchWithMeDashboardPage);
})();
