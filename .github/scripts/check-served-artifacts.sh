#!/usr/bin/env bash
# Compare what onprem.caura.ai actually serves against this repository.
#
# WHY THIS EXISTS. Every other gate here measures the repository TREE. The
# ratchet counts lines in tracked files; the sentinel checks strings in tracked
# files; the parity check hashes a vendored copy against its canonical. All
# three can be green, and the gated legacy-name count can reach zero, while
# customers curl a copy of install.sh from last month. Nothing goes red, because
# nothing was looking at the channel.
#
# For a served file the check is an HTTP GET and nothing substitutes for one.
#
# WHAT IT NEEDS: nothing. Both sides are public — a public bucket in front of a
# public repo — so unlike the publisher, which needs a GCS write credential and
# therefore lives in a private repo, this can run here with no secret at all.
#
# HOW IT DECIDES. It does not diff text. It hashes the served bytes and walks
# this repo's history for the commit whose version of that file matches
# EXACTLY, then counts how many later commits touched the same path. So the
# output is "the channel is serving the state of commit X, which is N commits
# behind", which is actionable, rather than "these files differ", which is not.
#
# A served copy that matches no commit's CURRENT path is split into two
# further states, because "absent from origin/main" collapses two different
# claims into one. `_match_commit` walks this path's full history --
# additions, edits, and the deletion itself are all commits that "touch" the
# path -- so a served copy can still match a commit even though the path is
# gone today. That match is REMOVED: a real, formerly-tracked file this repo
# once held, still being served after this repo dropped it. A served copy
# that matches NOTHING in that walk is UNACCOUNTED, which still carries one
# more distinction worth naming even though both halves fail the build the
# same way: a path with no commits touching it, ever, never came from this
# repository at all; a path with commits, none of whose blobs are these
# bytes, WAS tracked, but these particular served bytes are not a copy of
# anything this repo ever produced for it (tampering, corruption, or a
# coincidental name collision) -- not "this repo never had this path". The
# report says which. UNACCOUNTED is the more serious of the two states
# either way: it means the channel is serving something that never came from
# this repository. That was literally true before 2026-08-25, when the
# object being served came from the private fork half. The distinction
# matters operationally too: a bundle built with macOS's tar/libarchive
# defaults ships one `._name` AppleDouble sidecar per entry that carries
# Finder metadata (COPYFILE_DISABLE=1 or --no-mac-metadata suppresses it) --
# every one of those is UNACCOUNTED (this repo never tracked a `._`-prefixed
# path), and on a bundle built that way they outnumber and bury whatever
# else is actually wrong. REMOVED existing as its own bucket is what keeps a
# single genuinely informative entry (a stray vendored file the source
# checkout hadn't cleaned up) from reading as one more line in that noise.
#
# SCHEDULED, NOT A PR GATE, deliberately. Between two publishes the repository
# is SUPPOSED to be ahead of the channel — that is what an unpublished commit
# is. Failing a PR for it would make every PR red until the next release and
# teach everyone to ignore the check. On a schedule, red means "the channel has
# been behind for a day", which is the thing worth knowing.

set -euo pipefail

BASE="${SERVED_BASE:-https://onprem.caura.ai}"
REF="${COMPARE_REF:-origin/main}"

# The plain-file root objects. bundle.tar.gz is the third and is handled
# separately below: it is an archive, so it is unpacked and each member is
# compared to its own path in the repo.
ROOT_OBJECTS=(install.sh upgrade.sh)

# Loud and attributable rather than mysterious. Without this, an unresolvable
# ref makes every artefact report "does not exist at <ref>", which reads as a
# channel problem when it is a checkout problem.
if ! git rev-parse --verify "$REF" >/dev/null 2>&1; then
  echo "::error::COMPARE_REF '${REF}' does not resolve. This needs full history and the remote ref present — actions/checkout with fetch-depth: 0."
  exit 2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stale=0
unmatched=0
removed=0
unreachable=0
refused=0
current=0
# A local `git rev-list` failure while walking a path's history -- distinct
# from every other counter, which is a verdict ABOUT the served bytes. This
# one means the check couldn't be completed for that path at all, so it must
# never be folded into unmatched/removed's messages, both of which assert
# something specific about what the bytes are or aren't.
errors=0
report=""

# Hash a blob out of history without checking it out. Prints nothing and
# returns 1 when the path did not exist at that commit.
_blob_sha() {
  local commit="$1" path="$2" blob
  blob=$(git rev-parse "${commit}:${path}" 2>/dev/null) || return 1
  git cat-file blob "$blob" | shasum -a 256 | cut -d' ' -f1
}

# The heart of it: which commit's version of $path do these bytes match?
# Walks newest-first and stops at the first match, so the answer is the most
# recent commit that could have produced them. Takes the candidate commit
# list as trailing args when the caller already has one (see _compare's
# REMOVED/UNACCOUNTED branch, which also needs to know whether the path has
# ANY history and would otherwise walk rev-list twice for the same path);
# derives its own otherwise, for the call site in _compare that doesn't need
# that list for anything else.
#
# Three-way return, not two: 0 with the commit on stdout = matched, 1 = no
# match (a real verdict -- every history candidate's blob was checked and
# none is these bytes), 2 = couldn't tell (git rev-list itself failed while
# deriving the fallback list). Collapsing 1 and 2 is exactly the mistake
# _compare's own REMOVED/UNACCOUNTED branch was written to avoid one level
# up -- a caller here must be able to make the same distinction, so it
# cannot be lost inside this function. Captured via a plain command
# substitution rather than `cmd | mapfile` for the same reason as
# _compare's: a process-substitution pipe loses git's real exit status.
_match_commit() {
  local path="$1" want="$2"
  shift 2
  local -a candidates=("$@")
  local commit history_raw
  if [ "${#candidates[@]}" -eq 0 ]; then
    if ! history_raw=$(git rev-list "$REF" -- "$path"); then
      return 2
    fi
    if [ -n "$history_raw" ]; then
      mapfile -t candidates <<<"$history_raw"
    fi
  fi
  for commit in "${candidates[@]}"; do
    if [ "$(_blob_sha "$commit" "$path" || true)" = "$want" ]; then
      printf '%s' "$commit"
      return 0
    fi
  done
  return 1
}

_compare() {
  local label="$1" served_file="$2" path="$3"
  local served_sha head_sha commit behind subject

  served_sha=$(shasum -a 256 "$served_file" | cut -d' ' -f1)

  if ! head_sha=$(_blob_sha "$REF" "$path"); then
    # The path is absent at REF's tip, but "absent now" and "never existed"
    # are different claims -- see the REMOVED-vs-unaccounted note up top.
    # The candidate history is walked ONCE here and handed to _match_commit,
    # rather than asking git for the same path's history twice (a byte-match
    # attempt, then a separate "does it have any history at all" check).
    # Captured as a plain command substitution rather than piped into
    # mapfile via process substitution specifically so a real `git rev-list`
    # failure is visible on `$?` here and doesn't get silently read as "no
    # history" -- see _blob_sha's own `|| return 1` for the same reasoning;
    # `cmd | mapfile` loses that exit status because mapfile succeeds at
    # reading zero bytes regardless of why the pipe produced none.
    local history_raw
    if ! history_raw=$(git rev-list "$REF" -- "$path"); then
      report+=$(printf '\n  %-42s ERROR  could not read history for %s -- git rev-list failed' "$label" "$path")
      errors=$((errors + 1))
      return
    fi
    local -a history=()
    if [ -n "$history_raw" ]; then
      mapfile -t history <<<"$history_raw"
    fi

    if [ "${#history[@]}" -gt 0 ] && commit=$(_match_commit "$path" "$served_sha" "${history[@]}"); then
      subject=$(git log -1 --format='%s' "$commit" | cut -c1-48)
      report+=$(printf '\n  %-42s REMOVED matches %s (%s); %s no longer exists at %s' \
        "$label" "${commit:0:8}" "$subject" "$path" "$REF")
      removed=$((removed + 1))
      return
    fi
    if [ "${#history[@]}" -gt 0 ]; then
      report+=$(printf '\n  %-42s UNACCOUNTED %s existed in %s history but served bytes match no historical version of it' \
        "$label" "$path" "$REF")
    else
      report+=$(printf '\n  %-42s UNACCOUNTED %s never existed in %s history' "$label" "$path" "$REF")
    fi
    unmatched=$((unmatched + 1))
    return
  fi

  if [ "$served_sha" = "$head_sha" ]; then
    report+=$(printf '\n  %-42s current' "$label")
    current=$((current + 1))
    return
  fi

  local match_rc=0
  commit=$(_match_commit "$path" "$served_sha") || match_rc=$?
  if [ "$match_rc" -eq 2 ]; then
    report+=$(printf '\n  %-42s ERROR  could not read history for %s -- git rev-list failed' "$label" "$path")
    errors=$((errors + 1))
    return
  fi
  if [ "$match_rc" -ne 0 ]; then
    report+=$(printf '\n  %-42s UNACCOUNTED matches no commit in %s -- served bytes did not come from this repo' "$label" "$REF")
    unmatched=$((unmatched + 1))
    return
  fi

  behind=$(git rev-list --count "${commit}..${REF}" -- "$path")
  subject=$(git log -1 --format='%s' "$commit" | cut -c1-48)
  report+=$(printf '\n  %-42s STALE  %s behind, serving %s (%s)' \
    "$label" "$behind commit(s)" "${commit:0:8}" "$subject")
  stale=$((stale + 1))
}

echo "Comparing ${BASE} against ${REF}"
echo

# ── the root objects ────────────────────────────────────────────────────────
for obj in "${ROOT_OBJECTS[@]}"; do
  if ! curl -fsSL --max-time 60 "${BASE}/${obj}" -o "${work}/${obj}"; then
    report+=$(printf '\n  %-42s UNREACHABLE at %s/%s' "$obj" "$BASE" "$obj")
    unreachable=$((unreachable + 1))
    continue
  fi
  _compare "$obj" "${work}/${obj}" "$obj"
done

# ── the bundle ──────────────────────────────────────────────────────────────
# Compared member by member rather than as an archive, because a tarball's
# bytes depend on mtimes and ownership: two builds of identical content do not
# hash the same, so the archive itself can never be compared to a commit. The
# members can.
if ! curl -fsSL --max-time 120 "${BASE}/bundle.tar.gz" -o "${work}/bundle.tar.gz"; then
  report+=$(printf '\n  %-42s UNREACHABLE at %s/bundle.tar.gz' "bundle.tar.gz" "$BASE")
  unreachable=$((unreachable + 1))
else
  # Read the member list BEFORE extracting, and refuse anything that cannot
  # correspond to a path in this repo. That is a traversal guard, but it is also
  # what makes the comparison below sound: a member named `../x` or `/etc/x` has
  # no repo path to be compared against, so it has to be reported rather than
  # silently skipped. Symlinks are refused for the same two reasons at once —
  # they are the other half of a tar-slip, and a symlink has no blob to hash.
  #
  # Not relying on tar's own defaults here in either direction. GNU tar strips
  # leading slashes and skips `..` members, bsdtar behaves differently, and the
  # runner's tar is an implementation detail this check should not depend on.
  # Two checks, and NEITHER parses a member name out of verbose output. An
  # earlier revision took the name as awk's $NF, which a member called
  # "foo bar/../../etc/passwd" walks straight through: whitespace splits the
  # name across fields, $NF is "passwd", and the traversal test sees nothing.
  # The guard was defeated by the shape of its own parser.
  #
  # (1) NAMES come from `tar -tzf`, which prints one raw member per line with
  #     no metadata columns, so a space in a name is just a space.
  # (2) TYPES come from `tar -tzvf` but only via $1, the mode string, which is
  #     the FIRST field and therefore cannot be displaced by anything in the
  #     name. The whole line is reported verbatim rather than reconstructed:
  #     refusing does not require knowing which member it was.
  #
  # (2) also generalises past symlinks. Anything that is not a regular file or
  # a directory is refused -- hard links, devices, fifos -- because the
  # question is not "is this the specific vector I thought of" but "can this
  # member correspond to a blob in a git tree", and only those two shapes can.
  bad_paths=$(tar -tzf "${work}/bundle.tar.gz" | awk '
    /^\// { print "absolute path: " $0; next }
    /(^|\/)\.\.(\/|$)/ { print "parent traversal: " $0 }
  ')
  bad_types=$(tar -tzvf "${work}/bundle.tar.gz" | awk '$1 !~ /^[-d]/ { print "not a file or directory: " $0 }')

  # A name containing a NEWLINE is deliberately not guarded here, because the
  # guard that used to sit in this spot could not fire: both listings expand
  # such a name across two lines identically, so their line counts agree and a
  # count comparison sees nothing.
  #
  # It is handled where it actually lives instead — the loop below reads
  # null-delimited names, so an embedded newline cannot split one member into
  # two. Such a member then has no counterpart path in the repo and is reported
  # as unaccounted, which is exactly what it is.
  bad_members=$(printf '%s\n%s' "$bad_paths" "$bad_types" | sed '/^$/d')
  if [ -n "$bad_members" ]; then
    report+=$(printf '\n  %-42s REFUSED %s member(s), archive not extracted:\n%s' \
      "bundle.tar.gz" "$(printf '%s\n' "$bad_members" | wc -l | tr -d ' ')" \
      "$(printf '%s\n' "$bad_members" | sed 's/^/      /')")
    refused=$((refused + 1))
  else
    mkdir -p "${work}/bundle"
    # --no-same-owner: the served bundle's headers carry a workstation's uid and
    # group, which are meaningless here. Explicit rather than relying on tar
    # dropping them because the process is unprivileged.
    tar -xzf "${work}/bundle.tar.gz" -C "${work}/bundle" --no-same-owner
    # Null-delimited: a member name may contain a newline, and a line-based
    # read splits it into two names, neither of which exists. That crashed the
    # comparison below with "shasum: .../bundle/a: No such file or directory".
    while IFS= read -r -d '' member; do
      rel="${member#./}"
      _compare "bundle.tar.gz -> ${rel}" "${work}/bundle/${rel}" "$rel"
    done < <(cd "${work}/bundle" && find . -type f -print0 | sort -z)
  fi
fi

printf '%s\n\n' "$report"
printf 'current %d, stale %d, removed %d, unaccounted %d, unreachable %d, refused %d, errors %d\n' \
  "$current" "$stale" "$removed" "$unmatched" "$unreachable" "$refused" "$errors"

# Distinct failures with distinct remedies, so distinct messages rather than
# one counter. Collapsing them was the first thing the unreachable-channel
# dry run exposed: it printed "did not come from this repository" about a
# 404, which sends the reader looking for the wrong problem. `errors` is the
# same principle applied to a local git failure: it asserts nothing about
# the served bytes, so it must never share a message with a bucket that
# does (unmatched's "did not come from this repository" is a claim about
# the bytes; a `git rev-list` failure supports no claim about them at all).
if [ "$refused" -gt 0 ]; then
  echo
  echo "::error::A served archive contains member(s) that cannot correspond to a path in this repository and was NOT extracted. An absolute path, a parent traversal or a symlink in bundle.tar.gz means the archive was not built by the documented recipe — find out what published it before trusting anything else about the channel."
fi

if [ "$unreachable" -gt 0 ]; then
  echo
  echo "::error::${unreachable} artefact(s) could not be fetched from ${BASE}. Either the channel is down or an object is missing from the bucket — check the URL before reading anything else here, because nothing below was measured."
fi

if [ "$errors" -gt 0 ]; then
  echo
  echo "::error::${errors} artefact(s) could not be checked because \`git rev-list\` failed locally — this says nothing about what the channel is serving. Rerun, or investigate the checkout (fetch depth, repo corruption), not the channel."
fi

if [ "$unmatched" -gt 0 ]; then
  echo
  echo "::error::${unmatched} served artefact(s) could not be accounted for against ${REF}: their bytes never matched any commit of that path, ever. A served copy like this did not come from this repository — check which repo published it before publishing over it. If most of these carry a '._' prefix, that is macOS's tar/libarchive shipping one AppleDouble metadata sidecar per real file (COPYFILE_DISABLE=1 or --no-mac-metadata prevents it) — a strong sign the bundle was built and published by hand on a Mac rather than by CI, and worth chasing regardless of how many of the entries below are that same noise."
fi

if [ "$removed" -gt 0 ]; then
  echo
  echo "::error::${removed} served artefact(s) match a path this repository has since deleted. The channel is still serving content this repo dropped — republish, and find out why the deletion never reached the channel."
fi

if [ "$stale" -gt 0 ]; then
  echo
  echo "::error::${stale} served artefact(s) are behind ${REF}. Customers are fetching an older copy than this repository holds. Publishing is the 'Publish installer' workflow in caura-ai/caura-onprem (the private half this repo mirrors) — it pushes on every push to ITS main, not this repo's; verify the two are in sync before assuming a push here republishes anything."
fi

if [ "$stale" -gt 0 ] || [ "$unmatched" -gt 0 ] || [ "$removed" -gt 0 ] || [ "$unreachable" -gt 0 ] || [ "$refused" -gt 0 ] || [ "$errors" -gt 0 ]; then
  exit 1
fi

echo
echo "The channel matches ${REF}."
