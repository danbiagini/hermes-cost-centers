(function () {
  "use strict";
  // hermes-cost-centers: the Budgets tab. Thin by design: every figure comes
  // from the plugin's backend router, and this file only lays it out.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const C = SDK.components;
  const BASE = "/api/plugins/hermes-cost-centers";

  function api(path) {
    return SDK.fetchJSON(BASE + path);
  }

  function money(x) {
    if (x === null || x === undefined) return "—";
    return "$" + Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function pct(x) {
    return x === null || x === undefined ? "" : Math.round(x * 100) + "%";
  }

  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  const STATE_LABEL = { ok: "OK", wrap_up: "Wrap up", exhausted: "Exhausted", unset: "No budget" };

  function Bar({ used, allowance, budget }) {
    if (!budget) return null;
    const w = Math.min(100, (used || 0) * 100);
    const a = allowance ? Math.min(100, (allowance / budget) * 100) : null;
    return h("div", { className: "hcc-bar", title: pct(used) + " of budget" },
      h("div", { className: "hcc-bar-fill" + (used >= 1 ? " hcc-over" : used >= 0.8 ? " hcc-warn" : ""), style: { width: w + "%" } }),
      a !== null && h("div", { className: "hcc-bar-pace", style: { left: a + "%" }, title: "pace allowance " + money(allowance) })
    );
  }

  function Account({ data }) {
    const s = data.state;
    const a = s.account;
    return h(C.Card, null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Account, " + s.month.label)),
      h(C.CardContent, { className: "hcc-account" },
        h("div", null, h("div", { className: "hcc-big" }, money(a.spend_usd) + (a.incomplete ? "+" : "")),
          h("div", { className: "hcc-muted" }, a.target_usd !== null ? "of " + money(a.target_usd) + " target" : "no target set")),
        a.crosses_target_at && h("div", null,
          h("div", null, a.crosses_target_at === "crossed" ? "Target crossed" : "Crosses target " + when(a.crosses_target_at)),
          h("div", { className: "hcc-muted" }, "at this month's rate")),
        h("div", { className: "hcc-muted" }, "ledger: " + s.ledger + " · updated " + Math.round(data.age_seconds || 0) + "s ago")
      )
    );
  }

  function Runs({ name }) {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(function () {
      api("/cost-centers/" + encodeURIComponent(name) + "/runs?limit=25").then(setData).catch(function (e) { setErr(String(e)); });
    }, [name]);
    if (err) return h("div", { className: "hcc-error" }, err);
    if (!data) return h("div", { className: "hcc-muted" }, "Loading runs…");
    const days = Object.entries(data.daily_usd);
    const max = Math.max.apply(null, days.map(function (d) { return d[1]; }).concat([0.0001]));
    return h("div", { className: "hcc-detail" },
      h("div", { className: "hcc-days" }, days.map(function (d) {
        return h("div", { key: d[0], className: "hcc-day", title: d[0] + ": " + money(d[1]), style: { height: Math.max(2, (d[1] / max) * 48) + "px" } });
      })),
      h("table", { className: "hcc-table hcc-runs" },
        h("thead", null, h("tr", null, ["Started", "Cost", "Subagents", "Models", "Run"].map(function (t) { return h("th", { key: t }, t); }))),
        h("tbody", null, data.runs.map(function (r) {
          return h("tr", { key: r.ref },
            h("td", null, when(r.started)),
            h("td", null, money(r.total_usd) + (r.incomplete ? "+" : "")),
            h("td", null, r.subagents.length || ""),
            h("td", null, r.models.join(", ")),
            h("td", { className: "hcc-mono" }, r.ref));
        }))
      ),
      data.run_count > data.runs.length && h("div", { className: "hcc-muted" }, "showing " + data.runs.length + " of " + data.run_count + " runs")
    );
  }

  function Centers({ state }) {
    const [open, setOpen] = useState(null);
    const rows = Object.entries(state.cost_centers).sort(function (a, b) { return b[1].spend_usd - a[1].spend_usd; });
    return h(C.Card, null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Cost centers")),
      h(C.CardContent, null,
        h("table", { className: "hcc-table" },
          h("thead", null, h("tr", null, ["Cost center", "Spend", "Budget", "", "State", "Pace", "Runs"].map(function (t, i) { return h("th", { key: i }, t); }))),
          h("tbody", null, rows.map(function (kv) {
            const name = kv[0], c = kv[1];
            const pace = c.pace ? (c.pace.ahead_of_pace ? "Held until " + when(c.pace.eligible_at) : "On pace") : "";
            const main = h("tr", { key: name, className: "hcc-row", onClick: function () { setOpen(open === name ? null : name); } },
              h("td", null, name, c.sources.length > 1 && h("span", { className: "hcc-muted" }, " (" + c.sources.length + " sources)")),
              h("td", null, money(c.spend_usd) + (c.incomplete ? "+" : "")),
              h("td", null, money(c.budget_usd)),
              h("td", { className: "hcc-barcell" }, h(Bar, { used: c.used_fraction, allowance: c.pace && c.pace.allowance_usd, budget: c.budget_usd })),
              h("td", null, h(C.Badge, null, STATE_LABEL[c.state] || c.state)),
              h("td", null, pace),
              h("td", null, c.runs));
            if (open !== name) return main;
            return [main, h("tr", { key: name + "-detail" }, h("td", { colSpan: 7 }, h(Runs, { name: name })))];
          }))
        ),
        rows.some(function (kv) { return kv[1].incomplete; }) &&
          h("div", { className: "hcc-muted" }, "+ has unpriced usage: the spend shown is a floor, not the total.")
      )
    );
  }

  function Unattributed({ state }) {
    const rows = Object.entries(state.unattributed || {});
    if (!rows.length) return null;
    return h(C.Card, null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Unattributed")),
      h(C.CardContent, null,
        h("div", { className: "hcc-muted" }, "Spend from sources no cost center lists. Add them to a cost center in budgets.yaml."),
        h("table", { className: "hcc-table" }, h("tbody", null, rows.map(function (kv) {
          return h("tr", { key: kv[0] }, h("td", null, kv[0]), h("td", null, money(kv[1].spend_usd)), h("td", { className: "hcc-muted" }, kv[1].rows + " rows"));
        })))
      )
    );
  }

  function useBudgetState() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    const load = useCallback(function () {
      api("/state").then(function (d) { setData(d); setErr(null); }).catch(function (e) { setErr(String(e)); });
    }, []);
    useEffect(function () {
      load();
      const t = setInterval(load, 30000);
      return function () { clearInterval(t); };
    }, [load]);
    return [data, err];
  }

  function BudgetsPage() {
    const [data, err] = useBudgetState();
    if (err) return h("div", { className: "hcc-page" }, h("div", { className: "hcc-banner" }, "Budgets unavailable: " + err));
    if (!data) return h("div", { className: "hcc-page hcc-muted" }, "Loading…");
    if (!data.available) return h("div", { className: "hcc-page" }, h("div", { className: "hcc-banner" }, data.detail + " (" + data.state_path + ")"));
    return h("div", { className: "hcc-page" },
      data.stale && h("div", { className: "hcc-banner" }, "The budget evaluator hasn't run for " + Math.round((data.age_seconds || 0) / 60) + " minutes. These figures are out of date."),
      h(Account, { data: data }),
      h(Centers, { state: data.state }),
      h(Unattributed, { state: data.state })
    );
  }

  function AnalyticsSummary() {
    const [data] = useBudgetState();
    if (!data || !data.available) return null;
    const a = data.state.account;
    const exhausted = Object.values(data.state.cost_centers).filter(function (c) { return c.state === "exhausted"; }).length;
    return h("div", { className: "hcc-summary" },
      "Budgets: " + money(a.spend_usd) + (a.target_usd !== null ? " of " + money(a.target_usd) : "") + " this month" +
      (exhausted ? " · " + exhausted + " exhausted" : "") + (data.stale ? " · evaluator stale" : ""));
  }

  window.__HERMES_PLUGINS__.register("hermes-cost-centers", BudgetsPage);
  window.__HERMES_PLUGINS__.registerSlot("hermes-cost-centers", "analytics:bottom", AnalyticsSummary);
})();
