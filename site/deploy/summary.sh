#!/bin/sh
# "How much interest is there?" — answered from the access log, on the CT.
#
# GoAccess's report is the detailed view; this is the answer to the question
# actually being asked. No dependencies beyond awk, so it works in the nginx
# container or anywhere the log is mounted.
#
#   docker compose exec -T web sh /srv/summary.sh
set -eu

LOG="${1:-/var/log/nginx/clickgraft-access.log}"
[ -f "$LOG" ] || { echo "no log at $LOG"; exit 1; }

# Field layout of the `privacy` format in nginx.conf:
#   $1 prefix  $4 [date:time  $6 method  $7 path  $9 status  ... last: country
echo "ClickGraft — clickgraft.elusive.net"
echo "log: $LOG   (addresses are truncated at source; no full IPs are kept)"
echo

awk '
  { gsub(/^\[/, "", $4); split($4, d, ":"); day = d[1] }
  $7 == "/" || $7 == "/index.html" { if ($9 == 200 || $9 == 304) views[day]++ ; seen[day"|"$1]++ }
  $7 == "/ClickGraft.zip" && $9 == 200 { dl[day]++ }
  { all[day] = 1 }
  END {
    n = 0
    for (k in seen) { split(k, p, "|"); uniq[p[1]]++ }
    printf "%-12s %8s %8s %8s\n", "DAY", "VIEWS", "UNIQUE", "DOWNLOADS"
    for (day in all) days[n++] = day
    # crude but dependency-free chronological-ish sort on the log date string
    for (i = 0; i < n; i++) for (j = i+1; j < n; j++)
      if (days[i] > days[j]) { t = days[i]; days[i] = days[j]; days[j] = t }
    # `cur`, not `d`: d is already the split() array above, and awk refuses to
    # let a name be both an array and a scalar.
    for (i = 0; i < n; i++) {
      cur = days[i]
      printf "%-12s %8d %8d %8d\n", cur, views[cur]+0, uniq[cur]+0, dl[cur]+0
      tv += views[cur]; tu += uniq[cur]; td += dl[cur]
    }
    printf "%-12s %8d %8d %8d\n", "TOTAL", tv, tu, td
    if (tv > 0) printf "\nconversion: %.1f%% of page views started a download\n", (td*100.0)/tv
  }
' "$LOG"

echo
echo "WHERE THEY CAME FROM"
# Splitting on the quote character: field 2 is the request, 4 the referrer,
# 6 the user-agent, 8 the country. Counting from the left, not from $NF.
awk -F'"' '$4 != "-" && $4 != "" { split($4, u, "/"); host = u[3]; if (host != "") c[host]++ }
           END { for (h in c) printf "%8d  %s\n", c[h], h }' "$LOG" \
  | sort -rn | head -15
echo "  (blank means typed in directly or the referrer was withheld)"

echo
echo "COUNTRIES"
awk -F'"' '{ gsub(/^ +| +$/, "", $8); if ($8 != "" && $8 != "-") c[$8]++ }
           END { for (k in c) printf "%8d  %s\n", c[k], k }' "$LOG" \
  | sort -rn | head -12
