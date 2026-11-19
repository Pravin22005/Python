# GitHub Pages setup

The deployment workflow publishes the static site to the `gh-pages` branch. This avoids the Pages API permission error from `actions/configure-pages`.

In the repository, open **Settings → Pages**, choose **Deploy from a branch**, select `gh-pages` and `/ (root)`, then click **Save**. After the next successful `Deploy Python Playground` run, the site is available at:

`https://pravin22kumar.github.io/Python/`
