  function is_ours(pfx,   n, a, i) {
    n = split(ours, a, " ")
    for (i = 1; i <= n; i++) if (a[i] != "" && pfx == a[i]) return 1
    return 0
  }
  function major(ua, name,   i) {
    i = index(ua, name "/")
    return i ? int(substr(ua, i + length(name) + 1) + 0) : 0
  }
  # Microsoft address ranges that have only ever arrived here as link handling:
  # Skype/Teams URL previews and Defender safe-links detonation. Whois says
  # Microsoft Corporation for every one. Narrow on purpose -- 52.73 in this log
  # is Amazon, so a bare "52." would be wrong -- and only applied to Windows
  # user-agents, so a person on a Mac behind a Microsoft network still counts.
  function ms_link_scanner(pfx) {
    return pfx ~ /^(52\.112\.|52\.123\.|72\.145\.|72\.153\.|2a01:111:)/
  }
  function class(ua, method, pfx,   v) {
    # tolower(): the AI crawlers (ClaudeBot, Claude-SearchBot) matched /bot/ only
    # via their lowercase contact address, not their name. One of them downloads
    # the zip, so a miss here inflates the only number anyone acts on.
    if (tolower(ua) ~ /bot|crawler|spider|slurp|facebookexternalhit|recordedfuture|trendiction/) return "bot"
    if (ua ~ /^ClickGraft\//)                                    return "app"
    if (ua ~ /^(curl|Wget|Python-urllib|Go-http|libwww|ClickGraft-healthcheck)/) return "tool"
    # Everything below claims to be a browser and is not one. Measured against
    # the whole log on 15 Sep 2026, these took views from 162 to 121 and moved
    # no download, and not one reclassified prefix ever downloaded the zip or
    # ran the app.
    #
    # HEAD: a browser never sends it for a page. All of it here was a Cloudflare
    # link checker posing as Chrome 92, in pairs, which had been read as a
    # visitor on WARP. It also keeps a link checker HEADing the zip out of
    # DOWNLOADS.
    if (method == "HEAD")                                        return "bot"
    # A contact URL in the user-agent is a crawler naming itself. A bare
    # "(compatible...)" with nothing after it is the same idea without the name.
    if (index(ua, "+http") || ua ~ /^Mozilla\/[0-9.]+ \(compatible[^)]*\)$/) return "bot"
    # An address instead of a URL, same idea: SkypeUriPreview signs itself
    # skype-url-preview@microsoft.com. In this whole log the only user-agents
    # carrying an address are that one and Anthropic two crawlers; no browser
    # has ever sent one.
    if (ua ~ /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z][A-Za-z]/)  return "bot"
    # Microsoft fetching a link somebody shared in Teams, Skype or Outlook.
    # It reads as an ordinary Windows Chrome, several versions old, that loads
    # the page and then pulls the zip 40 seconds later and never returns: on
    # 17 Sep 2026 two of them downloaded, and on 27 Aug one scanned the
    # ?from=hp-forum link eight times and downloaded once, which is most of
    # what that campaign appeared to have earned. None has ever checked for an
    # update, which is what a person who ran it would do.
    if (ms_link_scanner(pfx) && index(ua, "Windows"))             return "bot"
    # More than 30 majors behind the newest seen (about two and a half years).
    # The largest group this catches: "Firefox/120.0" on 32-bit Linux, loading
    # the page at 05:0x and 07:2x UTC nearly every day from rotating proxies in
    # five countries, never fetching an image. 30 and not 26 so that one Firefox
    # 126 on a Mac in Spain, which did fetch the icon, still counts.
    v = major(ua, "Chrome"); if (v == 0) v = major(ua, "Firefox")
    if (newest > 0 && v > 0 && v < newest - 30)                  return "bot"
    if (ua ~ /Mozilla|AppleWebKit|Gecko|Safari|Chrome|Firefox/)  return "browser"
    return "other"
  }
