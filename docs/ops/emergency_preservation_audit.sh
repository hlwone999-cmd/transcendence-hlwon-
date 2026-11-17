#!/usr/bin/env bash
# Transcendence MT5 Journal — EMERGENCY PRESERVATION AUDIT (READ-ONLY) v3
#
# Jalankan di VPS sebagai root:
#     sudo bash emergency_preservation_audit.sh
# Skrip menurunkan prioritasnya sendiri (nice -n 19, ionice -c3) — hanya
# untuk prosesnya sendiri. Semua keluaran ke stdout; skrip TIDAK menulis
# berkas apa pun.
#
# Jaminan:
#  - TIDAK restart/stop/start service; TIDAK mengubah file, permission,
#    firewall, DNS, Nginx, konfigurasi; TIDAK menjalankan migration;
#    TIDAK membuat backup; TIDAK menyentuh terminal MT5 / port RPyC.
#  - PostgreSQL: PGOPTIONS diteruskan EKSPLISIT lewat `sudo ... env`;
#    setiap koneksi diawali guard yang GAGAL bila
#    default_transaction_read_only != on; wrapper hanya meneruskan
#    SELECT/SHOW; statement_timeout 30 s, lock_timeout 2 s.
#  - Default TANPA COUNT(*) eksak (estimasi reltuples). COUNT eksak hanya
#    untuk tabel di AUDIT_EXACT_COUNT_TABLES (setelah persetujuan).
#  - Secret — perlindungan BERLAPIS:
#      (1) struktural: nilai .env/JSON, isi log, isi konfigurasi
#          (logrotate, cron, nginx, firewall), command line proses, dan
#          argumen ExecStart TIDAK PERNAH dibaca ke keluaran; yang dicetak
#          hanya nama kunci, metadata (path, ukuran, izin, waktu), nama
#          executable, atau ringkasan terstruktur;
#      (2) filter redaksi seluruh keluaran (DSN, KEY=VALUE sensitif, flag
#          --token/--secret/--api-key/--password/-p, Authorization/Bearer,
#          blok private key, string berentropi tinggi) sebagai lapisan
#          cadangan, BUKAN satu-satunya perlindungan.
#  - Status per bagian: PASS / PARTIAL / FAILED / NOT AVAILABLE /
#    SKIPPED; jumlah query PostgreSQL gagal; "AUDIT COMPLETE" hanya bila
#    skrip selesai secara teknis DAN tidak ada pemeriksaan wajib yang gagal.
#
# Beban disk (default audit pertama: RINGAN):
#  - AUDIT_SKIP_DU=1 adalah DEFAULT. `du` menelusuri seluruh pohon
#    direktori (metadata setiap berkas) — untuk Wine prefix/terminal bisa
#    jutaan entri. Aktifkan HANYA setelah persetujuan: AUDIT_SKIP_DU=0.
#    Walau aktif, direktori terminal/Wine hanya ditelusuri bila
#    AUDIT_DU_TERMINALS=1 (persetujuan terpisah).
#  - `find` tidak pernah masuk ke direktori terminal/Wine (di-prune).
#  - Pemeriksaan isi dump (gzip -t, footer, sha256) membaca seluruh berkas:
#    default MATI; AUDIT_VERIFY_DUMPS=1 setelah persetujuan.
set -u
export LC_ALL=C
: "${AUDIT_SKIP_DU:=1}"
: "${AUDIT_DU_TERMINALS:=0}"
: "${AUDIT_VERIFY_DUMPS:=0}"
: "${AUDIT_EXACT_COUNT_TABLES:=}"
export AUDIT_SKIP_DU AUDIT_DU_TERMINALS AUDIT_VERIFY_DUMPS AUDIT_EXACT_COUNT_TABLES

# --- prioritas rendah (hanya proses skrip ini; tidak mengubah sistem)
if [ -z "${_AUDIT_NICED:-}" ]; then
  export _AUDIT_NICED=1
  if command -v nice >/dev/null 2>&1 && command -v ionice >/dev/null 2>&1; then
    exec nice -n 19 ionice -c3 bash "$0" "$@"
  elif command -v nice >/dev/null 2>&1; then
    exec nice -n 19 bash "$0" "$@"
  fi
fi

PGOPTS='-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=60000'
# Guard: SELECT yang gagal (division by zero) bila sesi TIDAK read-only.
# Dengan ON_ERROR_STOP=1, perintah berikutnya di sesi itu tidak dijalankan.
RO_GUARD="SELECT 1/(pg_catalog.current_setting('default_transaction_read_only')='on')::int AS ro_guard"
TERM_BASE=/var/lib/transcendence/terminals

# ------------------------------------------------------------ status
# Penanda status dikirim ke STDERR (fd 2) agar tidak tercampur ke hasil
# $(...). main dijalankan dengan 2>&1 sehingga penanda tetap masuk ke
# aliran, lalu diringkas oleh summarize() di akhir.
#   @@CHK <bagian> <req|opt> <PASS|FAIL|NA|SKIP> <label-tanpa-spasi>
#   @@SEC <bagian> <judul>   @@NA <bagian> <alasan>   @@END
CUR=0
sec() { CUR="$1"; shift; printf '\n===== %s. %s =====\n' "$CUR" "$*"; printf '@@SEC %s %s\n' "$CUR" "$*" >&2; }
sub() { printf -- '--- %s\n' "$1"; }
chk() { printf '@@CHK %s %s %s %s\n' "$CUR" "$1" "$2" "$(printf '%s' "$3" | tr -c 'A-Za-z0-9_.:/-' '_')" >&2; }
ok()   { chk "$1" PASS "$2"; }
fail() { chk "$1" FAIL "$2"; echo "[GAGAL] $(printf '%s' "$2" | tr '_' ' ')"; }
na()   { chk opt NA "$1"; }
skip() { chk opt SKIP "$1"; echo "[DILEWATI] $(printf '%s' "$1" | tr '_' ' ')"; }
na_section() { printf '@@NA %s %s\n' "$CUR" "$1" >&2; echo "[TIDAK TERSEDIA] $1"; }
have() { command -v "$1" >/dev/null 2>&1; }
# run_req/run_opt <label> <perintah...>: jalankan, catat PASS/FAIL dari rc
run_req() { local l="$1"; shift; if "$@"; then ok req "$l"; else fail req "$l"; fi; }
run_opt() { local l="$1"; shift; if "$@"; then ok opt "$l"; else fail opt "$l"; fi; }

# psqlq [-d DB] [-F SEP] -c "SELECT|SHOW ..." [-c ...]
# rc != 0 -> penanda @@PGFAIL; pesan galat psql ke stderr (tidak masuk $()).
psqlq() {
  local args=() a next_is_cmd=0 out rc
  for a in "$@"; do
    if [ "$next_is_cmd" = 1 ]; then
      if ! printf '%s' "$a" | grep -Eqi '^[[:space:]]*(SELECT|SHOW)[[:space:]]'; then
        echo "AUDIT-ABORT: query bukan SELECT/SHOW ditolak wrapper" >&2
        printf '@@PGFAIL %s rejected\n' "$CUR" >&2; return 97
      fi
      if printf '%s' "$a" | grep -Eqi '\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|ANALYZE|REINDEX|CLUSTER|LOCK|NOTIFY|SET[[:space:]]|RESET|DISCARD|CALL|DO|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|set_config|lo_|dblink)\b'; then
        echo "AUDIT-ABORT: kata kunci terlarang di query" >&2
        printf '@@PGFAIL %s rejected\n' "$CUR" >&2; return 97
      fi
      next_is_cmd=0
    fi
    [ "$a" = "-c" ] && next_is_cmd=1
    args+=("$a")
  done
  # PGOPTIONS & application_name diteruskan EKSPLISIT (sudo mereset env)
  out=$(sudo -n -u postgres env PGOPTIONS="$PGOPTS" PGAPPNAME=trx-preservation-audit \
        psql -X -A -t -q -v ON_ERROR_STOP=1 -c "$RO_GUARD" "${args[@]}" 2>/dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '@@PGFAIL %s rc=%s\n' "$CUR" "$rc" >&2
    echo "[GAGAL] query PostgreSQL (rc=$rc)" >&2
    return "$rc"
  fi
  printf '%s\n' "$out" | sed '1{/^1$/d}'
}
# psql_req <label> <args...>: cetak hasil; catat PASS/FAIL wajib
psql_req() { local l="$1"; shift; if psqlq "$@"; then ok req "pg:$l"; else fail req "pg:$l"; fi; }

meta() { stat -c '%n | %s B | %a %U:%G | mtime %y' "$1" 2>/dev/null || { echo "$1 | (tidak terbaca)"; return 1; }; }
keys_only() {
  grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$1" 2>/dev/null \
    | sed -E 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/' | sort -u | tr '\n' ' '
  echo
}
is_terminal_path() { case "$1" in "$TERM_BASE"*|*/.wine*|*/wine-prefix*|/home/jurnal/mt5*) return 0;; esac; return 1; }
dus() {
  if [ "$AUDIT_SKIP_DU" = 1 ]; then echo "(du dilewati — AUDIT_SKIP_DU=1)"; return 0; fi
  if is_terminal_path "$1" && [ "$AUDIT_DU_TERMINALS" != 1 ]; then echo "(du terminal/Wine dilewati — AUDIT_DU_TERMINALS=0)"; return 0; fi
  du -sh "$1" 2>/dev/null | cut -f1
}
exe_of_unit() { # hanya path executable dari ExecStart (tanpa argumen)
  systemctl show "$1" -p ExecStart --no-pager 2>/dev/null \
    | grep -o 'path=[^ ;]*' | head -1 | sed 's/^path=//'
}
# pointer berkas: cetak HANYA bila cocok format yang diharapkan
pointer_value() { # $1 berkas, $2 regex
  local v; v=$(head -c 200 "$1" 2>/dev/null | head -1)
  if printf '%s' "$v" | grep -Eq "$2"; then printf '%s' "$v" | cut -c1-48; else echo "(format tidak dikenal — tidak ditampilkan)"; fi
}

cron_summary() { # stdin: baris cron; $1 = "system" (ada kolom user) / "user"
  awk -v kind="$1" '
    /^[[:space:]]*(#|$)/ { next }
    /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ {
      n=$0; sub(/[[:space:]]*=.*/, "", n); gsub(/^[[:space:]]+/, "", n)
      print "env-var: " n " = [REDACTED]"; next }
    {
      i=1
      if ($1 ~ /^@/) { sched=$1; i=2 } else { sched=$1" "$2" "$3" "$4" "$5; i=6 }
      user="-"; if (kind=="system") { user=$i; i++ }
      while (i<=NF && $i ~ /^[A-Za-z_][A-Za-z0-9_]*=/) i++
      exe=$i; n=split(exe, p, "/"); base=p[n]
      print "jadwal [" sched "] user [" user "] exe [" base "] argumen [REDACTED]"
    }'
}
nginx_summary() { # stdin: keluaran nginx -T
  awk '
    /^[[:space:]]*#/ { next }
    $1=="server_name" || $1=="listen" || $1=="ssl_certificate" || $1=="root" { print; next }
    $1=="ssl_certificate_key" { print "    ssl_certificate_key <path tersedia; isi tidak dibaca>"; next }
    /proxy_pass[[:space:]]/ {
      t=$0; sub(/.*proxy_pass[[:space:]]+/, "", t); sub(/[;[:space:]].*/, "", t)
      lo = (t ~ /^(https?:\/\/)?(127\.0\.0\.1|localhost|\[::1\])([:\/;]|$)/ || t ~ /^(https?:\/\/)?unix:/)
      print "    proxy_pass <disembunyikan> upstream-loopback=" (lo ? "ya" : "TIDAK/periksa manual"); next }'
}

# ---------------------------------------------------------- filter redaksi (lapisan cadangan)
REDACT_PY='
import math, re, sys
SENS = r"(?:pass(?:word|wd|phrase)?|secret|token|api[_-]?key|apikey|key|auth\w*|cred(?:ential)?s?|dsn|url|uri|conn(?:ection)?_?str(?:ing)?|cookie|session|signature|private|bearer|salt|otp)"
rules = [
  # userinfo pada URL apa pun (termasuk user kosong: redis://:pw@)
  (re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]*@", re.I), r"\1[REDACTED]@"),
  # KEY=VALUE / key: value / "key": "value" dengan nama sensitif
  (re.compile(r"((?:\b|\")[A-Za-z0-9_.-]*" + SENS + r"[A-Za-z0-9_.-]*\"?\s*[=:]\s*)(\"[^\"]*\"|\x27[^\x27]*\x27|[^\s,;]+)", re.I), r"\1[REDACTED]"),
  # flag CLI: --token X, --secret=X, --api-key X, --password=X, -p X
  (re.compile(r"((?:^|\s)--?(?:" + SENS + r"|p)\b[=\s]+)(\S+)", re.I), r"\1[REDACTED]"),
  (re.compile(r"(authorization\s*[:=]\s*)(.+)", re.I), r"\1[REDACTED]"),
  (re.compile(r"(\bbearer\s+)(\S+)", re.I), r"\1[REDACTED]"),
]
ENT = re.compile(r"(?<![A-Za-z0-9+/=_.-])[A-Za-z0-9+/=_-]{24,}(?![A-Za-z0-9+/=_-])")
IDENT = re.compile(r"^(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+)$")
def shannon(s):
    n = len(s); return -sum(c/n*math.log2(c/n) for c in (s.count(ch) for ch in set(s)))
def entropic(m):
    s = m.group(0)
    if s.startswith("/") and "+" not in s and "=" not in s:
        return s                      # path absolut
    if re.fullmatch(r"[0-9a-fA-F]{24,}", s):
        return "[REDACTED-HIGH-ENTROPY]"
    if IDENT.match(s):
        return s                      # nama identifier (ENV_VAR, unit-name)
    if shannon(s) >= 3.5:
        return "[REDACTED-HIGH-ENTROPY]"
    return s
in_key = False
MARK = re.compile(r"^@@(CHK|SEC|NA|PGFAIL|END)( [^\x00-\x1f]*)?\n?$")
for line in sys.stdin:
    if len(line) < 300 and MARK.match(line):
        sys.stdout.write(line); continue      # penanda status buatan skrip (label sudah disanitasi)
    if re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", line):
        in_key = True
    if in_key:
        if re.search(r"-----END [A-Z0-9 ]*PRIVATE KEY-----", line):
            in_key = False
        sys.stdout.write("[REDACTED-PRIVATE-KEY-LINE]\n"); continue
    for rx, rep in rules:
        line = rx.sub(rep, line)
    line = ENT.sub(entropic, line)
    sys.stdout.write(line)
'
redact() {
  if have python3; then python3 -I -c "$REDACT_PY"
  else echo "AUDIT-ABORT: python3 tidak ada — redaksi tidak dapat dijamin, keluaran ditahan" ; cat >/dev/null; fi
}

main() {
echo "AUDIT READ-ONLY v3 dimulai $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
echo "nice=$(nice) ionice=$(ionice 2>/dev/null || echo n/a) AUDIT_SKIP_DU=$AUDIT_SKIP_DU AUDIT_DU_TERMINALS=$AUDIT_DU_TERMINALS AUDIT_VERIFY_DUMPS=$AUDIT_VERIFY_DUMPS AUDIT_EXACT_COUNT_TABLES=${AUDIT_EXACT_COUNT_TABLES:-<kosong>}"
[ "$(id -u)" = 0 ] && ok req "root" || fail req "harus_root_untuk_inventaris_lengkap"

# ------------------------------------------------------------------ 1
sec 1 "SERVER: OS, kernel, CPU, RAM, swap, disk, inode, timezone"
if [ -r /etc/os-release ]; then . /etc/os-release; echo "OS: $PRETTY_NAME"; ok req os-release; else fail req os-release; fi
echo "kernel: $(uname -r)  arch: $(uname -m)"
have systemd-detect-virt && echo "virt: $(systemd-detect-virt 2>/dev/null)"
echo "CPU: $(nproc) vCPU — $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ //')"
run_req free free -h
swapon --show 2>/dev/null || echo "(tanpa swap)"
sub "disk"; run_req df df -hT -x tmpfs -x devtmpfs -x squashfs
sub "inode"; run_req df-inode df -i -x tmpfs -x devtmpfs -x squashfs
sub "waktu"; if have timedatectl && timedatectl 2>/dev/null | sed -n '1,8p'; then ok opt timedatectl; else date; fail opt timedatectl; fi
uptime
sub "top 10 RSS (nama executable saja)"
run_opt ps ps -eo pid,user,rss,etime,comm --sort=-rss | head -11

# ------------------------------------------------------------------ 2
sec 2 "POSTGRESQL (read-only, tanpa COUNT eksak secara default)"
if ! have psql || ! id postgres >/dev/null 2>&1; then
  na_section "psql / user postgres tidak ada"; fail req postgresql_tidak_tersedia
else
  have pg_lsclusters && pg_lsclusters
  sub "verifikasi sesi read-only"
  v=$(psqlq -c "SHOW default_transaction_read_only")
  if [ "$v" = on ]; then echo "default_transaction_read_only = on"; ok req pg_read_only_on; else fail req pg_read_only_tidak_terbukti; fi
  psql_req settings -c "SHOW statement_timeout" -c "SELECT pg_catalog.current_setting('application_name')"
  psql_req version -c "SELECT 'version: '||pg_catalog.version()"
  DD=$(psqlq -c "SHOW data_directory") && { echo "data_directory: $DD"; ok req pg_data_directory; } || fail req pg_data_directory
  for s in server_encoding TimeZone wal_level archive_mode max_wal_size data_checksums; do
    if v=$(psqlq -c "SHOW $s"); then echo "$s = $v"; else echo "$s = (gagal dibaca)"; fail opt "pg_show_$s"; fi
  done
  if v=$(psqlq -c "SELECT CASE WHEN pg_catalog.current_setting('archive_command') IN ('','(disabled)') THEN 'kosong' ELSE 'TERISI (isi disembunyikan)' END"); then echo "archive_command = $v"; else echo "archive_command = (gagal dibaca)"; fail opt pg_archive_command; fi
  sub "database & ukuran"
  psql_req databases -F ' | ' -c "SELECT datname, pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(datname)), pg_catalog.pg_database_size(datname), datcollate, pg_catalog.pg_encoding_to_char(encoding) FROM pg_catalog.pg_database WHERE NOT datistemplate ORDER BY 3 DESC"
  sub "tablespace"; psql_req tablespaces -F ' | ' -c "SELECT spcname, pg_catalog.pg_tablespace_location(oid) FROM pg_catalog.pg_tablespace"
  sub "role (TANPA password)"
  psql_req roles -F ' | ' -c "SELECT rolname, rolsuper, rolcanlogin, rolreplication, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_catalog.pg_roles WHERE rolname !~ '^pg_' ORDER BY 1"
  if DBS=$(psqlq -c "SELECT datname FROM pg_catalog.pg_database WHERE NOT datistemplate AND datname<>'postgres' ORDER BY 1"); then
    ok req pg_database_list
  else
    DBS=""; fail req pg_database_list
  fi
  for db in $DBS; do
    printf '%s' "$db" | grep -Eq '^[A-Za-z0-9_]+$' || { echo "(nama DB tidak biasa dilewati)"; fail opt pg_db_name_unusual; continue; }
    sub "DB $db: ekstensi"
    psql_req "ext_$db" -d "$db" -F ' | ' -c "SELECT extname, extversion FROM pg_catalog.pg_extension ORDER BY 1"
    sub "DB $db: schema_migrations"
    r=$(psqlq -d "$db" -c "SELECT pg_catalog.to_regclass('public.schema_migrations') IS NOT NULL")
    case "$r" in
      t) psql_req "migrations_$db" -d "$db" -c "SELECT pg_catalog.string_agg(version::text, ',' ORDER BY version) FROM public.schema_migrations" ;;
      f) echo "(tidak ada schema_migrations)" ;;
      *) fail req "migrations_check_$db" ;;
    esac
    sub "DB $db: tabel (ukuran, ESTIMASI baris reltuples, punya PK)"
    psql_req "tables_$db" -d "$db" -F ' | ' -c "SELECT n.nspname||'.'||c.relname, pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)), pg_catalog.pg_total_relation_size(c.oid), c.reltuples::bigint, EXISTS (SELECT 1 FROM pg_catalog.pg_index i WHERE i.indrelid=c.oid AND i.indisprimary) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 3 DESC"
    sub "DB $db: terminal MT5 (maks 50 baris, tanpa data customer)"
    r=$(psqlq -d "$db" -c "SELECT pg_catalog.to_regclass('public.mt5_terminals') IS NOT NULL")
    case "$r" in
      t) psql_req "terminals_$db" -d "$db" -F ' | ' -c "SELECT rpc_mode, lifecycle, status, (op_owner IS NOT NULL) AS held FROM public.mt5_terminals ORDER BY id LIMIT 50" ;;
      f) echo "(tidak ada mt5_terminals)" ;;
      *) fail req "terminals_check_$db" ;;
    esac
    if [ -n "$AUDIT_EXACT_COUNT_TABLES" ]; then
      sub "DB $db: COUNT eksak (DISETUJUI via AUDIT_EXACT_COUNT_TABLES)"
      for t in $(printf '%s' "$AUDIT_EXACT_COUNT_TABLES" | tr ',' ' '); do
        printf '%s' "$t" | grep -Eq '^[a-z_][a-z0-9_]*$' || { echo "$t: nama tidak sah, dilewati"; continue; }
        [ "$(psqlq -d "$db" -c "SELECT pg_catalog.to_regclass('public.$t') IS NOT NULL")" = t ] || { echo "$t: tidak ada"; continue; }
        n=$(psqlq -d "$db" -c "SELECT count(*) FROM public.$t") && echo "$t = $n" || fail opt "count_$t"
      done
    else
      skip "COUNT_eksak_belum_disetujui"
    fi
    sub "DB $db: sesi aktif (tanpa teks query)"
    psqlq -d "$db" -F ' | ' -c "SELECT usename, application_name, state, backend_type, count(*) FROM pg_catalog.pg_stat_activity WHERE datname=pg_catalog.current_database() GROUP BY 1,2,3,4" || fail opt "activity_$db"
  done
  if [ -n "${DD:-}" ] && [ -d "$DD" ]; then echo "ukuran data_directory: $(dus "$DD")   WAL: $(dus "$DD/pg_wal")"; fi
fi
sub "SQLite (layout lama, metadata)"
for f in /opt/jurnal-mt5/backend/data/*.db /opt/*/backend/data/*.db; do [ -f "$f" ] && meta "$f"; done

# ------------------------------------------------------------------ 3
sec 3 "REDIS (repo backend TIDAK memakai Redis — cek saja)"
if have redis-cli; then
  for u in redis-server redis; do echo "$u: $(systemctl is-active "$u" 2>/dev/null || true)"; done
  if redis-cli INFO persistence 2>/dev/null | grep -E '^(rdb_last_save_time|aof_enabled|rdb_changes_since_last_save|loading):' ; then ok opt redis_info; else fail opt redis_info; fi
else
  na_section "redis-cli tidak terpasang"
fi

# ------------------------------------------------------------------ 4
sec 4 "SERVICE: backend, worker, scheduler, nginx, systemd, cron, timer"
if ! have systemctl; then
  na_section "systemctl tidak ada"; fail req systemctl_tidak_tersedia
else
  if UNITS=$(systemctl list-units --type=service --all --no-pager --plain --no-legend 2>/dev/null) && [ -n "$UNITS" ]; then
    ok req systemd_list_units
    printf '%s\n' "$UNITS" | grep -Ei 'transcend|jurnal|journal|mt5|wine|xvfb|nginx|postgres|redis|caddy' || echo "(tidak ada unit terkait)"
  else
    fail req systemd_list_units
  fi
  sub "unit-file terkait"
  systemctl list-unit-files --no-pager --plain 2>/dev/null | grep -Ei 'transcend|jurnal|mt5|xvfb' || echo "(tidak ada)"
  for u in $(printf '%s\n' "${UNITS:-}" | awk '{print $1}' | grep -Ei 'transcend|jurnal|mt5'); do
    sub "unit $u"
    systemctl show "$u" -p FragmentPath -p User -p WorkingDirectory -p EnvironmentFiles -p ActiveState -p SubState -p ExecMainStartTimestamp --no-pager 2>/dev/null || fail opt "unit_show_$u"
    echo "ExecStart executable: $(exe_of_unit "$u")  [argumen tidak ditampilkan]"
    systemctl show "$u" -p Environment --no-pager 2>/dev/null | tr ' ' '\n' \
      | sed -nE 's/^(Environment=)?([A-Za-z_][A-Za-z0-9_]*)=.*/env-name: \2/p'
  done
  sub "timer"
  if systemctl list-timers --all --no-pager 2>/dev/null | head -30; then ok opt timers; else fail opt timers; fi
fi
sub "cron (jadwal, user, nama executable — argumen disembunyikan)"
for f in /etc/crontab /etc/cron.d/*; do
  [ -f "$f" ] || continue
  if [ -r "$f" ]; then echo "# $f"; cron_summary system < "$f"; else fail req "cron_unreadable_$f"; fi
done
ok req cron_files
if have crontab; then
  for u in $(cut -d: -f1 /etc/passwd); do
    c=$(crontab -l -u "$u" 2>/dev/null) && [ -n "$c" ] && { echo "# crontab $u"; printf '%s\n' "$c" | cron_summary user; }
  done
  ok opt user_crontabs
else
  na "crontab_tidak_ada"
fi
sub "port LISTEN (alamat & nama proses saja)"
if have ss; then
  ss -ltnH 2>/dev/null | awk '{print $4}' | sort -u
  ss -ltnpH 2>/dev/null | grep -o 'users:(("[^"]*"' | sed 's/users:(("//' | sort | uniq -c
  ok req ss_listen
else
  fail req ss_tidak_ada
fi
sub "proses aplikasi (nama executable saja)"
ps -eo pid,user,etime,comm 2>/dev/null | grep -Ei 'python|waitress|gunicorn|mt5server|terminal64|wine|xvfb|postgres|nginx' | grep -v grep
sub "nginx (ringkasan; proxy_pass disembunyikan)"
if have nginx; then
  if NGX=$(nginx -T 2>/dev/null); then printf '%s\n' "$NGX" | nginx_summary | sort -u; ok req nginx_config_read; else fail req nginx_config_read; fi
  ls -la /etc/nginx/sites-enabled/ 2>/dev/null
else
  na "nginx_tidak_terpasang"; echo "nginx tidak terpasang"
fi
sub "sertifikat TLS (publik: subject & kedaluwarsa)"
for c in /etc/letsencrypt/live/*/fullchain.pem; do
  [ -f "$c" ] && echo "$c :: $(openssl x509 -in "$c" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
done
sub "kode aplikasi (git, read-only)"
for d in /opt/transcendence-hlwon- /opt/jurnal-mt5 /opt/transcendence /srv/transcendence*; do
  [ -d "$d" ] || continue
  echo "# $d  ukuran: $(dus "$d")"
  if [ -d "$d/.git" ]; then
    git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null
    git -C "$d" log -1 --format='%h %cI' 2>/dev/null || fail opt "git_log_$d"
    echo "perubahan lokal belum di-commit: $(git -C "$d" status --porcelain 2>/dev/null | wc -l) berkas"
    git -C "$d" status --porcelain 2>/dev/null | head -20
  fi
done

# ------------------------------------------------------------------ 5
sec 5 "TERMINAL MT5 / WINE (tanpa penelusuran penuh)"
if [ -d "$TERM_BASE" ]; then
  ok req terminal_base_ada
  echo "# $TERM_BASE total: $(dus "$TERM_BASE")"
  for t in "$TERM_BASE"/*/; do
    t=${t%/}
    [ -d "$t" ] || continue
    code=$(basename "$t")
    mode=$(grep -ho '"rpc_mode"[[:space:]]*:[[:space:]]*"[a-z]*"' "$t/.install_complete" 2>/dev/null | head -1)
    echo "$code | total $(dus "$t") | marker $( [ -f "$t/.provision_id" ] && echo ada || echo TIDAK) | ${mode:-rpc_mode ?}"
    for d in wine-prefix terminal data terminal/bases terminal/logs terminal/MQL5; do
      [ -d "$t/$d" ] && echo "   $d: ada ($(dus "$t/$d"))"
    done
    for sens in "$t/terminal/rpc.token" "$t/terminal/config/accounts.dat"; do [ -e "$sens" ] && echo "   sensitif: $(meta "$sens")"; done
  done
  for x in "$TERM_BASE"/.tombstones "$TERM_BASE"/.fence; do [ -d "$x" ] && echo "$x: $(ls -1 "$x" 2>/dev/null | wc -l) entri"; done
else
  na_section "$TERM_BASE tidak ada (mungkin layout lama)"
fi
for p in /opt/transcendence/mt5-template /home/jurnal/.wine-mt5 /home/jurnal/mt5; do
  [ -d "$p" ] && echo "$p : ada ($(dus "$p"))"
done
sub "unit terminal"
systemctl list-units --all --no-pager --plain 2>/dev/null | grep -E 'transcendence-mt5-|mt5server' || echo "(tidak ada)"
sub "helper provisioning (metadata saja)"
for h in /usr/local/sbin/transcendence-mt5-provision /usr/local/libexec/transcendence*; do [ -e "$h" ] && meta "$h"; done

# ------------------------------------------------------------------ 6
sec 6 "LOKASI SECRET / KREDENSIAL / KUNCI (NILAI TIDAK DIBACA KE KELUARAN)"
found_env=0
for f in /opt/transcendence-hlwon-/backend/.env /opt/jurnal-mt5/backend/.env /opt/*/backend/.env /etc/default/transcendence-backend /etc/transcendence*/*.env; do
  [ -f "$f" ] || continue
  found_env=1
  echo "# $(meta "$f")"
  if [ -r "$f" ]; then
    printf '  kunci: '; keys_only "$f"
    for k in MT5_CRED_KEY MT5_CRED_KEY_OLD MT5_RPC_SECRET DATABASE_URL DATABASE_SYSTEM_URL API_TOKEN; do
      grep -qE "^[[:space:]]*(export[[:space:]]+)?$k=.+" "$f" && echo "  $k: TERISI" || echo "  $k: kosong/tidak ada"
    done
  else
    fail req "env_unreadable"
  fi
done
[ "$found_env" = 1 ] && ok req env_ditemukan || fail req env_backend_tidak_ditemukan_MT5_CRED_KEY_tidak_terlokasi
f=/etc/transcendence/provisioner.json
if [ -f "$f" ]; then
  echo "# $(meta "$f")"
  printf '  kunci: '
  if python3 -I -c 'import json,sys;print(" ".join(sorted(k for k in json.load(open(sys.argv[1])).keys() if k.replace("_","").isalnum())))' "$f" 2>/dev/null; then ok req provisioner_json; else echo "(tidak terbaca)"; fail req provisioner_json; fi
else
  na "provisioner_json_tidak_ada"; echo "$f tidak ada"
fi
sub "sudoers & SSH (metadata saja)"
for f in /etc/sudoers.d/*; do [ -f "$f" ] && meta "$f"; done
for f in /etc/ssh/ssh_host_*_key; do [ -f "$f" ] && meta "$f"; done
for h in /root /home/*; do [ -f "$h/.ssh/authorized_keys" ] && echo "$h/.ssh/authorized_keys: $(grep -cvE '^\s*(#|$)' "$h/.ssh/authorized_keys") kunci"; done
sub "kunci privat TLS (metadata saja)"
for f in /etc/letsencrypt/live/*/privkey.pem /etc/ssl/private/*; do [ -e "$f" ] && meta "$f"; done
sub "berkas kunci lain (nama saja; terminal/Wine tidak ditelusuri)"
find /opt /etc/transcendence /etc/default -xdev -maxdepth 4 \
  \( -path "$TERM_BASE" -o -name '.wine*' -o -name 'wine-prefix' -o -name '.venv' -o -name 'node_modules' -o -name '.git' \) -prune -o \
  -type f \( -name '*.pem' -o -name '*.key' -o -name '*.jks' -o -name '*.keystore' -o -name '*.p12' -o -name '*.gpg' -o -name '*.age' \) -print 2>/dev/null | head -50
for t in "$TERM_BASE"/*/terminal/rpc.token; do [ -e "$t" ] && echo "$t"; done

# ------------------------------------------------------------------ 7/8
sec 7 "BACKUP YANG ADA (validitas TIDAK diasumsikan)"
B=/var/backups/transcendence
if [ -d "$B" ]; then
  ok req backup_dir_dibaca
  echo "$(meta "$B")  total: $(dus "$B")"
  [ -f "$B/LAST_DAILY" ] && echo "LAST_DAILY = $(pointer_value "$B/LAST_DAILY" '^daily-[0-9]{8}-[0-9]{6}$')"
  [ -f "$B/LAST_PRE_DEPLOY" ] && echo "LAST_PRE_DEPLOY = $(pointer_value "$B/LAST_PRE_DEPLOY" '^(run-[0-9]+-attempt-[0-9]+|[0-9]{8}-[0-9]{6})$')"
  if [ -f "$B/PREVIOUS_COMMIT" ]; then
    pc=$(head -c 64 "$B/PREVIOUS_COMMIT" | head -1)
    printf '%s' "$pc" | grep -Eq '^[0-9a-f]{40}$' && echo "PREVIOUS_COMMIT = ${pc:0:12}" || echo "PREVIOUS_COMMIT = (format tidak dikenal — tidak ditampilkan)"
  fi
  for pat in 'daily-*.sql.gz' 'pre-deploy-*.sql.gz' 'manual-*.sql.gz' '.*.part'; do
    echo "$pat : $(find "$B" -maxdepth 1 -name "$pat" | wc -l) berkas (terlama / terbaru):"
    find "$B" -maxdepth 1 -name "$pat" -printf '%TY-%Tm-%Td %TH:%TM | %s B | %f\n' 2>/dev/null | sort | sed -n '1p;$p'
  done
  if [ "$AUDIT_VERIFY_DUMPS" = 1 ]; then
    sub "3 dump terbaru: gzip -t, header, footer, sha256 (DISETUJUI; membaca seluruh berkas)"
    for f in $(ls -1t "$B"/*.sql.gz 2>/dev/null | head -3); do
      gz=$(gzip -t "$f" 2>/dev/null && echo ok || echo GAGAL)
      hdr=$(gzip -dc "$f" 2>/dev/null | head -c 4096 | grep -c 'PostgreSQL database dump')
      ftr=$(gzip -dc "$f" 2>/dev/null | tail -c 4096 | grep -c 'PostgreSQL database dump complete')
      echo "$(basename "$f") | gzip $gz | header $hdr | footer-complete $ftr | sha256 $(sha256sum "$f" | cut -c1-16)…"
    done
  else
    skip "verifikasi_isi_dump_belum_disetujui"
  fi
  sub "daily-cron.log (metadata saja; isi tidak ditampilkan)"
  if [ -f "$B/daily-cron.log" ]; then meta "$B/daily-cron.log"; else echo "daily-cron.log: tidak ada"; fi
  for f in "$B"/daily-cron.log-*; do [ -f "$f" ] && meta "$f"; done
else
  echo "TEMUAN: $B TIDAK ADA"
  ok req backup_dir_diperiksa
fi
sub "lokasi backup lain (nama/ukuran saja; terminal/Wine tidak ditelusuri)"
find /var/backups /root /home /opt /srv -xdev -maxdepth 4 \
  \( -path "$B" -o -path "$TERM_BASE" -o -name '.wine*' -o -name 'wine-prefix' -o -name '.venv' -o -name '.git' -o -name 'node_modules' \) -prune -o \
  -type f \( -name '*.sql' -o -name '*.sql.gz' -o -name '*.dump' -o -name '*.backup' -o -name '*.tar*' -o -name 'journal.db*' \) \
  -printf '%TY-%Tm-%Td | %s B | %p\n' 2>/dev/null | head -40
sub "logrotate (metadata saja; isi tidak ditampilkan)"
lr=0; for f in /etc/logrotate.d/transcendence*; do [ -f "$f" ] && { meta "$f"; lr=1; }; done
[ "$lr" = 1 ] || echo "logrotate transcendence: tidak ada"

sec 8 "SKRIP BACKUP TERPASANG (metadata)"
for f in backup_daily.sh backup_db.sh rollback.sh make_backup_stamp.sh; do
  p=/opt/transcendence-hlwon-/backend/deploy/$f
  [ -f "$p" ] && meta "$p"
done
have rclone && echo "rclone terpasang (konfigurasi offsite: periksa manual)" || echo "rclone tidak ada"
echo "Catatan: skrip repo hanya menulis ke $B (DISK YANG SAMA). Tidak ada salinan offsite di repo."
ok opt backup_scripts

# ------------------------------------------------------------------ 9
sec 9 "RISIKO RPC CLASSIC (tanpa koneksi ke port)"
if have ss; then
  L=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(1881[0-9]|1882[0-9])$')
  if [ -n "$L" ]; then
    printf '%s\n' "$L"
    if printf '%s\n' "$L" | grep -vqE '^(127\.0\.0\.1|\[::1\]):'; then echo "BAHAYA: ada listener RPC bukan loopback"; fail req rpc_listener_bukan_loopback; else echo "semua listener RPC di loopback"; ok req rpc_listener_loopback; fi
  else
    echo "(tidak ada listener 18810-18829)"; ok req rpc_listener_diperiksa
  fi
else
  fail req ss_tidak_ada
fi
sub "firewall (HANYA aturan yang menyebut port RPC 18810-18829)"
fw=0
if have ufw; then ufw status 2>/dev/null | grep -E '\b188[12][0-9]\b' && fw=1; fi
if have nft; then nft list ruleset 2>/dev/null | grep -E '\b188[12][0-9]\b' | sed -E 's/^[[:space:]]+//' | head -20 && fw=1; fi
if have iptables; then iptables -S 2>/dev/null | grep -E '\b188[12][0-9]\b' | head -20 && fw=1; fi
[ "$fw" = 1 ] || echo "(tidak ada aturan firewall yang menyebut port RPC, atau alat firewall tidak ada)"

# ------------------------------------------------------------------ 10
sec 10 "RINGKASAN UKURAN"
if [ "$AUDIT_SKIP_DU" = 1 ]; then
  skip "du_dimatikan_default_aktifkan_setelah_persetujuan_AUDIT_SKIP_DU=0"
  echo "Ukuran DB tersedia dari bagian 2 (pg_database_size). Ukuran direktori butuh du (persetujuan)."
else
  for p in "${DD:-}" "$TERM_BASE" /var/backups/transcendence /opt/transcendence-hlwon- /opt/jurnal-mt5 /opt/transcendence /home/jurnal /etc/transcendence /etc/letsencrypt /etc/nginx; do
    [ -n "$p" ] && [ -e "$p" ] && printf '%-45s %s\n' "$p" "$(dus "$p")"
  done
  ok opt du
fi
echo
echo "@@END" >&2
}

# ------------------------------------------------------------ ringkasan
summarize() {
  awk '
    { i=index($0,"@@"); if (i>1 && substr($0,i) ~ /^@@(CHK|SEC|NA|PGFAIL|END)/) { print substr($0,1,i-1); $0=substr($0,i) } }
    /^@@SEC / { s=$2; $1=$2=""; t[s]=substr($0,3); order[++n]=s; next }
    /^@@NA /  { nas[$2]=1; nac[$2]++; k=$2; $1=$2=""; naitems[++nn]=k ":" substr($0,3); next }
    /^@@PGFAIL / { pgf++; next }
    /^@@CHK / { sec=$2; req=$3; st=$4
                if (st=="FAIL") { if (req=="req") { rf[sec]++; rft++ } else of[sec]++ }
                else if (st=="PASS") ps[sec]++
                else if (st=="SKIP") sk[sec]++
                else if (st=="NA") { nac[sec]++; naitems[++nn]=sec ":" $5 }
                if (st=="FAIL") failed[++nf]=sec ":" req ":" $5
                next }
    /^@@END/ { ended=1; next }
    { print }
    END {
      print ""; print "===== STATUS PER BAGIAN ====="
      for (i=1;i<=n;i++) { s=order[i]
        # urutan: FAILED > NOT AVAILABLE (semua NA) > PARTIAL > PASS.
        # PASS hanya bila tidak ada FAIL/NA/SKIP sama sekali.
        if (rf[s]>0) st="FAILED"
        else if (nac[s]>0 && ps[s]+of[s]+sk[s]==0) st="NOT AVAILABLE"
        else if (nac[s]>0 || of[s]>0 || sk[s]>0) {
          st="PARTIAL ("; sep=""
          if (nac[s]>0) { st=st sep "ada yang TIDAK TERSEDIA"; sep="; " }
          if (of[s]>0)  { st=st sep "pemeriksaan opsional gagal"; sep="; " }
          if (sk[s]>0)  { st=st sep "ada yang DILEWATI, butuh persetujuan" }
          st=st ")" }
        else st="PASS"
        printf "%-3s %-60s %s\n", s".", t[s], st }
      print ""
      print "Query PostgreSQL gagal: " (pgf+0)
      print "Pemeriksaan WAJIB gagal: " (rft+0)
      for (i=1;i<=nf;i++) print "  - gagal: " failed[i]
      print "Tidak tersedia (NA): " (nn+0)
      for (i=1;i<=nn;i++) print "  - NA: " naitems[i]
      print ""
      tech = ended ? "SELESAI" : "TIDAK SELESAI (skrip berhenti sebelum akhir)"
      inv  = (ended && rft==0) ? "LENGKAP" : "TIDAK LENGKAP"
      print "TEKNIS    : " tech
      print "INVENTARIS: " inv
      if (ended && rft==0) print "HASIL     : AUDIT COMPLETE (batas: item DILEWATI butuh persetujuan terpisah; item TIDAK TERSEDIA tercantum di atas)"
      else                 print "HASIL     : AUDIT INCOMPLETE — jangan dipakai sebagai dasar backup sebelum kegagalan di atas ditangani"
    }'
}

main 2>&1 | redact | summarize
