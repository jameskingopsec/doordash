# Custom emoji assets

Drop one image per key here and restart the bot — it uploads each file as an
**application emoji** (works in DMs and every server, no permissions needed) and
uses it everywhere that key appears in the UI. Nothing to change in code.

* File name = key. `loading.gif` → the `loading` key.
* `.gif` keeps the animation. `.png` / `.webp` also work.
* Max 256 KB per file, 256×256 recommended.
* Already-uploaded emojis are reused, never duplicated.

Keys the UI uses:

```
back      camera    card      cart      cash      check     clock     close
cross     dominos   link      loading   lock      mail      menu      money
note      person    phone     pin       plus      receipt   refresh   rocket
sparkles  star      store     trash     warning
```

Alternative: if you already have emojis in a server the bot is in, put their
raw strings in `data/emojis.json` instead — those win over everything:

```json
{
  "loading": "<a:loading:1234567890123456789>",
  "check":   "<a:check:1234567890123456789>"
}
```
