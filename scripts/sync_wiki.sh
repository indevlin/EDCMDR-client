#!/usr/bin/env bash
# scripts/sync_wiki.sh
#
# Copies the player-facing docs into the wiki repo. The README, which is for
# people working on the client, is not copied.
#
# Pages:
#   docs/wiki/Home.md            → Home.md
#   docs/wiki/NN_some_page.md    → Some-Page.md (NN orders the sidebar)
#   CHANGELOG.md                 → Release-Notes.md
#
# Links resolve against the source file's directory. A link to another synced
# doc becomes its wiki page; an image becomes a raw URL; anything else in the
# repo becomes a blob (file) or tree (directory) URL on main. Pages in the wiki
# that no source produced are removed. _Sidebar.md is regenerated.
#
# Usage (local):
#   WIKI_DIR=/path/to/EDLD.wiki bash scripts/sync_wiki.sh
#
# Usage (CI — called by .github/workflows/sync-wiki.yml):
#   Env vars WIKI_DIR and REPO_DIR are set by the workflow.
#
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)}"
WIKI_DIR="${WIKI_DIR:-}"

if [[ -z "$WIKI_DIR" ]]; then
    echo "ERROR: WIKI_DIR is not set." >&2
    exit 1
fi

echo "Syncing docs to wiki at: $WIKI_DIR"

REPO_DIR="$REPO_DIR" WIKI_DIR="$WIKI_DIR" \
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-indevlin/EDCMDR-client}" \
python3 - <<'PY'
import os, posixpath, re, sys
from pathlib import Path

repo = Path(os.environ["REPO_DIR"])
wiki = Path(os.environ["WIKI_DIR"])
slug = os.environ["GITHUB_REPOSITORY"]
blob = f"https://github.com/{slug}/blob/main/"
tree = f"https://github.com/{slug}/tree/main/"
raw = f"https://raw.githubusercontent.com/{slug}/main/"

FIXED = {
    "docs/wiki/Home.md": "Home",
    "CHANGELOG.md": "Release-Notes",
}
WORDS = {"with": "With", "edcmdr": "EDCMDR"}
IMAGE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico)$", re.I)


def wiki_name(rel):
    if rel in FIXED:
        return FIXED[rel]
    base = re.sub(r"^\d+[_-]", "", Path(rel).stem)
    return "-".join(WORDS.get(w.lower(), w[:1].upper() + w[1:].lower())
                    for w in re.split(r"[_-]+", base) if w)


pages = {rel: wiki_name(rel) for rel in FIXED if (repo / rel).is_file()}
guides = sorted(p.name for p in (repo / "docs/wiki").glob("*.md") if p.name != "Home.md")
for n in guides:
    pages[f"docs/wiki/{n}"] = wiki_name(n)

names = list(pages.values())
dupes = {n for n in names if names.count(n) > 1}
if dupes:
    sys.exit(f"ERROR: wiki page names collide: {sorted(dupes)}")


def resolve(src, target, image):
    if re.match(r"^([a-z][a-z0-9+.-]*:|#|//)", target, re.I):
        return target
    path, _, anchor = target.partition("#")
    frag = f"#{anchor}" if anchor else ""
    rel = posixpath.normpath(posixpath.join(posixpath.dirname(src), path))
    if rel.startswith("../"):
        print(f"  WARNING: {src}: link leaves the repo: {target}", file=sys.stderr)
        return target
    if rel in pages:
        return pages[rel] + frag
    full = repo / rel
    if not full.exists():
        print(f"  WARNING: {src}: missing target: {target}", file=sys.stderr)
    if image or IMAGE.search(rel):
        return raw + rel
    return (tree if full.is_dir() else blob) + rel + frag


MD_LINK = re.compile(r"(!?)\[((?:[^\]\[]|\[[^\]]*\])*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)")
ATTR = re.compile(r"\b(href|src)=\"([^\"]+)\"")
FENCE = re.compile(r"^\s*(```|~~~)")


def links(src, chunk):
    chunk = MD_LINK.sub(lambda m: f"{m[1]}[{m[2]}]({resolve(src, m[3], bool(m[1]))}{m[4] or ''})", chunk)
    return ATTR.sub(lambda m: f'{m[1]}="{resolve(src, m[2], m[1] == "src")}"', chunk)


def rewrite(src, text):
    out, prose, fenced = [], [], False
    for line in text.splitlines(keepends=True):
        if FENCE.match(line):
            if not fenced:
                out.append(links(src, "".join(prose)))
                prose = []
            fenced = not fenced
            out.append(line)
        elif fenced:
            out.append(line)
        else:
            prose.append(line)
    out.append(links(src, "".join(prose)))
    return "".join(out)


written = set()
for rel, name in sorted(pages.items()):
    text = (repo / rel).read_text(encoding="utf-8")
    (wiki / f"{name}.md").write_text(rewrite(rel, text), encoding="utf-8")
    written.add(f"{name}.md")
    print(f"  copied: {rel} → {name}.md")


def entry(rel):
    name = pages[rel]
    return f"- [[{name.replace('-', ' ')}|{name}]]"


side = ["## EDCMDR client", "", entry("docs/wiki/Home.md")]
if guides:
    side += [""] + [entry(f"docs/wiki/{n}") for n in guides]
if "CHANGELOG.md" in pages:
    side += ["", "**Releases**", entry("CHANGELOG.md")]
(wiki / "_Sidebar.md").write_text("\n".join(side) + "\n", encoding="utf-8")
written.add("_Sidebar.md")
print("  wrote: _Sidebar.md")

for page in sorted(wiki.glob("*.md")):
    if page.name not in written and not page.name.startswith("_"):
        page.unlink()
        print(f"  removed: {page.name}")
PY

echo ""
echo "Sync complete."
