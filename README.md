Welcome to the repo for the Docs Signal Radar!

This repo tracks docs signals from
- GitHub
- Reddit
- Hacker News

It combines these signals to identify early-stage startups that are beginning to work on their docs journeys.

The repo uses Python3 @3.12 and the static site generator Hugo @0.170.1.
If those are installed, you can clone this repo and run:

You'll need to have your own GitHub access token, and then load it as an environment variable in your terminal.

```
export GITHUB_TOKEN=<insert-your-pat>
pip install requests
python scripts/fetch_signals.py
hugo server
```

to run the signals yourself.

View the results locally by opening `http://localhost:1313` in your browser.
