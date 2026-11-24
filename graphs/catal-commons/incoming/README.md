# Contributions waiting to be folded in

A graph curated in the [graph explorer](https://trace-ai-org.github.io/scale/) is contributed here, with every quote
of a source in copyright withheld. Its *Contribute* button signs you in with GitHub and opens GitHub's pull request page
for you, the graph attached and the description filled in, to review and create under your login (your credit on the leaderboard) ([`service/github-login`](../../../service/github-login/README.md)).
Until that sign-in is set up, the button downloads the graph and opens GitHub's upload page for this folder: drop the
file there, then **Propose changes** and **Create pull request**.

The commons intake (`.github/workflows/commons-intake.yml`, `service/scaleweb/intake.py`) reads the file as data,
folds it into catal-commons with `scale graph contribute` (the source taken as in copyright: no quote kept), opens the
fold pull request crediting you, and closes yours. A person reviews and merges the fold pull request. Nothing stays in
this folder.
