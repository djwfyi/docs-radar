name: Fetch Docs Signals

on:
  schedule:
    - cron: "0 6 * * 1" # Every Monday at 06:00 UTC
  workflow_dispatch:
  push:
    branches: [main]
    paths:
      - "scripts/fetch_signals.py"

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: "pages"
  cancel-in-progress: false

jobs:
  fetch-and-deploy:
    runs-on: ubuntu-latest
    env:
      HUGO_VERSION: 0.170.1
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Python dependencies
        run: pip install requests dnspython

      - name: Run Signal Collector
        env:
          GITHUB_TOKEN: ${{ secrets.SEARCH_PAT || secrets.GITHUB_TOKEN }}
        run: python scripts/fetch_signals.py

      - name: Commit updated signals
        run: |
          git config --global user.name "DocsRadar Bot"
          git config --global user.email "bot@users.noreply.github.com"
          git add data/signals.json
          if [ -f data/seen.json ]; then git add data/seen.json; fi
          git diff --staged --quiet || (git commit -m "chore: update weekly docs signals [skip ci]" && git push)

      - name: Setup Hugo
        uses: peaceiris/actions-hugo@v3
        with:
          hugo-version: ${{ env.HUGO_VERSION }}
          extended: true

      - name: Setup Pages
        uses: actions/configure-pages@v5

      - name: Build Hugo site
        run: hugo --minify --cacheDir /tmp/hugo_cache

      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: ./public

  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    needs: fetch-and-deploy
    steps:
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
