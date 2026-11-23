# GitHub sign-in for the graph explorer

The explorer is a static page, so it cannot finish GitHub's sign-in on its own: trading the code GitHub gives for a
token needs the OAuth app's secret. [`worker.js`](worker.js) does that one step and keeps nothing. With it, the
explorer's **Contribute** is one click: a GitHub sign-in window opens, the page puts the graph on a branch of the
reader's fork, and the window shows GitHub's *Open a pull request* page, the graph attached and the title and
description filled in; the reader reviews it and creates it under their login (their credit). The token lives in the
page's memory for that click and is never saved. Without it, Contribute downloads the graph and opens GitHub's upload
page, where the reader drops it.

## Setting it up (done by a person)

1. **An OAuth app.** GitHub, Settings, Developer settings, OAuth Apps, New OAuth App: homepage
   `https://trace-ai-org.github.io/scale/`, authorization callback URL `https://<your worker>/callback`. Note its
   client ID and generate a client secret.
2. **The worker.** With a Cloudflare account (the free plan is enough) and
   [wrangler](https://developers.cloudflare.com/workers/wrangler/):

   ```bash
   cd service/github-login
   npx wrangler deploy worker.js --name scale-github-login --compatibility-date 2026-11-01
   npx wrangler secret put GITHUB_CLIENT_ID --name scale-github-login
   npx wrangler secret put GITHUB_CLIENT_SECRET --name scale-github-login
   npx wrangler secret put ALLOWED_ORIGINS --name scale-github-login   # https://trace-ai-org.github.io
   ```

   Any other host that runs a JavaScript module with a `fetch` handler works too.
3. **The page.** Set `window.SCALE_GITHUB_LOGIN` in `site/config.js` to the worker's URL, by a pull request.

The sign-in asks the `public_repo` scope: the page forks the repository and adds the graph on a branch of the fork;
the reader creates the pull request on GitHub's page. Only the origins in `ALLOWED_ORIGINS` can start a sign-in or
receive its token, which goes to the window that opened the sign-in and nowhere else.