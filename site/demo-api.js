// 試験公開版: サーバーの API (/api/...) の代わりに、data/ の JSON から同じ形の答えを返す。
// 画面 (index.html) は web/index.html をそのまま写したもの。tools/export_site.py が作る。
(function () {
  "use strict";
  const realFetch = window.fetch.bind(window);
  const cache = {};
  const load = (name) => (cache[name] ||= realFetch("data/" + name).then((r) => {
    if (!r.ok) throw new Error(name + " を読み込めませんでした");
    return r.json();
  }));
  const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
  });
  const NO_AI = "試験公開版のため、AI による回答は使えません。タイムラインで、収集した資料から取り出した出来事と根拠の原文を確かめられます。";
  const ndjson = (events) => new Response(events.map((e) => JSON.stringify(e) + "\n").join(""), {
    headers: { "Content-Type": "application/x-ndjson" },
  });

  // 初めて開いたときは、動く機能のあるタイムラインから見せる
  try { if (!localStorage.getItem("view")) localStorage.setItem("view", "timeline"); } catch {}

  // サーバー版 (timeline/view.py の timeline()) と同じ条件で絞る
  async function timeline(params) {
    const all = await load("timeline.json");
    const split = (s) => (s || "").split(",").filter(Boolean);
    const fields = split(params.get("fields")), actors = split(params.get("actors"));
    const from = (params.get("date_from") || "").slice(0, 7), to = params.get("date_to") || "";
    const events = all.events.filter((e) =>
      (!fields.length || e._fields.some((f) => fields.includes(f))) &&
      (!actors.length || actors.includes(e.actor)) &&
      (!from || !e.start || (e.end || e.start) >= from) &&
      (!to || !e.start || e.start <= to));
    const present = new Set(events.map((e) => e.actor));
    return { ...all, actors: all.actors.filter((a) => present.has(a.id) || actors.includes(a.id)), events };
  }

  async function api(path, params, method) {
    if (path === "/api/me") return json({ username: "試験公開版", limit: 0, used: 0, remaining: 0 });
    if (path === "/api/login") return json({ username: "試験公開版" });
    if (path === "/api/logout") return json({});
    if (path === "/api/status") return json(await load("status.json"));
    if (path === "/api/options") return json(await load("options.json"));
    if (path === "/api/timeline/options") return json(await load("timeline-options.json"));
    if (path === "/api/timeline") return json(await timeline(params));
    if (path === "/api/chat" || path === "/api/timeline/ask") return ndjson([{ type: "error", message: NO_AI }]);
    if (path === "/api/conversations") return json([]);
    let m = path.match(/^\/api\/conversations\/\d+$/);
    if (m) return method === "DELETE" ? json({}) : json({ detail: "試験公開版では会話の履歴を残しません" }, 404);
    m = path.match(/^\/api\/timeline\/source\/(\d+)$/);
    if (m) {
      const s = (await load("sources.json"))[m[1]];
      return s ? json(s) : json({ detail: "出来事が見つかりません" }, 404);
    }
    return json({ detail: "試験公開版では使えません" }, 404);
  }

  window.fetch = function (input, init) {
    const raw = typeof input === "string" ? input : input.url;
    if (!raw.startsWith("/api/")) return realFetch(input, init);
    const url = new URL(raw, location.origin);
    const method = (init && init.method) || "GET";
    return api(url.pathname, url.searchParams, method.toUpperCase())
      .catch((e) => json({ detail: e.message }, 500));
  };
})();
