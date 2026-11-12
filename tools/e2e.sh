#!/usr/bin/env bash
# End-to-end checks for a release-rollup build, run against the release zip.
#
#   bash tools/e2e.sh RELEASE.zip [--cross OLD_RELEASE.zip]
#   bash tools/e2e.sh --self-check RELEASE.zip
#
# RELEASE.zip is the release asset (release-rollup-<version>.zip, 0.2.0 or newer). It is unpacked into a temp
# folder and every check runs on scratch copies there. --cross also takes the zip of a 0.1.x release and checks
# that both versions leave each other's rollups readable and intact. --self-check changes one byte of the VERSION file in a copy of
# the zip and passes only if the checks fail on that copy.
# If a SHA256SUMS file sits next to the zip (or one folder above it, as in a release folder), the zip is verified
# against it first. Set PY to pick the interpreter (default python3, 3.14 or newer); on Windows from Git Bash use
# PY="py -3.14". The gio trash checks write under $E2E_HOME_TMP (default ~/.cache/release-rollup-e2e) because gio
# refuses to trash from /tmp.
# Prints one PASS, FAIL or INFO line per check and a total; exit 0 only when nothing failed.
set -u
PY=${PY:-python3}
usage(){ echo "usage: bash tools/e2e.sh RELEASE.zip [--cross OLD_RELEASE.zip]"; echo "       bash tools/e2e.sh --self-check RELEASE.zip"; exit 2; }
wp(){ if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }
MODE=single
case "${1:-}" in
  --self-check) [ $# = 2 ] || usage; MODE=self; ZIP=$2;;
  ""|-h|--help|--*) usage;;
  *) ZIP=$1
     if [ "${2:-}" = "--cross" ]; then [ $# = 3 ] || usage; MODE=cross; OZIP=$3; else [ $# = 1 ] || usage; fi;;
esac
[ -f "$ZIP" ] || { echo "FAIL  $ZIP is not a file"; exit 2; }
[ $MODE != cross ] || [ -f "$OZIP" ] || { echo "FAIL  $OZIP is not a file"; exit 2; }
ZIP=$(cd "$(dirname "$ZIP")" && pwd)/$(basename "$ZIP")
[ $MODE != cross ] || OZIP=$(cd "$(dirname "$OZIP")" && pwd)/$(basename "$OZIP")
W=$(mktemp -d); mkdir -p "$W/tools"
trap 'rm -rf "$W"' EXIT

if [ $MODE = self ]; then
  # one byte changed inside the VERSION file, rewritten with a valid CRC so the zip itself still reads cleanly
  $PY -I -c "import sys, zipfile
src = zipfile.ZipFile(sys.argv[1])
with zipfile.ZipFile(sys.argv[2], 'w') as out:
    for i in src.infolist():
        d = src.read(i)
        if i.filename.endswith('/VERSION') and i.filename.count('/') == 1:
            d = b'x' + d[1:]
        out.writestr(i, d, compress_type=i.compress_type)" "$(wp "$ZIP")" "$(wp "$W/$(basename "$ZIP")")" || { echo "FAIL  could not make the tampered copy"; exit 1; }
  out=$(bash "$0" "$W/$(basename "$ZIP")" 2>&1); rc=$?
  info_tail=$(echo "$out" | grep -E '^(FAIL|total:)' | head -3 | tr '\n' ' ')
  if [ $rc != 0 ] && echo "$out" | grep -q '^FAIL'; then
    echo "PASS  self-check: a zip with one changed byte fails the checks ($info_tail)"; echo "total: 1 pass, 0 fail"; exit 0
  fi
  echo "FAIL  self-check: the zip with one changed byte passed (exit $rc)"; echo "total: 0 pass, 1 fail"; exit 1
fi

IS_WIN=0; case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1;; esac
pass=0; fail=0
ok(){ echo "PASS  $1"; pass=$((pass+1)); }
no(){ echo "FAIL  $1"; fail=$((fail+1)); }
chk(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1"; fi; }
info(){ echo "INFO  $1"; }
# Thousands separators for byte counts: 1234567 becomes 1,234,567.
num(){ echo "$1" | sed -e ':a' -e 's/\([0-9]\)\([0-9]\{3\}\)\($\|,\)/\1,\2\3/' -e 'ta'; }
finish(){ echo "total: $pass pass, $fail fail"; [ $fail = 0 ]; exit $?; }

NAME=$(basename "$ZIP" .zip); VER=${NAME#release-rollup-}
echo "e2e: $NAME ($MODE) on $($PY -V 2>&1), $(uname -s)"
$PY -I -c "import sys; sys.exit(sys.version_info < (3, 14))" || no "Python is 3.14 or newer"
if ! echo "$VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-(alpha|beta|rc)\.[0-9]+)?$'; then no "zip name release-rollup-<version>.zip (got $NAME)"; finish; fi
case "$VER" in 0.0.*|0.1.*) no "this script tests 0.2.0 or newer, got $VER"; finish;; esac
SUMS=
for s in "$(dirname "$ZIP")/SHA256SUMS" "$(dirname "$ZIP")/../SHA256SUMS"; do [ -f "$s" ] && { SUMS=$s; break; }; done
verify_sum(){ # $1 = zip, $2 = SHA256SUMS: the line naming the zip must match its hash
  local want have; want=$(awk -v n="$(basename "$1")" '{f=$2; sub(/^\*/,"",f); sub(/.*\//,"",f); if (f==n) {print $1; exit}}' "$2")
  have=$(sha256sum "$1" | awk '{print $1}'); [ -n "$want" ] && [ "$want" = "$have" ]; }
if [ -n "$SUMS" ]; then chk "SHA256SUMS matches $NAME.zip" "verify_sum '$ZIP' '$SUMS'"; else info "no SHA256SUMS next to the zip, hash check not run"; fi
unzto(){ rm -rf "$2"; mkdir -p "$2"; $PY -I -m zipfile -e "$(wp "$1")" "$(wp "$2")"; }
if unzto "$ZIP" "$W/src" && [ -d "$W/src/$NAME" ]; then ok "$NAME.zip unpacks to $NAME/"; else no "$NAME.zip unpacks to $NAME/"; finish; fi
T="$W/src/$NAME"
if [ $MODE = cross ]; then
  ONAME=$(basename "$OZIP" .zip); OVER=${ONAME#release-rollup-}
  case "$OVER" in 0.1.*) ;; *) no "the old side must be a 0.1.x release, got $OVER"; finish;; esac
  if [ -n "$(dirname "$OZIP")" ]; then for s in "$(dirname "$OZIP")/SHA256SUMS" "$(dirname "$OZIP")/../SHA256SUMS"; do
    [ -f "$s" ] && { chk "SHA256SUMS matches $ONAME.zip" "verify_sum '$OZIP' '$s'"; break; }; done; fi
  if unzto "$OZIP" "$W/osrc" && [ -d "$W/osrc/$ONAME" ]; then ok "$ONAME.zip unpacks to $ONAME/"; else no "$ONAME.zip unpacks to $ONAME/"; finish; fi
  OT="$W/osrc/$ONAME"
fi
BRX(){ local t=$1; shift; $PY -I -c "import sys,runpy;sys.path.insert(0,'$(wp "$t")/src');sys.argv=['release-rollup']+sys.argv[1:];runpy.run_module('release_rollup',run_name='__main__')" "$@"; }
BR(){ BRX "$T" "$@"; }
BO(){ BRX "$OT" "$@"; }

# ---- shared helpers and test data ----
cat > "$W/tools/g.py" <<'PYEOF'
import sys, zipfile
cmd = sys.argv[1]
if cmd == 'bundle':    # bundle OUT SHAREDFILE UNIQUETEXT : a zip with one shared member and one unique member
    out, sh, u = sys.argv[2:5]
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr(zipfile.ZipInfo('big.bin', (2026, 1, 1, 0, 0, 0)), open(sh, 'rb').read(), zipfile.ZIP_DEFLATED)
        z.writestr(zipfile.ZipInfo('u.txt', (2026, 1, 1, 0, 0, 0)), u, zipfile.ZIP_DEFLATED)
elif cmd == 'flip':  # flip SRC OUT : same zip, one byte changed inside the biggest dedupe blob (CRC stays valid)
    src, out = sys.argv[2:4]
    zin = zipfile.ZipFile(src)
    names = zin.namelist()
    tgt = max((n for n in names if n.startswith('.rollup/blobs/')), key=lambda n: zin.getinfo(n).file_size)
    with zipfile.ZipFile(out, 'w') as z:
        for n in names:
            d = zin.read(n)
            if n == tgt:
                d = bytes([d[0] ^ 1]) + d[1:]
            z.writestr(zin.getinfo(n), d, compress_type=zin.getinfo(n).compress_type)
elif cmd == 'names':  # names ZIP : print member names
    print('\n'.join(zipfile.ZipFile(sys.argv[2]).namelist()))
PYEOF
G(){ $PY -I "$(wp "$W/tools/g.py")" "$@"; }
head -c 150000 /dev/urandom > "$W/shared.bin"
same(){ diff -rq "$1" "$2" >/dev/null 2>&1; }
fsz(){ wc -c < "$1" | tr -d ' '; }
ex(){ local o; o=$("$@" >/dev/null 2>&1; echo $?); echo "$o"; }
# a release / pre-release build: shared big file, shared-member zip, unique file; tag makes the unique parts differ
mkrel(){ local d=$1 tag=$2 kind=${3:-rel}; mkdir -p "$d/assets"; cp "$W/shared.bin" "$d/assets/app.bin"
  G bundle "$(wp "$d/assets/bundle.zip")" "$(wp "$W/shared.bin")" "unique $tag"; head -c 2000 /dev/urandom > "$d/assets/u.bin"
  if [ "$kind" = rel ]; then echo "notes $tag" > "$d/RELEASE_NOTES.md"; (cd "$d" && sha256sum assets/* RELEASE_NOTES.md > SHA256SUMS)
  else echo "{\"tag\":\"$tag\"}" > "$d/PRERELEASE.json"; (cd "$d" && sha256sum assets/* PRERELEASE.json > SHA256SUMS); fi; }
# small unrelated builds: nothing to share
mkuniq(){ local d=$1; mkdir -p "$d/assets"; head -c 3000 /dev/urandom > "$d/assets/app.bin"; echo n > "$d/RELEASE_NOTES.md"; (cd "$d" && sha256sum assets/app.bin > SHA256SUMS); }
isdedupe(){ G names "$(wp "$1")" | grep -q '^\.rollup/MANIFEST.json$'; }
isplain(){ ! isdedupe "$1"; }
# every file below $1 (a restored/unzipped tree) equals the same path below $2
subset(){ (cd "$1" && find . -type f | sort) | while read -r f; do cmp -s "$1/$f" "$2/$f" || { echo "differs: $f"; return 1; }; done; }
mkrelsets(){ # $1 = dir: R (releases) and P (prereleases)
  cd "$1"; for v in 0.1.0 0.1.1 0.1.2 0.2.0; do mkrel R/v$v $v; done
  for n in 1 2 3; do mkrel P/alpha/v0.0.2-alpha.$n a$n pre; done; mkrel P/beta/v0.0.2-beta.1 b1 pre; mkrel P/alpha/v0.0.1-alpha.1 old1 pre
  mkdir -p P/rc/v0.0.2-rc.1; echo wip > P/rc/v0.0.2-rc.1/a.txt; cd "$W"; }

cd "$W"; mkdir tr

if [ $MODE = single ]; then
# ---- version and unit tests ----
PEP=$(echo "$VER" | sed -E 's/-alpha\./a/; s/-beta\./b/; s/-rc\./rc/')
chk "version strings say $VER ($PEP in pyproject)" "grep -qx '$VER' '$T/VERSION' && grep -qx '$VER' '$T/src/release_rollup/VERSION' && grep -qx 'version = \"$PEP\"' '$T/pyproject.toml'"
chk "CHANGELOG has an entry for $VER" "grep -q '^## \[$VER\]' '$T/CHANGELOG.md'"
out=$(cd "$T" && PYTHONPATH=src $PY -I -m unittest discover -s tests 2>&1)
echo "$out" | grep -qx 'OK' && ok "unit tests: $(echo "$out" | grep -o '^Ran [0-9]* tests') $(echo "$out" | grep -o 'skipped=[0-9]*')" || no "unit tests: $(echo "$out" | grep -E '^(Ran|OK|FAILED)' | tr '\n' ' ')"
chk "check.sh (PY passed through)" "cd '$T' && PY='$PY' bash tools/check.sh"
chk "--version prints $VER" "BR --version | grep -q 'release-rollup $VER'"
chk "help lists run pre upgrade restore archive" "BR -h | grep -q upgrade && BR -h | grep -q restore && BR -h | grep -q archive"

mkrelsets "$W"; cp -a R Ro; cp -a P Po

# ---- run: deduplicated rollup ----
cp -a R Rp
BR run --root Rp --trash-dir tr --no-dedupe >/dev/null; rc=$?
chk "run --no-dedupe: plain 0.1.zip, exit 0" "[ $rc = 0 ] && isplain Rp/0.1.zip"
BR run --root R --dry-run --trash-dir tr >/dev/null
chk "run --dry-run changes nothing" "same R Ro"
out=$(BR run --root R --trash-dir tr 2>&1); rc=$?
chk "run: exit 0, 0.1.zip is a dedupe rollup, only 0.1.zip + v0.2.0 left" "[ $rc = 0 ] && isdedupe R/0.1.zip && [ \"\$(ls R | tr '\n' ' ')\" = '0.1.zip v0.2.0 ' ]"
chk "run: ends with a space: line" "echo \"\$out\" | grep -q '^space: before [0-9,]* bytes, after [0-9,]* bytes, saved'"
PS=$(fsz Rp/0.1.zip); DS=$(fsz R/0.1.zip); info "run dedupe size: plain $(num $PS) bytes, deduped $(num $DS) bytes"
chk "run: dedupe smaller ($DS < 60% of $PS)" "[ $DS -lt $((PS*6/10)) ]"
chk "run: restore --check passes" "BR restore R/0.1.zip --check"
chk "run: restore --check passes on the plain zip" "BR restore Rp/0.1.zip --check"
rm -rf xr; BR restore R/0.1.zip --out xr >/dev/null; rc=$?
chk "run: restore --out byte-identical (v0.1.0, v0.1.1, v0.1.2)" "[ $rc = 0 ] && same Ro/v0.1.0 xr/v0.1.0 && same Ro/v0.1.1 xr/v0.1.1 && same Ro/v0.1.2 xr/v0.1.2"
rm -rf xp; BR restore Rp/0.1.zip --out xp >/dev/null
chk "run: restore --out of the plain zip byte-identical" "same Ro/v0.1.0 xp/v0.1.0 && same Ro/v0.1.2 xp/v0.1.2"
unzto Rp/0.1.zip xu
chk "run --no-dedupe: plain unzip byte-identical" "same Ro/v0.1.1 xu/v0.1.1"
chk "run: newest v0.2.0 untouched" "same Ro/v0.2.0 R/v0.2.0"
chk "run: rerun is a no-op" "BR run --root R --trash-dir tr | grep -qx 'nothing to do'"
chk "restore --out refuses to overwrite (exit 1, file kept)" "echo keep > xr/v0.1.0/RELEASE_NOTES.md; [ \$(ex BR restore R/0.1.zip --out xr) = 1 ] && grep -qx keep xr/v0.1.0/RELEASE_NOTES.md"
chk "restore --max-size too small is refused, nothing written" "[ \$(ex BR restore R/0.1.zip --out xm --max-size 1000) = 1 ] && [ ! -e xm/v0.1.0 ]"
G flip "$(wp R/0.1.zip)" "$(wp bad.zip)"
chk "restore --check FAILS on a corrupted dedupe zip (exit 1)" "[ \$(ex BR restore bad.zip --check) = 1 ]"
chk "restore --out of a corrupted dedupe zip: exit 1, nothing written" "[ \$(ex BR restore bad.zip --out xbad) = 1 ] && [ -z \"\$(ls xbad 2>/dev/null)\" ]"
chk "restore of a missing zip: exit 1" "[ \$(ex BR restore nope.zip --check) = 1 ]"

# unshared small builds stay a plain zip, byte for byte as --no-dedupe
for v in 0.1.0 0.1.1 0.2.0; do mkuniq U/v$v; done; cp -a U Uq; cp -a U Uo
BR run --root U --trash-dir tr >/dev/null; BR run --root Uq --trash-dir tr --no-dedupe >/dev/null
chk "run: nothing to share stays plain, same bytes as --no-dedupe" "isplain U/0.1.zip && cmp U/0.1.zip Uq/0.1.zip"

# ---- pre ----
BR pre --root P --current 0.0.2-beta.1 --dry-run --trash-dir tr >/dev/null
chk "pre: dry-run changes nothing" "same P Po"
BR pre --root P --current 0.0.2-beta.1 --trash-dir tr >/dev/null; rc=$?
rm -rf xa xb; BR restore P/alpha/v0.0.2-alpha.x.zip --out xa >/dev/null; BR restore P/alpha/v0.0.1-alpha.x.zip --out xb >/dev/null
chk "pre: exit 0, stage zip is a dedupe rollup" "[ $rc = 0 ] && isdedupe P/alpha/v0.0.2-alpha.x.zip"
chk "pre: restore --check passes" "BR restore P/alpha/v0.0.2-alpha.x.zip --check"
chk "pre: restored builds byte-identical" "same Po/alpha/v0.0.2-alpha.1 xa/v0.0.2-alpha.1 && same Po/alpha/v0.0.2-alpha.2 xa/v0.0.2-alpha.2 && same Po/alpha/v0.0.1-alpha.1 xb/v0.0.1-alpha.1"
chk "pre: newest alpha, beta and unfinished rc untouched" "same Po/alpha/v0.0.2-alpha.3 P/alpha/v0.0.2-alpha.3 && same Po/beta P/beta && same Po/rc P/rc"
chk "pre: rerun is a no-op" "BR pre --root P --current 0.0.2-beta.1 --trash-dir tr | grep -qx 'nothing to do'"
cp -a Po Pp; BR pre --root Pp --current 0.0.2-beta.1 --trash-dir tr --no-dedupe >/dev/null
chk "pre --no-dedupe: plain, and the dedupe zip is smaller ($(fsz P/alpha/v0.0.2-alpha.x.zip) < $(fsz Pp/alpha/v0.0.2-alpha.x.zip))" "isplain Pp/alpha/v0.0.2-alpha.x.zip && [ $(fsz P/alpha/v0.0.2-alpha.x.zip) -lt $(fsz Pp/alpha/v0.0.2-alpha.x.zip) ]"
# a new build arrives later: the stage zip is rebuilt with it
mkrel P/alpha/v0.0.2-alpha.4 a4 pre; cp -a P/alpha/v0.0.2-alpha.4 Po/alpha/; BR pre --root P --current 0.0.2-beta.1 --trash-dir tr >/dev/null; rc=$?
rm -rf xa; BR restore P/alpha/v0.0.2-alpha.x.zip --out xa >/dev/null
chk "pre: new build rebuilds the zip, all builds byte-identical after restore" "[ $rc = 0 ] && same Po/alpha/v0.0.2-alpha.3 xa/v0.0.2-alpha.3 && same Po/alpha/v0.0.2-alpha.1 xa/v0.0.2-alpha.1 && same Po/alpha/v0.0.2-alpha.4 P/alpha/v0.0.2-alpha.4"

# ---- upgrade: plain rollups (what 0.1.x wrote, or --no-dedupe) into the new format ----
cp -a Rp Ru; cp -a Rp Ruo; cp -a Pp Pu; cp -a Pp Puo
BR upgrade --root Ru --dry-run --trash-dir tr >/dev/null; rc=$?
chk "upgrade --dry-run: exit 0, changes nothing" "[ $rc = 0 ] && same Ru Ruo"
out=$(BR upgrade --root Ru --trash-dir tr 2>&1); rc=$?
rm -rf xr; BR restore Ru/0.1.zip --out xr >/dev/null
chk "upgrade: exit 0, plain zip becomes dedupe, byte-identical after restore" "[ $rc = 0 ] && isdedupe Ru/0.1.zip && same Ro/v0.1.0 xr/v0.1.0 && same Ro/v0.1.1 xr/v0.1.1 && same Ro/v0.1.2 xr/v0.1.2"
chk "upgrade: unpacked newest untouched, ends with a space: line" "same Ro/v0.2.0 Ru/v0.2.0 && echo \"\$out\" | grep -q '^space:'"
chk "upgrade: rerun prints nothing to do" "BR upgrade --root Ru --trash-dir tr | grep -qx 'nothing to do'"
BR upgrade --root Pu --trash-dir tr >/dev/null; rc=$?; rm -rf xa; BR restore Pu/alpha/v0.0.2-alpha.x.zip --out xa >/dev/null
chk "upgrade on a prereleases folder: dedupe, byte-identical, newest builds untouched" "[ $rc = 0 ] && isdedupe Pu/alpha/v0.0.2-alpha.x.zip && same Po/alpha/v0.0.2-alpha.1 xa/v0.0.2-alpha.1 && same Pp/alpha/v0.0.2-alpha.3 Pu/alpha/v0.0.2-alpha.3 && same Pp/beta Pu/beta"
mkdir -p Ux; cp -a Uq/0.1.zip Ux/; cp -a Ux Uxo; BR upgrade --root Ux --trash-dir tr >/dev/null; rc=$?
chk "upgrade: a zip that shares too little stays plain, exit 0" "[ $rc = 0 ] && same Ux Uxo"
mkdir -p Uc; cp -a Rp/0.1.zip Uc/; head -c 800 /dev/urandom > Uc/bad.zip; cp -a Rp/0.1.zip Uc/0.2.zip; echo "not a zip" > Uc/0.3.zip; cp -a Uc Uco
BR upgrade --root Uc --trash-dir tr >/dev/null 2>&1; rc=$?
chk "upgrade: unreadable zips refused (exit 1, untouched), good zips still upgraded" "[ $rc = 1 ] && cmp Uc/bad.zip Uco/bad.zip && cmp Uc/0.3.zip Uco/0.3.zip && isdedupe Uc/0.1.zip && isdedupe Uc/0.2.zip"
# leftover from a run that died after the rename: .<name>.upgrade-old
mkdir -p Ul; cp -a Ru/0.1.zip Ul/; cp -a Rp/0.1.zip Ul/.0.1.zip.upgrade-old
BR upgrade --root Ul --trash-dir tr >/dev/null 2>&1; rc=$?
chk "upgrade: leftover .upgrade-old of a finished upgrade is trashed" "[ $rc = 0 ] && [ ! -e Ul/.0.1.zip.upgrade-old ] && isdedupe Ul/0.1.zip"

# ---- options ----
for v in 0.1.0 0.1.1; do mkuniq A/v$v; rm A/v$v/RELEASE_NOTES.md; done
chk "--marker: default leaves note-less releases alone" "BR run --root A --trash-dir tr | grep -qx 'nothing to do'"
BR run --root A --trash-dir tr --marker SHA256SUMS --marker assets/ >/dev/null
chk "--marker: custom markers roll them" "[ -f A/0.1.zip ] || [ -f A/v0.1.0.zip ]"
for d in K1 K2 K3; do for v in v0.1.0 v0.2.0 v0.2.0-rc.1 v0.3.0-beta.1; do mkuniq $d/$v; done; done
BR run --root K1 --trash-dir tr >/dev/null; BR run --root K2 --trash-dir tr --keep-rc >/dev/null; BR run --root K3 --trash-dir tr --keep-pre >/dev/null
chk "published release trashes its rc, unpublished beta stays; --keep-rc/--keep-pre keep it" "[ ! -e K1/v0.2.0-rc.1 ] && [ -d K1/v0.3.0-beta.1 ] && [ -d K2/v0.2.0-rc.1 ] && [ -d K3/v0.2.0-rc.1 ]"
mkrel Z/v0.1.0 z1; mkrel Z/v0.2.0 z2; cp -a Z Zo; BR run --root Z --trash-dir tr --zip-newest >/dev/null; rm -rf xz; BR restore Z/v0.2.0.zip --out xz >/dev/null
chk "--zip-newest: nothing unpacked, newest byte-identical after restore" "[ \"\$(ls Z | tr '\n' ' ')\" = '0.1.zip v0.2.0.zip ' ] && same Zo/v0.2.0 xz/v0.2.0"

# ---- errors: nothing changes ----
mkrel D/v0.1.3 d1; mkrel D/v0.2.0 d2; head -c 500 /dev/urandom > D/0.1.zip; cp -a D Do
BR run --root D --trash-dir tr >/dev/null 2>&1; rc=$?
chk "corrupt existing zip: run exit 1, nothing changed" "[ $rc = 1 ] && same D Do"
mkdir -p E; cp -a bad.zip E/0.1.zip; mkrel E/v0.1.9 e1; mkrel E/v0.2.0 e2; cp -a E Eo
BR run --root E --trash-dir tr >/dev/null 2>&1; rc=$?
chk "corrupted dedupe zip (flipped byte) in the folder: run exit 1, nothing changed" "[ $rc = 1 ] && same E Eo"
BR pre --root nope --trash-dir tr >/dev/null 2>&1; r1=$?; BR run --root nope --trash-dir tr >/dev/null 2>&1; r2=$?; BR upgrade --root nope >/dev/null 2>&1; r3=$?
chk "bad --root: exit 2 for run, pre and upgrade" "[ $r1 = 2 ] && [ $r2 = 2 ] && [ $r3 = 2 ]"
chk "no gio and no --trash-dir is only checked when something would change (dry-run needs neither)" "BR run --root Ro --dry-run | grep -q 'would write 0.1.zip'"

# ---- Windows names ----
{
  if [ $IS_WIN = 0 ]; then
    mkrel WN/v0.1.0 w1; mkrel WN/v0.1.1 w2; mkrel WN/v0.2.0 w3; echo x > WN/v0.1.0/aux.txt; echo y > WN/v0.1.1/CON; cp -a WN WNo
    out=$(BR run --root WN --trash-dir tr 2>&1); rc=$?; rm -rf xw; BR restore WN/0.1.zip --out xw >/dev/null
    chk "reserved names on Linux: one warning: line, rollup written, restore byte-identical" "[ $rc = 0 ] && echo \"\$out\" | grep -q '^warning:' && same WNo/v0.1.0 xw/v0.1.0 && same WNo/v0.1.1 xw/v0.1.1"
  fi

  # A plain (0.1.x-style) rollup holding a reserved name, built in memory so no such file is ever made on disk.
  $PY -I -c "import zipfile,sys
z=zipfile.ZipFile(sys.argv[1],'w')
for v in ('v0.1.0','v0.1.1'):
    for n in ('RELEASE_NOTES.md','SHA256SUMS','assets/app.bin'): z.writestr(v+'/'+n, v+n)
z.writestr('v0.1.0/CON','x'); z.close()" "$(wp "$W/wr.zip")"
  mkdir -p WU; cp wr.zip WU/0.1.zip; mkrel WU/v0.2.0 wu; cp -a WU WUo
  if [ $IS_WIN = 1 ]; then
    out=$(BR restore wr.zip --out xwr 2>&1); rc=$?
    chk "Windows: restore refuses CON by name, exit 1, nothing written" "[ $rc = 1 ] && echo \"\$out\" | grep -q CON && [ -z \"\$(ls -A xwr 2>/dev/null)\" ]"
    out=$(BR upgrade --root WU --trash-dir tr 2>&1); rc=$?
    chk "Windows: upgrade refuses CON by name, exit 1, nothing changed" "[ $rc = 1 ] && echo \"\$out\" | grep -q CON && same WU WUo"
  else
    rm -rf xwr; BR restore wr.zip --out xwr >/dev/null 2>&1; rc=$?
    chk "Linux: the same CON zip restores (so the Windows refusal is about the name)" "[ $rc = 0 ] && [ -f xwr/v0.1.0/CON ]"
  fi
}

# ---- archive ----
{
  mkdir F; G bundle "$(wp F/bundle-a.zip)" "$(wp "$W/shared.bin")" "ua"; G bundle "$(wp F/bundle-b.zip)" "$(wp "$W/shared.bin")" "ub"
  head -c 5000 /dev/urandom > F/report.log; echo index1 > F/INDEX.md; echo hid > F/.hidden; mkdir F/sub; echo s > F/sub/s.txt; cp -a F Fo
  BR archive --root F --out A.zip --dry-run --trash-dir tr >/dev/null; rc=$?
  chk "archive --dry-run: exit 0, changes nothing" "[ $rc = 0 ] && same F Fo"
  out=$(BR archive --root F --out A.zip --trash-dir tr 2>&1); rc=$?; rm -rf xf; BR restore F/A.zip --out xf >/dev/null
  chk "archive: exit 0, A.zip is a dedupe archive, files byte-identical after restore (incl. INDEX.md)" "[ $rc = 0 ] && isdedupe F/A.zip && cmp Fo/bundle-a.zip xf/bundle-a.zip && cmp Fo/bundle-b.zip xf/bundle-b.zip && cmp Fo/report.log xf/report.log && cmp Fo/INDEX.md xf/INDEX.md"
  chk "archive: sources trashed, INDEX.md, hidden file and folder stay, space: line" "[ ! -e F/bundle-a.zip ] && [ ! -e F/report.log ] && [ -f F/INDEX.md ] && [ -f F/.hidden ] && [ -f F/sub/s.txt ] && echo \"\$out\" | grep -q '^space:'"
  PS=$(( $(fsz Fo/bundle-a.zip) + $(fsz Fo/bundle-b.zip) )); info "archive: two bundles $(num $PS) bytes plain, archive $(num $(fsz F/A.zip)) bytes"
  chk "archive: shared content stored once (archive smaller than the two bundles)" "[ $(fsz F/A.zip) -lt $PS ]"
  chk "archive: restore --check passes" "BR restore F/A.zip --check"
  chk "archive: rerun prints nothing to do" "BR archive --root F --out A.zip --trash-dir tr | grep -qx 'nothing to do'"
  G bundle "$(wp F/bundle-c.zip)" "$(wp "$W/shared.bin")" "uc"; cp -a F/bundle-c.zip Fo/; BR archive --root F --out A.zip --trash-dir tr >/dev/null; rc=$?; rm -rf xf; BR restore F/A.zip --out xf >/dev/null
  chk "archive: a new file is added to the same archive, old files still byte-identical" "[ $rc = 0 ] && [ ! -e F/bundle-c.zip ] && cmp Fo/bundle-c.zip xf/bundle-c.zip && cmp Fo/bundle-a.zip xf/bundle-a.zip && cmp Fo/report.log xf/report.log"
  echo index2 > F/INDEX.md; BR archive --root F --out A.zip --trash-dir tr >/dev/null; rc=$?; rm -rf xf; BR restore F/A.zip --out xf >/dev/null
  chk "archive: a changed INDEX.md is refreshed in the archive and stays in the folder" "[ $rc = 0 ] && grep -qx index2 xf/INDEX.md && grep -qx index2 F/INDEX.md"
  echo different > F/report.log; cp -a F Ff
  BR archive --root F --out A.zip --trash-dir tr >/dev/null 2>&1; rc=$?
  chk "archive: same name with other bytes refused (exit 1), nothing changed" "[ $rc = 1 ] && same F Ff && [ -f F/report.log ]"
  mkdir -p F2; echo data > F2/a.txt; head -c 800 /dev/urandom > F2/X.zip; cp -a F2 F2o
  BR archive --root F2 --out X.zip --trash-dir tr >/dev/null 2>&1; rc=$?
  chk "archive: --out that is not an archive is never replaced (exit 1, unchanged)" "[ $rc = 1 ] && same F2 F2o"
  G flip "$(wp F/A.zip)" "$(wp badA.zip)"; mkdir -p F3; cp badA.zip F3/A.zip; echo n > F3/new.txt; cp -a F3 F3o
  BR archive --root F3 --out A.zip --trash-dir tr >/dev/null 2>&1; rc=$?
  chk "archive: corrupted existing archive: exit 1, nothing changed" "[ $rc = 1 ] && same F3 F3o"
  chk "archive: restore --check FAILS on the corrupted archive" "[ \$(ex BR restore badA.zip --check) = 1 ]"
  chk "archive: bad --root exit 2" "[ \$(ex BR archive --root nope --out A.zip --trash-dir tr) = 2 ]"
}


# ---- no gio and no --trash-dir: refuse before touching anything (every host; gio is hidden where it exists) ----
PYX=$($PY -I -c "import sys;print(sys.executable)"); command -v cygpath >/dev/null && PYX=$(cygpath -u "$PYX")
mkdir -p "$W/nogio"; mkrel NG/v0.1.0 n1; mkrel NG/v0.1.1 n2; mkrel NG/v0.2.0 n3; cp -a NG NGo
PATH="$W/nogio" "$PYX" -I -c "import sys,runpy;sys.path.insert(0,'$(wp "$T")/src');sys.argv=['release-rollup']+sys.argv[1:];runpy.run_module('release_rollup',run_name='__main__')" run --root "$(wp "$W/NG")" >/dev/null 2>&1; rc=$?
chk "no gio and no --trash-dir: exit 1, nothing changed" "[ $rc = 1 ] && same NG NGo && [ ! -e NG/0.1.zip ]"

# ---- gio trash under the home folder, plus recovery from a half-finished run ----
H=${E2E_HOME_TMP:-$HOME/.cache/release-rollup-e2e}/run.$$
if command -v gio >/dev/null; then
  mkdir -p "$H"; for v in 0.1.0 0.1.1 0.2.0; do mkrel "$H/G/v$v" g$v; done; cp -a "$H/G" Go
  BR run --root "$H/G" >/dev/null 2>&1; rc=$?; rm -rf xg; BR restore "$H/G/0.1.zip" --out xg >/dev/null
  chk "gio trash: exit 0, dedupe rollup restores byte-identical, sources gone" "[ $rc = 0 ] && isdedupe '$H/G/0.1.zip' && same Go/v0.1.0 xg/v0.1.0 && same Go/v0.1.1 xg/v0.1.1 && [ ! -e '$H/G/v0.1.0' ]"
  mkdir -p "$H/half"; cp -a Go/. "$H/half/"; cp "$H/G/0.1.zip" "$H/half/"
  BR run --root "$H/half" >/dev/null 2>&1; rc=$?
  chk "half-finished state (zip in place, sources left): rerun finishes, then no-op" "[ $rc = 0 ] && [ ! -e '$H/half/v0.1.0' ] && BR run --root '$H/half' | grep -qx 'nothing to do'"
  {
    mkdir -p "$H/ar"; cp Fo/bundle-a.zip Fo/bundle-b.zip "$H/ar/"; BR archive --root "$H/ar" --out A.zip >/dev/null 2>&1; rc=$?
    chk "gio trash: archive exit 0, sources gone, restore byte-identical" "[ $rc = 0 ] && [ ! -e '$H/ar/bundle-a.zip' ] && rm -rf xar && BR restore '$H/ar/A.zip' --out xar >/dev/null && cmp Fo/bundle-a.zip xar/bundle-a.zip"
  }
  gio trash "$H" 2>/dev/null; rmdir "$(dirname "$H")" 2>/dev/null
else
  info "no gio on this system: the no-gio refusal above is its trash check; the gio trash checks run where gio exists"
fi

else
# ---- cross version: a 0.1.x release (plain zips only, no restore or upgrade) and this version ----
chk "old version reports $OVER" "BO --version | grep -q 'release-rollup $OVER'"
mkrelsets "$W"; cp -a R Ro; cp -a P Po
# (a) old version writes, this version reads
BO run --root R --trash-dir tr >/dev/null; r1=$?; BO pre --root P --current 0.0.2-beta.1 --trash-dir tr >/dev/null; r2=$?
chk "old version: run and pre wrote plain zips (exit 0)" "[ $r1 = 0 ] && [ $r2 = 0 ] && isplain R/0.1.zip && isplain P/alpha/v0.0.2-alpha.x.zip"
cp -a R Ra; cp -a P Pa
chk "(a) this version restore --check passes on the old line zip and stage zips" "BR restore R/0.1.zip --check && BR restore P/alpha/v0.0.2-alpha.x.zip --check && BR restore P/alpha/v0.0.1-alpha.x.zip --check"
rm -rf xr xa; BR restore R/0.1.zip --out xr >/dev/null; BR restore P/alpha/v0.0.2-alpha.x.zip --out xa >/dev/null
chk "(a) this version restore --out of old zips byte-identical" "same Ro/v0.1.0 xr/v0.1.0 && same Ro/v0.1.2 xr/v0.1.2 && same Po/alpha/v0.0.2-alpha.1 xa/v0.0.2-alpha.1 && same Po/alpha/v0.0.2-alpha.2 xa/v0.0.2-alpha.2"
chk "(a) this version run and pre on the old result: nothing to do, zips unchanged" "BR run --root R --trash-dir tr | grep -qx 'nothing to do' && BR pre --root P --current 0.0.2-beta.1 --trash-dir tr | grep -qx 'nothing to do' && same R Ra && same P Pa"
BR upgrade --root R --dry-run --trash-dir tr >/dev/null; rc=$?
chk "(a) this version upgrade --dry-run on old zips changes nothing" "[ $rc = 0 ] && same R Ra"
BR upgrade --root R --trash-dir tr >/dev/null; r1=$?; BR upgrade --root P --trash-dir tr >/dev/null; r2=$?
rm -rf xr xa; BR restore R/0.1.zip --out xr >/dev/null; BR restore P/alpha/v0.0.2-alpha.x.zip --out xa >/dev/null
chk "(a) this version upgrade of old zips: dedupe, restore byte-identical, newest untouched" "[ $r1 = 0 ] && [ $r2 = 0 ] && isdedupe R/0.1.zip && isdedupe P/alpha/v0.0.2-alpha.x.zip && same Ro/v0.1.1 xr/v0.1.1 && same Ro/v0.1.2 xr/v0.1.2 && same Po/alpha/v0.0.2-alpha.1 xa/v0.0.2-alpha.1 && same Ro/v0.2.0 R/v0.2.0 && same Po/alpha/v0.0.2-alpha.3 P/alpha/v0.0.2-alpha.3"
chk "(a) this version upgrade rerun: nothing to do" "BR upgrade --root R --trash-dir tr | grep -qx 'nothing to do' && BR upgrade --root P --trash-dir tr | grep -qx 'nothing to do'"
# a late release / new build is merged into an old plain zip by this version
rm -rf R2 P2; mkdir R2 P2; cp -a Ra/. R2/; cp -a Pa/. P2/; mkrel R2/v0.1.3 late; mkrel P2/alpha/v0.0.2-alpha.4 a4 pre
BR run --root R2 --trash-dir tr >/dev/null; r1=$?; BR pre --root P2 --current 0.0.2-beta.1 --trash-dir tr >/dev/null; r2=$?
rm -rf xr xa; BR restore R2/0.1.zip --out xr >/dev/null; BR restore P2/alpha/v0.0.2-alpha.x.zip --out xa >/dev/null
chk "(a) this version merges a late release and a new build into old zips, all byte-identical" "[ $r1 = 0 ] && [ $r2 = 0 ] && same Ro/v0.1.0 xr/v0.1.0 && same Ro/v0.1.2 xr/v0.1.2 && [ -d xr/v0.1.3 ] && same Po/alpha/v0.0.2-alpha.1 xa/v0.0.2-alpha.1 && [ -d xa/v0.0.2-alpha.3 ]"

# (b) old version version against folders already rolled by this version (deduped zips)
rm -rf R P; mkrelsets "$W"  # fresh pristine data in R, P for the originals
rm -rf Rq Pq; mv R Rq; mv P Pq; cp -a Rq Rq0; cp -a Pq Pq0   # Rq0/Pq0 = originals, Rq/Pq will be rolled by this version
BR run --root Rq --trash-dir tr >/dev/null; BR pre --root Pq --current 0.0.2-beta.1 --trash-dir tr >/dev/null
chk "this version made deduped zips for the (b) folders" "isdedupe Rq/0.1.zip && isdedupe Pq/alpha/v0.0.2-alpha.x.zip"
# every original file still recoverable from folder+zips of $1 (using this version restore for dedupe zips)
recover(){ local root=$1 orig=$2 out="$W/rec.$3"; rm -rf "$out"; mkdir -p "$out"; local z
  find "$root" -name '*.zip' | while read -r z; do BR restore "$z" --out "$out/$(basename "$z" .zip)" >/dev/null 2>&1 || $PY -I -m zipfile -e "$(wp "$z")" "$(wp "$out/$(basename "$z" .zip)")" >/dev/null 2>&1; done
  local d; for d in $(cd "$orig" && find . -mindepth 1 -maxdepth 2 -type d -name 'v*' | sed 's|^\./||'); do
    [ -d "$root/$d" ] && same "$orig/$d" "$root/$d" && continue
    find "$out" -type d -path "*/$(basename "$d")" | while read -r c; do same "$orig/$d" "$c" && echo hit; done | grep -q hit || { echo "lost: $d"; return 1; }
  done; }
verdict(){ # name rootdir snapshot origdir rc
  if same "$2" "$3"; then ok "(b) old version $1: refused or no-op, nothing changed (exit $5)"; info "$1: old version exit $5, folder byte-identical to before"
  elif recover "$2" "$4" "$1" >/dev/null; then ok "(b) old version $1: folder changed but every original file is still recoverable (exit $5)"; info "$1: old version changed the folder, data recoverable"
  else no "(b) old version $1: DATA LOST or corrupted (exit $5)"; fi; }
cp -a Rq Rq1; BO run --root Rq --trash-dir tr >/dev/null 2>"$W/e1"; rc=$?; info "old run on a rollup from this version: exit $rc, $(head -c 160 "$W/e1" | head -1)"; verdict "run rerun on deduped line zip" Rq Rq1 Rq0 $rc
cp -a Rq Rq1; rm -rf Rl; cp -a Rq Rl; mkrel Rl/v0.1.3 late; cp -a Rl Rl1; cp -a Rq0 Rl0; mkrel Rl0/v0.1.3 late
BO run --root Rl --trash-dir tr >/dev/null 2>"$W/e2"; rc=$?; info "old run with a late release over a deduped 0.1.zip: exit $rc, $(head -c 160 "$W/e2" | head -1)"; verdict "run with late release into deduped line zip" Rl Rl1 Rl0 $rc
cp -a Pq Pq1; BO pre --root Pq --current 0.0.2-beta.1 --trash-dir tr >/dev/null 2>"$W/e3"; rc=$?; info "old pre on stage zips from this version: exit $rc, $(head -c 160 "$W/e3" | head -1)"; verdict "pre rerun on deduped stage zips" Pq Pq1 Pq0 $rc
rm -rf Pl; cp -a Pq Pl; mkrel Pl/alpha/v0.0.2-alpha.4 a4 pre; cp -a Pl Pl1; cp -a Pq0 Pl0; mkrel Pl0/alpha/v0.0.2-alpha.4 a4 pre
BO pre --root Pl --current 0.0.2-beta.1 --trash-dir tr >/dev/null 2>"$W/e4"; rc=$?; info "old pre with a new build over a deduped stage zip: exit $rc, $(head -c 160 "$W/e4" | head -1)"; verdict "pre with new build into deduped stage zip" Pl Pl1 Pl0 $rc
# an archive folder from this version and a bare dedupe zip in a releases folder
mkdir -p Fa; G bundle "$(wp Fa/bundle-a.zip)" "$(wp "$W/shared.bin")" ua; G bundle "$(wp Fa/bundle-b.zip)" "$(wp "$W/shared.bin")" ub; mkdir -p xfa0; cp Fa/bundle-a.zip Fa/bundle-b.zip xfa0/; BR archive --root Fa --out A.zip --trash-dir tr >/dev/null
mkrel Fa/v0.1.0 x1; mkrel Fa/v0.2.0 x2; cp -a Fa Fa1
BO run --root Fa --trash-dir tr >/dev/null 2>"$W/e5"; rc=$?; info "old run in a folder holding an archive from this version: exit $rc, $(head -c 160 "$W/e5" | head -1)"
if same Fa Fa1; then ok "(b) old version run next to an archive from this version: refused or no-op, nothing changed (exit $rc)"
elif cmp -s Fa/A.zip Fa1/A.zip && BR restore Fa/A.zip --check >/dev/null && rm -rf xfv && BR restore Fa/0.1.zip --out xfv >/dev/null && same Fa1/v0.1.0 xfv/v0.1.0; then
  ok "(b) old version run next to an archive from this version: archive zip left byte-identical and valid, only the old release v0.1.0 rolled (byte-identical), exit $rc"
else no "(b) old version run next to an archive from this version damaged the archive or the release data (exit $rc)"; fi
mkdir -p Fb; cp Fa/A.zip Fb/; cp -a Fb Fb1; BO run --root Fb --trash-dir tr >/dev/null 2>&1; rc=$?
chk "(b) old version run in a folder holding only an archive from this version: A.zip byte-identical (exit $rc)" "cmp Fb/A.zip Fb1/A.zip && same Fb Fb1"
chk "(b) the archive from this version still restores byte-identical to the original files after old ran" "rm -rf xfa; BR restore Fa/A.zip --out xfa >/dev/null && cmp xfa/bundle-a.zip \"$W/xfa0/bundle-a.zip\" && cmp xfa/bundle-b.zip \"$W/xfa0/bundle-b.zip\""
chk "(b) rollups from this version still restore --check after old touched the folders" "BR restore Rq/0.1.zip --check && BR restore Pq/alpha/v0.0.2-alpha.x.zip --check"
fi

finish
