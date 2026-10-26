# Branding assets

## social-preview.png

The image GitHub renders when a link to this repository is shared on X,
LinkedIn, Slack or anywhere else that reads Open Graph tags. Without one,
every share is a grey box with the repository name in it.

**1280 by 640**, which is what GitHub asks for.

It cannot be set from the API or the CLI. Upload it by hand:
**Settings → General → Social preview → Upload an image**.

### Regenerating it

`social-preview.html` is the source. It is deliberately a file rather than a
design in somebody's account, so the next person to change the headline does
not have to recreate the whole thing.

```bash
B=~/.claude/skills/gstack/browse/dist/browse
$B goto "file://$PWD/.github/branding/social-preview.html"
$B screenshot .github/branding/social-preview.png
python3 -c "from PIL import Image; \
  Image.open('.github/branding/social-preview.png').convert('RGB') \
  .crop((0,0,1280,640)).save('.github/branding/social-preview.png', optimize=True)"
```

The crop is needed because the browser viewport is 720 tall and the card is
640.

### Why it says what it says

The landing page's own Open Graph image leads with "Nothing is deleted.
Everything explains itself", which is the right line for somebody who arrived
at a marketing site. This is not that audience. A developer looking at a
repository card is deciding whether to click, so it leads with the thing no
competitor says: **the server never calls a model to write**.

The panel on the right is the data model rather than decoration. It is the
same figure the README opens with, and it carries the cross-tool claim in a
form you can read in two seconds: written by claude-code, read by cursor and
codex.

Colours and the mark are taken from `app/opengraph-image.tsx` in the landing
page repository, so the card and the site agree.


## demo.gif

The thing the README claims, shown instead of described: Claude Code records a
decision with no model call, Cursor recalls it three days later, and the fact
still names who wrote it.

**Every line of output in it is real.** It was captured by running those
commands against a throwaway database, and the capture is kept beside the GIF
in `demo-transcript.txt`. A demo that fabricated its own output would be the
one place this project's numbers were not measured, which is the opposite of
the argument it exists to make.

Two bugs were found making it, which is the argument for filming a product
rather than describing it:

- `echo-memory why` answered "No audit history found" for any fact in the
  other scope, which is most of them (#155)
- every fact written from a source checkout is stamped with the version of
  whatever is installed in the venv, not the code running

### Regenerating it

```bash
python .github/branding/make_demo_gif.py     # writes frames/
ffmpeg -y -framerate 18 -i frames/%05d.png \
  -vf "palettegen=max_colors=96:stats_mode=diff" palette.png
ffmpeg -y -framerate 18 -i frames/%05d.png -i palette.png \
  -lavfi "paletteuse=dither=bayer:bayer_scale=3" demo.gif
```

47KB at 1040x712, 11 seconds. Keep it under a hundred kilobytes: this loads on
every view of the front page, including on a phone.

If the script changes, re-capture the transcript rather than editing the lines
by hand. The point is that it is real.
