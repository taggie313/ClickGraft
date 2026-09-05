#!/bin/sh
# "How much interest is there?" — answered from the access log, on the CT.
#
#   sh summary.sh [logfile]
#
# Classifies by USER-AGENT rather than by excluding our own addresses. That
# earlier approach depended on knowing which IP we were coming from, and the
# answer changed three times in two days while travelling — a hand-typed curl
# from a phone hotspot was reported as a second real download for a day before
# the pattern gave it away.
#
# A request now has to look like a browser to count as a visit. Anything else
# is still shown, just under its own heading where it cannot be mistaken for a
# person. Being wrong about which bucket something lands in is recoverable;
# silently inflating the only numbers anyone acts on is not.
set -eu

LOG="${1:-/var/log/nginx/clickgraft-access.log}"
[ -f "$LOG" ] || { echo "no log at $LOG"; exit 1; }

echo "ClickGraft — clickgraft.elusive.net"
echo "log: $LOG   (addresses are truncated at source; no full IPs are kept)"
echo

awk -F'"' '
  # "27/Aug/2026" -> "20260827", so days sort by date instead of by text.
  # A plain string compare orders the day-of-month first, which was invisible
  # while the log held one month and put 01/Sep above 02/Aug the moment it held
  # two. No apostrophes in here - the whole awk program is single quoted.
  function daykey(d,   parts, pos) {
    # Both guards matter. index() finds the empty string at position 1, so a
    # value that does not split into three parts would silently key as January
    # rather than falling back.
    if (split(d, parts, "/") != 3) return d
    pos = index("JanFebMarAprMayJunJulAugSepOctNovDec", parts[2])
    if (pos == 0) return d
    return parts[3] sprintf("%02d%02d", int((pos + 2) / 3), parts[1])
  }
  function class(ua) {
    # tolower(): the AI crawlers (ClaudeBot, Claude-SearchBot) matched /bot/ only
    # via their lowercase contact address, not their name. One of them downloads
    # the zip, so a miss here inflates the only number anyone acts on.
    if (tolower(ua) ~ /bot|crawler|spider|slurp|facebookexternalhit|recordedfuture|trendiction/) return "bot"
    if (ua ~ /^ClickGraft\//)                                    return "app"
    if (ua ~ /^(curl|Wget|Python-urllib|Go-http|libwww|ClickGraft-healthcheck)/) return "tool"
    if (ua ~ /Mozilla|AppleWebKit|Gecko|Safari|Chrome|Firefox/)  return "browser"
    return "other"
  }
  {
    split($1, f, " "); pfx = f[1]
    split($2, r, " "); path = r[2]
    # s[1], not s[2]: awk given " " as the separator splits on runs of
    # whitespace and discards leading blanks, so field 3 " 200 6529 " yields
    # s[1]=status, s[2]=bytes. Reading s[2] silently compared byte counts to
    # 200 and every view and download counted zero.
    split($3, s, " "); status = s[1]
    ua = $6; ref = $4; camp = $10
    d = f[4]; gsub(/^\[/, "", d); split(d, dd, ":"); day = dd[1]
    c = class(ua)
    seen[c]++
    if (c == "browser") {
      all[day] = 1
      if (path == "/" || path == "/index.html") { if (status ~ /^(200|304)$/) { v[day]++; u[day "|" pfx] = 1 } }
      if (path == "/ClickGraft.zip" && status == "200") dl[day]++
      # The HP forum sends no referrer at all (a no-referrer policy), so a
      # tagged link is the only way traffic from there can be told apart from
      # someone typing the URL. No apostrophes here - see the note below.
      if (camp != "") { if (!(camp in camps)) ncamp++; camps[camp]++ }
      # Self-referrals are the page asking for its own favicon, not a source.
      if (ref != "-" && ref != "") {
        split(ref, x, "/")
        if (x[3] != "" && x[3] !~ /clickgraft\.elusive\.net/) {
          if (!(x[3] in refs)) nref++
          refs[x[3]]++
        }
      }
    }
    if (c == "app") { appseen[pfx] = 1; appreq++ }
  }
  END {
    printf "%-12s %8s %8s %8s\n", "DAY", "VIEWS", "UNIQUE", "DOWNLOADS"
    n = 0; for (day in all) days[n++] = day
    for (i = 0; i < n; i++) for (j = i+1; j < n; j++)
      if (daykey(days[i]) > daykey(days[j])) { t = days[i]; days[i] = days[j]; days[j] = t }
    for (i = 0; i < n; i++) {
      cur = days[i]; uu = 0
      # parts, not p: p is the loop variable in the appseen loop below, and
      # mawk refuses a name used as both array and scalar. No apostrophes in
      # here either - the whole awk program is single quoted.
      for (k in u) { split(k, parts, "|"); if (parts[1] == cur) uu++ }
      printf "%-12s %8d %8d %8d\n", cur, v[cur]+0, uu, dl[cur]+0
      tv += v[cur]; tu += uu; td += dl[cur]
    }
    printf "%-12s %8d %8d %8d\n", "TOTAL", tv, tu, td
    print ""
    print "BROWSERS ONLY. Everything else is below, where it cannot be"
    print "mistaken for a person."
    print ""
    # Prefixes, NOT machines. A truncated address is all we keep, so one
    # laptop that moves between networks counts several times and an office of
    # Macs behind one NAT counts once. Reported as what it is: this was quoted
    # as "3 machines" when it was one stranger and the same test Mac twice.
    ni = 0; for (ap in appseen) ni++
    printf "  %-34s %d request(s) from %d address prefix(es)\n", "the app checking for updates:", appreq+0, ni
    print  "                                     (prefixes, not machines - one roaming"
    print  "                                      laptop counts twice, an office counts once)"
    printf "  %-34s %d\n", "crawlers:", seen["bot"]+0
    printf "  %-34s %d\n", "command line (curl/wget/etc):", seen["tool"]+0
    printf "  %-34s %d\n", "unclassified:", seen["other"]+0
    print ""
    print "WHERE THEY CAME FROM"
    for (h in refs) printf "%8d  %s\n", refs[h], h
    # length(array) is a gawk extension; the CT runs mawk.
    if (nref+0 == 0) print "       (none recorded)"
    print ""
    print "TAGGED LINKS (?from=)"
    for (t in camps) printf "%8d  %s\n", camps[t], t
    if (ncamp+0 == 0) print "       (none recorded)"
  }
' "$LOG"

echo "  (blank means typed in directly or the referrer was withheld)"
echo
echo "COUNTRIES (browsers only)"
awk -F'"' '$6 ~ /Mozilla|AppleWebKit|Gecko/ && $6 !~ /bot|crawler|spider/ {
    gsub(/^ +| +$/, "", $8); if ($8 != "" && $8 != "-") c[$8]++ }
  END { for (k in c) printf "%8d  %s\n", c[k], k }' "$LOG" | sort -rn | head -12
