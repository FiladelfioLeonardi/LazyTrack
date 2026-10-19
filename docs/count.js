// Counts visits for Mochi's own stats: the date, the page, and the website you came from (only its name,
// like "reddit.com"). No cookies, no IP address, nothing that identifies you. Nobody can read it from outside.
(function () {
  var SUPA = "https://zmrzumltciteeznzjwaw.supabase.co", KEY = "sb_publishable_mmX_kRPufTOQk1M4fmjjFg_HenbqicQ";
  function source() {
    try {
      var q = new URLSearchParams(location.search), tag = q.get("s") || q.get("ref") || q.get("utm_source");
      if (tag) return tag.toLowerCase().replace(/[^a-z0-9._-]/g, "").slice(0, 60);
      if (!document.referrer) return "";
      var h = new URL(document.referrer).hostname.replace(/^www\./, "");
      if (h === location.hostname) return "";
      var names = {"t.co": "x.com", "out.reddit.com": "reddit.com", "old.reddit.com": "reddit.com", "l.instagram.com": "instagram.com",
                   "lm.facebook.com": "facebook.com", "l.facebook.com": "facebook.com", "news.ycombinator.com": "hackernews", "m.youtube.com": "youtube.com"};
      return (names[h] || h).slice(0, 60);
    } catch (_) { return ""; }
  }
  var page = (location.pathname.split("/").pop() || "index.html").replace(".html", "") || "index";
  var from = source();
  function send(event) {
    try {
      fetch(SUPA + "/rest/v1/page_views", {method: "POST", keepalive: true,
        headers: {apikey: KEY, "Content-Type": "application/json", Prefer: "return=minimal"},
        body: JSON.stringify({page: page.slice(0, 40), event: event, source: from})}).catch(function () {});
    } catch (_) {}
  }
  var key = "mochi-counted-" + page, seen = false;
  try { seen = sessionStorage.getItem(key) === "1"; sessionStorage.setItem(key, "1"); } catch (_) {}
  if (!seen) send("visit");   // a reload in the same tab isn't a new visit
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    if (/Mochi-Setup\.exe$/.test(a.href)) send("download-win");
    else if (/Install-Mochi-Mac\.zip$/.test(a.href)) send("download-mac");
  });
})();
