// WristWire status view.
//
// One plain script, no build step, used in two places:
//   - the Hermes dashboard tab (registered through window.__HERMES_PLUGINS__), which reads
//     /api/plugins/wristwire/status behind the dashboard's login;
//   - the relay's own page at http://127.0.0.1:8788/status, which reads /v1/garmin/status
//     with the relay admin key.
// Everything is built with DOM calls and textContent: no innerHTML, so nothing the relay
// reports (message text, error strings) can turn into markup.
(function () {
  "use strict";

  var NAME = "wristwire";
  var POLL_MS = 5000;

  // ------------------------------------------------------------------ DOM helpers

  function el(tag, attrs) {
    var node = document.createElement(tag);
    setAttrs(node, attrs);
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  var SVGNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    var node = document.createElementNS(SVGNS, tag);
    setAttrs(node, attrs);
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function setAttrs(node, attrs) {
    if (!attrs) return;
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.setAttribute("class", value);
      else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    });
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) return child.forEach(function (c) { append(node, c); });
    node.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }

  // Text with `code` spans, as the relay writes fixes ("run `wristwire doctor`").
  function richText(text) {
    var out = [];
    String(text || "").split("`").forEach(function (part, i) {
      if (part) out.push(i % 2 ? el("code", null, part) : part);
    });
    return out;
  }

  function ago(ts, now) {
    if (!ts) return "never";
    var s = Math.max(0, (now || Date.now() / 1000) - ts);
    if (s < 10) return "just now";
    if (s < 60) return Math.floor(s) + "\u00a0s ago";
    if (s < 3600) return Math.floor(s / 60) + "\u00a0min ago";
    if (s < 86400) return Math.floor(s / 3600) + "\u00a0h ago";
    var d = Math.floor(s / 86400);
    return d === 1 ? "1 day ago" : d + " days ago";
  }

  function seconds(a, b) {
    if (!a || !b) return null;
    var s = b - a;
    return s < 0 ? null : s < 10 ? s.toFixed(1) + " s" : s < 120 ? Math.round(s) + " s" : Math.round(s / 60) + " min";
  }

  // ------------------------------------------------------------------ device drawings

  // Simple line drawings, 48x48, stroked in currentColor so they follow the theme.
  function deviceIcon(kind) {
    var common = { fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round" };
    var parts;
    if (kind === "watch") {
      parts = [
        svg("path", Object.assign({ d: "M18 11 L19.5 4 H28.5 L30 11" }, common)),
        svg("path", Object.assign({ d: "M18 37 L19.5 44 H28.5 L30 37" }, common)),
        svg("circle", Object.assign({ cx: "24", cy: "24", r: "13" }, common)),
        svg("circle", Object.assign({ cx: "24", cy: "24", r: "9.5", opacity: "0.45" }, common)),
        svg("path", Object.assign({ d: "M24 18.5 V24 L27.5 26" }, common)),
        svg("path", Object.assign({ d: "M37.5 20.5 H39.5 M37.5 27.5 H39.5" }, common)),
      ];
    } else if (kind === "phone") {
      parts = [
        svg("rect", Object.assign({ x: "14", y: "4", width: "20", height: "40", rx: "4" }, common)),
        svg("path", Object.assign({ d: "M21 8 H27" }, common)),
        svg("path", Object.assign({ d: "M19 17 H29 M19 22 H26 M19 27 H28", opacity: "0.55" }, common)),
        svg("path", Object.assign({ d: "M21 39 H27" }, common)),
      ];
    } else if (kind === "relay") {
      parts = [
        svg("rect", Object.assign({ x: "7", y: "10", width: "34", height: "12", rx: "2.5" }, common)),
        svg("rect", Object.assign({ x: "7", y: "26", width: "34", height: "12", rx: "2.5" }, common)),
        svg("circle", { cx: "13", cy: "16", r: "1.6", fill: "currentColor" }),
        svg("circle", { cx: "13", cy: "32", r: "1.6", fill: "currentColor" }),
        svg("path", Object.assign({ d: "M20 16 H35 M20 32 H31", opacity: "0.55" }, common)),
      ];
    } else {
      parts = [
        svg("circle", Object.assign({ cx: "24", cy: "24", r: "6" }, common)),
        svg("ellipse", Object.assign({ cx: "24", cy: "24", rx: "17", ry: "7.5", transform: "rotate(-28 24 24)", opacity: "0.6" }, common)),
        svg("ellipse", Object.assign({ cx: "24", cy: "24", rx: "17", ry: "7.5", transform: "rotate(28 24 24)", opacity: "0.6" }, common)),
        svg("circle", { cx: "38.5", cy: "16.5", r: "1.8", fill: "currentColor" }),
      ];
    }
    return svg("svg", { viewBox: "0 0 48 48", width: "48", height: "48", "aria-hidden": "true", class: "ww-device-art" }, parts);
  }

  // State is shown by shape as well as color.
  var MARK = { ok: "✓", warn: "!", down: "×", idle: "•", unseen: "?" };
  var STATE_WORDS = {
    ok: "Working",
    warn: "Needs a look",
    down: "Not working",
    idle: "Waiting for traffic",
    unseen: "Not visible to WristWire",
  };

  function mark(state) {
    return el("span", { class: "ww-mark", "data-state": state, "aria-hidden": "true" }, MARK[state] || "?");
  }

  // ------------------------------------------------------------------ pieces

  function nodeCard(node) {
    return el(
      "div",
      { class: "ww-node", "data-state": node.state, "data-kind": node.id },
      el("div", { class: "ww-device" }, deviceIcon(node.id), mark(node.state)),
      el("h3", { class: "ww-node-name" }, node.label, el("span", { class: "ww-sr" }, ": " + (STATE_WORDS[node.state] || ""))),
      el(
        "ul",
        { class: "ww-facts" },
        (node.facts || []).map(function (f) { return el("li", null, f); })
      )
    );
  }

  function lane(dir, info) {
    var label = dir === "in" ? "Toward the agent" : "Back to the watch";
    return el(
      "div",
      { class: "ww-lane", "data-dir": dir, "data-state": info.state },
      el(
        "div",
        { class: "ww-wire", "aria-hidden": "true" },
        info.state === "down" ? el("span", { class: "ww-break" }, "×") : null
      ),
      el(
        "p",
        { class: "ww-lane-text" },
        el("span", { class: "ww-sr" }, label + ", " + (STATE_WORDS[info.state] || "") + ": "),
        info.text
      )
    );
  }

  function linkCard(link) {
    return el(
      "div",
      { class: "ww-link", "data-link": link.id },
      el("p", { class: "ww-via" }, link.via),
      lane("in", link.inbound),
      lane("out", link.outbound)
    );
  }

  var REQUEST_STATUS = {
    received: "Received",
    queued: "Sent to the agent",
    running: "Agent working",
    needs_approval: "Waiting for your approval code",
    completed: "Answered, delivering",
    delivered: "Delivered",
    failed: "Failed",
    blocked: "Blocked",
    rejected: "Rejected",
  };

  function recentTable(rows, now) {
    // Messages the agent sent on its own (including test messages) aren't watch traffic.
    rows = (rows || []).filter(function (r) { return r.source_type !== "hermes_proactive"; });
    if (!rows.length) {
      return el("p", { class: "ww-empty" }, "No watch messages yet. Send one from your watch, then come back here.");
    }
    return el(
      "div",
      { class: "ww-table-wrap" },
      el(
        "table",
        { class: "ww-table" },
        el(
          "thead",
          null,
          el("tr", null,
            el("th", { scope: "col" }, "Message"),
            el("th", { scope: "col" }, "Received"),
            el("th", { scope: "col" }, "Agent took"),
            el("th", { scope: "col" }, "Delivery took"),
            el("th", { scope: "col" }, "Status"))
        ),
        el(
          "tbody",
          null,
          rows.slice(0, 10).map(function (r) {
            var text = String(r.message_text_redacted || "").trim();
            if (text.length > 70) text = text.slice(0, 67) + "…";
            var kind = r.source_type === "voice_note" ? "Voice note" : "Text";
            var failed = r.status === "failed";
            return el(
              "tr",
              { "data-status": r.status },
              el("td", { "data-label": "Message" }, el("span", { class: "ww-kind" }, kind), " ", text || "—"),
              el("td", { "data-label": "Received" }, ago(r.created_at, now)),
              el("td", { "data-label": "Agent took" }, seconds(r.created_at, r.completed_at) || "—"),
              el("td", { "data-label": "Delivery took" }, seconds(r.completed_at, r.delivered_at) || "—"),
              el("td", { "data-label": "Status", class: failed ? "ww-bad" : null }, REQUEST_STATUS[r.status] || r.status)
            );
          })
        )
      )
    );
  }

  // When the relay itself can't be reached, draw the chain with the relay broken.
  function relayDownLinks(data) {
    var where = data.relay_url ? " at " + data.relay_url : "";
    var auth = data.error_kind === "auth";
    var title = auth ? "Hermes can't sign in to the WristWire relay" : "The WristWire relay isn't running";
    var fix = auth
      ? "Set GARMIN_RELAY_ADMIN_KEY for Hermes, or the plugin setting `admin_key`, to the value in the relay's .env. " +
        (data.error ? "Error: " + data.error : "")
      : "Start it with `systemctl --user start wristwire` (or run `wristwire adb-bridge`). " +
        (data.error ? "Error: " + data.error : "");
    var unknown = { state: "idle", text: auth ? "Unknown until Hermes can sign in" : "Unknown while the relay is down" };
    var down = auth ? unknown : { state: "down", text: "Relay isn't answering" + where };
    return {
      verdict: { state: "down", text: title },
      nodes: [
        { id: "watch", label: "Garmin watch", state: "idle", facts: [] },
        { id: "phone", label: "Agent phone", state: "idle", facts: [] },
        { id: "relay", label: "WristWire relay", state: "down", facts: [auth ? "Running, key rejected" : "Not answering"] },
        { id: "agent", label: "Agent", state: "idle", facts: [] },
      ],
      links: [
        { id: "watch-phone", via: "Garmin Messenger", inbound: unknown, outbound: { state: "unseen", text: "Not visible to WristWire" } },
        { id: "phone-relay", via: "Phone link", inbound: down, outbound: down },
        { id: "relay-agent", via: "Agent", inbound: down, outbound: down },
      ],
      problems: [{ state: "down", title: title, fix: fix }],
    };
  }

  // ------------------------------------------------------------------ the view

  function createView(root, host) {
    var timer = null;
    var destroyed = false;
    var last = null;
    var loadedAt = 0;
    var testNote = null;
    var testing = false;

    root.classList.add("ww-view");

    function render() {
      var data = last || {};
      var now = (data.links && data.links.now) || data.timestamp || Date.now() / 1000;
      var links = data.relay_reachable === false ? relayDownLinks(data) : data.links;
      while (root.firstChild) root.removeChild(root.firstChild);

      if (!links) {
        root.appendChild(el("p", { class: "ww-empty" }, data.error ? data.error : "Loading status…"));
        return;
      }

      var verdict = links.verdict || { state: "idle", text: "" };
      var approvals = data.pending_approvals || [];
      // The view is rebuilt on every poll; keep keyboard focus on the same control.
      var focused = document.activeElement && root.contains(document.activeElement)
        ? document.activeElement.getAttribute("data-focus")
        : null;

      root.appendChild(
        el(
          "header",
          { class: "ww-head", "data-state": verdict.state },
          el(
            "div",
            { class: "ww-head-text" },
            el("h2", { class: "ww-verdict" }, mark(verdict.state), el("span", null, verdict.text)),
            el(
              "p",
              { class: "ww-updated" },
              "Checked " + ago(loadedAt / 1000) + ". Updates every few seconds."
            )
          ),
          el(
            "div",
            { class: "ww-actions" },
            el(
              "button",
              {
                type: "button",
                class: "ww-button",
                "data-focus": "send-test",
                disabled: testing || data.relay_reachable === false,
                onclick: sendTest,
              },
              testing ? "Sending…" : "Send test message"
            ),
            testNote ? el("p", { class: "ww-test-note", "data-state": testNote.state, role: "status" }, testNote.text) : null
          )
        )
      );

      var chain = el("section", { class: "ww-chain", "aria-label": "Connections from watch to agent" });
      links.nodes.forEach(function (node, i) {
        chain.appendChild(nodeCard(node));
        if (links.links[i]) chain.appendChild(linkCard(links.links[i]));
      });
      root.appendChild(chain);
      root.appendChild(
        el(
          "p",
          { class: "ww-legend" },
          "Each connection has two lines, and the arrows show the direction: one carries watch messages toward the agent, the other carries replies back. ",
          "A dotted line is a step WristWire can't see."
        )
      );

      if (approvals.length) {
        root.appendChild(
          el(
            "section",
            { class: "ww-section" },
            el("h3", { class: "ww-section-title" }, "Waiting for your approval"),
            el(
              "ul",
              { class: "ww-list" },
              approvals.map(function (a) {
                return el("li", null, "Reply ", el("code", null, "approve " + a.approval_code), " on your watch to run: ", a.action_summary || "a gated action");
              })
            )
          )
        );
      }

      if (links.problems && links.problems.length) {
        root.appendChild(
          el(
            "section",
            { class: "ww-section ww-problems" },
            el("h3", { class: "ww-section-title" }, "What needs attention"),
            el(
              "ul",
              { class: "ww-list" },
              links.problems.map(function (p) {
                return el(
                  "li",
                  { "data-state": p.state },
                  mark(p.state),
                  el("div", null, el("p", { class: "ww-problem-title" }, p.title), el("p", { class: "ww-fix" }, richText(p.fix)))
                );
              })
            )
          )
        );
      }

      root.appendChild(
        el(
          "section",
          { class: "ww-section" },
          el("h3", { class: "ww-section-title" }, "Recent watch messages"),
          recentTable(data.recent_requests, now)
        )
      );

      if (focused) {
        var again = root.querySelector('[data-focus="' + focused + '"]');
        if (again && !again.disabled) again.focus();
      }
    }

    function schedule() {
      if (destroyed) return;
      clearTimeout(timer);
      timer = setTimeout(tick, document.hidden ? POLL_MS * 6 : POLL_MS);
    }

    function tick() {
      return Promise.resolve()
        .then(host.load)
        .then(function (data) {
          if (destroyed || !data) return;
          last = data;
          loadedAt = Date.now();
          render();
        })
        .catch(function (err) {
          if (destroyed) return;
          if (host.onError && host.onError(err)) return;
          last = { relay_reachable: false, error: String((err && err.message) || err) };
          loadedAt = Date.now();
          render();
        })
        .then(schedule);
    }

    function sendTest() {
      testing = true;
      testNote = null;
      render();
      Promise.resolve()
        .then(host.sendTest)
        .then(function (res) {
          testNote = res && res.ok
            ? { state: "ok", text: "Queued. Watch the phone link: it turns green when the reply lands in Garmin Messenger, then check your watch." }
            : { state: "down", text: "Not sent: " + ((res && (res.error || (res.result && res.result.status))) || "the relay refused it") };
        })
        .catch(function (err) {
          testNote = { state: "down", text: "Not sent: " + String((err && err.message) || err) };
        })
        .then(function () {
          testing = false;
          tick();
        });
    }

    function onVisible() {
      if (!document.hidden) tick();
    }
    document.addEventListener("visibilitychange", onVisible);
    render();
    tick();

    return {
      destroy: function () {
        destroyed = true;
        clearTimeout(timer);
        document.removeEventListener("visibilitychange", onVisible);
      },
    };
  }

  // ------------------------------------------------------------------ host: Hermes dashboard

  if (window.__HERMES_PLUGINS__ && window.__HERMES_PLUGIN_SDK__) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var API = "/api/plugins/" + NAME;

    var Page = function WristWirePage() {
      var ref = SDK.hooks.useRef(null);
      SDK.hooks.useEffect(function () {
        var view = createView(ref.current, {
          load: function () { return SDK.fetchJSON(API + "/status"); },
          sendTest: function () {
            return SDK.fetchJSON(API + "/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
          },
        });
        return function () { view.destroy(); };
      }, []);
      return React.createElement("div", { ref: ref, className: "ww-root ww-in-hermes" });
    };
    window.__HERMES_PLUGINS__.register(NAME, Page);
    return;
  }

  // ------------------------------------------------------------------ host: the relay's own page

  var mount = document.getElementById("wristwire-root");
  if (!mount) return;

  // Holds a session token from a sign-in link, or an admin key typed into the form.
  var KEY = "wristwire-auth";
  function readKey() {
    try { return sessionStorage.getItem(KEY) || ""; } catch (e) { return memoryKey; }
  }
  function writeKey(value) {
    memoryKey = value;
    try { value ? sessionStorage.setItem(KEY, value) : sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ }
  }
  var memoryKey = "";

  // `wristwire dashboard` prints a link with a one-time code after `#`. The fragment never
  // reaches the server; the code is traded for a session token once and is dead afterwards,
  // so the copy in browser history can't sign anyone in.
  function takeCode() {
    var match = /(?:^|[#&])code=([^&]+)/.exec(location.hash || "");
    if (!match) return Promise.resolve(null);
    history.replaceState(null, "", location.pathname + location.search);
    var code;
    try { code = decodeURIComponent(match[1]); } catch (e) { return Promise.resolve("bad"); }
    return fetch("/v1/garmin/ui/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code }),
      cache: "no-store",
    })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (data && data.token) { writeKey(data.token); return "ok"; }
        return "bad";
      })
      .catch(function () { return "bad"; });
  }

  function relayFetch(path, options) {
    options = options || {};
    var headers = Object.assign({ Authorization: "Bearer " + readKey() }, options.headers || {});
    return fetch(path, Object.assign({}, options, { headers: headers, cache: "no-store" })).then(function (res) {
      if (res.status === 401) {
        var err = new Error("unauthorized");
        err.unauthorized = true;
        throw err;
      }
      return res.json();
    });
  }

  var view = null;

  function showKeyForm(message) {
    if (view) { view.destroy(); view = null; }
    while (mount.firstChild) mount.removeChild(mount.firstChild);
    var input = el("input", { id: "ww-key", class: "ww-input", type: "password", autocomplete: "off", spellcheck: "false" });
    var form = el(
      "form",
      {
        class: "ww-key-form",
        onsubmit: function (event) {
          event.preventDefault();
          if (input.value.trim()) { writeKey(input.value.trim()); start(); }
        },
      },
      el("h1", { class: "ww-title" }, "WristWire status"),
      el("p", null, "Run ", el("code", null, "wristwire dashboard"), " on the relay machine for a link that signs you in. Or paste the relay admin key, ", el("code", null, "GARMIN_RELAY_ADMIN_KEY"), " in your .env file."),
      message ? el("p", { class: "ww-bad", role: "alert" }, richText(message)) : null,
      el("label", { class: "ww-label", for: "ww-key" }, "Relay admin key"),
      el("div", { class: "ww-key-row" }, input, el("button", { type: "submit", class: "ww-button" }, "Show status"))
    );
    mount.appendChild(form);
    input.focus();
  }

  function start() {
    if (!readKey()) return showKeyForm();
    while (mount.firstChild) mount.removeChild(mount.firstChild);
    mount.appendChild(el("h1", { class: "ww-title" }, "WristWire status"));
    var body = el("div");
    mount.appendChild(body);
    view = createView(body, {
      load: function () {
        return relayFetch("/v1/garmin/status").then(function (data) {
          data.relay_reachable = true;
          return data;
        });
      },
      sendTest: function () {
        var now = new Date();
        var hhmm = ("0" + now.getHours()).slice(-2) + ":" + ("0" + now.getMinutes()).slice(-2);
        return relayFetch("/v1/garmin/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: "WristWire test, " + hhmm + ". If this reached your watch, replies work." }),
        }).then(function (res) {
          return { ok: res.status === "queued_for_watch", result: res, error: res.error };
        });
      },
      onError: function (err) {
        if (err && err.unauthorized) {
          writeKey("");
          showKeyForm("Your sign-in expired or the key was rejected. Run `wristwire dashboard` for a new link.");
          return true;
        }
        return false;
      },
    });
  }

  takeCode().then(function (result) {
    if (result === "bad") {
      showKeyForm("That sign-in link was already used or has expired. Run `wristwire dashboard` for a new one.");
    } else {
      start();
    }
  });
})();
