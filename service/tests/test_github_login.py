"""service/github-login/worker.js: the explorer's GitHub sign-in, run under Node with GitHub's token endpoint stubbed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1] / "github-login" / "worker.js"

SCRIPT = r"""
const { default: worker } = await import(process.argv[2]);
const env = { GITHUB_CLIENT_ID: "cid", GITHUB_CLIENT_SECRET: "secret", ALLOWED_ORIGINS: "https://trace-ai-org.github.io, http://localhost:8000" };
const sent = [];
globalThis.fetch = async (url, init) => { sent.push({ url, body: JSON.parse(init.body) });
  return new Response(JSON.stringify(init.body.includes("good") ? { access_token: "gho_token" } : { error_description: "bad code" })); };
const get = (path) => worker.fetch(new Request("https://login.example" + path), env);
const out = {};
let r = await get("/login?origin=https%3A%2F%2Ftrace-ai-org.github.io&nonce=00112233445566778899aabbccddeeff");
const to = new URL(r.headers.get("location"));
out.login = { status: r.status, host: to.host, scope: to.searchParams.get("scope"), redirect: to.searchParams.get("redirect_uri"), client: to.searchParams.get("client_id") };
const state = to.searchParams.get("state");
out.other_origin = (await get("/login?origin=https%3A%2F%2Fevil.example&nonce=00112233445566778899aabbccddeeff")).status;
out.bad_nonce = (await get("/login?origin=https%3A%2F%2Ftrace-ai-org.github.io&nonce=x")).status;
r = await get(`/callback?code=good&state=${state}`);
out.callback = { status: r.status, html: await r.text(), cache: r.headers.get("cache-control") };
out.exchange = sent[0];
out.bad_code = await (await get(`/callback?code=bad&state=${state}`)).text();
out.cancelled = await (await get(`/callback?error_description=no&state=${state}`)).text();
out.forged_state = (await get("/callback?code=good&state=e30")).status;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_sign_in_hands_the_token_only_to_the_explorers_origin(tmp_path):
    script = tmp_path / "run.mjs"
    script.write_text(SCRIPT, encoding="utf-8")
    res = subprocess.run(["node", str(script), WORKER.as_uri()], capture_output=True, text=True, timeout=60, check=True)
    out = json.loads(res.stdout)
    # GitHub's sign-in, asking only public_repo, coming back to this worker
    assert out["login"] == {"status": 302, "host": "github.com", "scope": "public_repo",
                            "redirect": "https://login.example/callback", "client": "cid"}
    # only an allowed origin, with a nonce, may start a sign-in; a state it did not make is refused
    assert out["other_origin"] == 403 and out["bad_nonce"] == 403 and out["forged_state"] == 403
    # the code is traded with the app's secret, and the token goes to the explorer's origin only, never cached
    assert out["exchange"]["url"] == "https://github.com/login/oauth/access_token"
    assert out["exchange"]["body"]["client_secret"] == "secret" and out["exchange"]["body"]["code"] == "good"
    html = out["callback"]["html"]
    assert '"token":"gho_token"' in html and 'postMessage(' in html and '"https://trace-ai-org.github.io")' in html
    assert '"nonce":"00112233445566778899aabbccddeeff"' in html and out["callback"]["cache"] == "no-store"
    assert "bad code" in out["bad_code"] and "gho_" not in out["bad_code"] and "cancelled" not in out["cancelled"] and "gho_" not in out["cancelled"]
