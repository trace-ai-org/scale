// SCALE's GitHub sign-in for the graph explorer: the one step of GitHub's OAuth sign-in a static page cannot do itself,
// trading the code GitHub gives for a token, which needs the OAuth app's secret. A Cloudflare Worker (or any host that
// runs a module with a fetch handler); it keeps nothing.
//
//   GET /login?origin=<page origin>&nonce=<hex>  ->  GitHub's sign-in, asking the public_repo scope (to fork the
//                                                    repository and open the pull request under the reader's login)
//   GET /callback?code=…&state=…                 ->  a page that hands the token to the window that opened it (the
//                                                    explorer, by postMessage to its origin only); the explorer then
//                                                    shows the pull request it opened in this same window
//
// Settings: GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (the OAuth app's; its callback URL is <this worker>/callback) and
// ALLOWED_ORIGINS (the explorer's origins, comma-separated: only they may start a sign-in or receive a token).
// service/github-login/README.md says how to set it up.

const b64url = (s) => btoa(String.fromCharCode(...new TextEncoder().encode(s))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64url = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0)));
const allowed = (env) => (env.ALLOWED_ORIGINS || "").split(",").map((o) => o.trim()).filter(Boolean);

// the page the sign-in window ends on: it hands the outcome to the explorer's origin only, then closes
function handBack(msg, origin) {
  const data = JSON.stringify(msg).replace(/</g, "\\u003c"), to = JSON.stringify(origin).replace(/</g, "\\u003c");
  return new Response(`<!doctype html><meta charset="utf-8"><title>SCALE: GitHub sign-in</title>
<p style="font:14px system-ui;margin:2em">${msg.token ? "Signed in. Opening your pull request…" : "The sign-in did not finish. You can close this window."}</p>
<script>if (window.opener) window.opener.postMessage(${data}, ${to});</script>`, {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/login") {
      const origin = url.searchParams.get("origin") || "", nonce = url.searchParams.get("nonce") || "";
      if (!allowed(env).includes(origin) || !/^[0-9a-f]{16,64}$/.test(nonce)) return new Response("This page may not sign in here.", { status: 403 });
      const gh = new URL("https://github.com/login/oauth/authorize");
      gh.searchParams.set("client_id", env.GITHUB_CLIENT_ID);
      gh.searchParams.set("redirect_uri", `${url.origin}/callback`);
      gh.searchParams.set("scope", "public_repo");
      gh.searchParams.set("state", b64url(JSON.stringify({ o: origin, n: nonce })));
      return Response.redirect(gh.toString(), 302);
    }
    if (url.pathname === "/callback") {
      let state;
      try { state = JSON.parse(unb64url(url.searchParams.get("state") || "")); } catch { state = null; }
      if (!state || !allowed(env).includes(state.o)) return new Response("This sign-in was not started here.", { status: 403 });
      const out = { type: "scale-github", nonce: state.n };
      const code = url.searchParams.get("code");
      if (!code) return handBack({ ...out, error: url.searchParams.get("error_description") || "the sign-in was cancelled" }, state.o);
      const r = await fetch("https://github.com/login/oauth/access_token", { method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json", "User-Agent": "scale-github-login" },
        body: JSON.stringify({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code, redirect_uri: `${url.origin}/callback` }) });
      const j = await r.json().catch(() => ({}));
      return handBack(j.access_token ? { ...out, token: j.access_token } : { ...out, error: j.error_description || "GitHub did not sign you in" }, state.o);
    }
    return new Response("SCALE's GitHub sign-in for the graph explorer.", { status: 404 });
  },
};
