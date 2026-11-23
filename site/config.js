// The SCALE web service this page talks to (service/README.md). Set when the service is deployed; empty, the page
// still opens and shows a graph file, and says the service is not configured.
window.SCALE_SERVICE_URL = "";
// The repository a graph is contributed to: without the service, Contribute opens GitHub's upload page for its
// graphs/catal-commons/incoming/ folder, and the commons intake (.github/workflows/commons-intake.yml) folds it in.
window.SCALE_REPO = "trace-ai-org/scale";
// The GitHub sign-in (service/github-login): set, Contribute signs the reader in with GitHub and opens the pull request
// itself, the graph attached; empty, it downloads the graph and opens GitHub's upload page instead.
window.SCALE_GITHUB_LOGIN = "https://scale-github-login.github-login.workers.dev";
