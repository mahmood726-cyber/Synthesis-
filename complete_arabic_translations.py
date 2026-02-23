"""Complete Arabic translations for all -ar.html course files.

Finds remaining English text nodes and translates them to Arabic
using Google Translate API. Creates backups before modifying files.

Usage:
  python complete_arabic_translations.py --dry-run
  python complete_arabic_translations.py
  python complete_arabic_translations.py --files cast-when-certainty-kills-ar.html
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Tuple

# UTF-8 stdout for Windows
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

WORDS_RE = re.compile(r"\b[A-Za-z][A-Za-z'\-]{2,}\b")
CORE_ENGLISH = {
    "the", "is", "are", "were", "was", "to", "of", "and", "or", "not", "for",
    "with", "from", "into", "about", "over", "under", "this", "that", "these",
    "those", "you", "your", "we", "our", "has", "have", "had", "been", "will",
    "would", "can", "could", "should", "may", "might", "shall", "must", "do",
    "does", "did", "but", "than", "then", "when", "where", "which", "who",
    "whom", "how", "what", "why", "all", "each", "every", "both", "few",
    "more", "most", "some", "any", "no", "other", "only", "also", "just",
    "because", "if", "while", "after", "before", "during", "between",
    "through", "against", "above", "below", "its", "their", "his", "her",
    "they", "them", "she", "he", "it", "being", "become", "became",
}

SKIP_PARENTS = {"script", "style", "code", "pre", "noscript"}


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def has_arabic(text: str) -> bool:
    """Check if text contains Arabic characters."""
    for ch in text:
        if "\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f" or "\ufb50" <= ch <= "\ufdff" or "\ufe70" <= ch <= "\ufeff":
            return True
    return False


def is_english_like(text: str) -> bool:
    """Detect English text nodes that need translation.

    Strategy: if text is predominantly ASCII letters (no Arabic), it's English.
    This catches medical/technical phrases that lack common function words.
    """
    text = normalize(text)
    if len(text) < 5:
        return False
    # Skip URLs, code-like text
    if text.startswith(("http://", "https://", "/*", "//", "<!--")):
        return False
    if "<-" in text:
        return False
    # Skip keyboard shortcut labels (arrows + key names)
    if re.search(r"[→←↑↓]", text):
        return False

    words = [w.lower() for w in WORDS_RE.findall(text)]
    if len(words) < 1:
        return False

    letters = sum(ch.isalpha() for ch in text)
    ascii_letters = sum(("A" <= ch <= "Z") or ("a" <= ch <= "z") for ch in text)
    if letters == 0:
        return False

    # If text has Arabic characters mixed in, it's already (partially) translated
    # Only target purely English text nodes
    if has_arabic(text):
        return False

    ratio = ascii_letters / letters
    if ratio < 0.75:
        return False

    # If purely ASCII letters (no Arabic/Cyrillic/CJK), it's English
    # even without function-word signal
    return True


def translate_google(text: str, cache: Dict[str, str]) -> str:
    """Translate English text to Arabic using Google Translate."""
    text = normalize(text)
    if not text:
        return text
    if text in cache:
        return cache[text]

    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": text}
    )
    url = f"https://translate.googleapis.com/translate_a/single?{params}"

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = resp.read().decode("utf-8")
            obj = json.loads(payload)
            translated = "".join(part[0] for part in obj[0]).strip()
            if translated:
                cache[text] = translated
                return translated
        except Exception as exc:
            if attempt == 2:
                print(f"  [WARN] Translation failed: {text[:60]!r} -> {exc}")
                cache[text] = text  # Keep original on failure
                return text
            time.sleep(0.3 * (attempt + 1))
    return text


def batch_translate(texts: List[str], cache: Dict[str, str]) -> None:
    """Translate a batch of texts, using separator-based batching for efficiency."""
    unique = []
    seen = set()
    for t in texts:
        t = normalize(t)
        if not t or t in cache or t in seen:
            continue
        unique.append(t)
        seen.add(t)

    if not unique:
        return

    SEP = " <<<SEP>>> "
    i = 0
    while i < len(unique):
        chunk = []
        total_len = 0
        while i < len(unique):
            nxt = unique[i]
            add_len = len(nxt) + (len(SEP) if chunk else 0)
            if chunk and (len(chunk) >= 25 or total_len + add_len > 3500):
                break
            chunk.append(nxt)
            total_len += add_len
            i += 1

        joined = SEP.join(chunk)
        try:
            params = urllib.parse.urlencode(
                {"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": joined}
            )
            url = f"https://translate.googleapis.com/translate_a/single?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8")
            obj = json.loads(payload)
            translated = "".join(part[0] for part in obj[0]).strip()

            # Try to split by separator variants (Google may modify it)
            sep_variants = ["<<<SEP>>>", "<<< SEP >>>", "<<<sep>>>", "<<< sep >>>",
                           "«SEP»", "«sep»", "<<<SEP >>>", "<<< SEP>>>"]
            parts = None
            for sv in sep_variants:
                candidate = translated.split(sv)
                if len(candidate) == len(chunk):
                    parts = candidate
                    break
            # Also try the original separator
            if parts is None:
                candidate = translated.split(SEP.strip())
                if len(candidate) == len(chunk):
                    parts = candidate

            if parts and len(parts) == len(chunk):
                for src, tr in zip(chunk, parts):
                    out = tr.strip()
                    cache[src] = out if out else src
            else:
                # Fallback: translate individually
                for src in chunk:
                    translate_google(src, cache)
                    time.sleep(0.05)
        except Exception:
            # Fallback: translate individually
            for src in chunk:
                translate_google(src, cache)
                time.sleep(0.05)

        time.sleep(0.08)


def process_file(path: str, cache: Dict[str, str], dry_run: bool) -> int:
    """Process a single Arabic HTML file. Returns number of translations made."""
    from bs4 import BeautifulSoup, NavigableString

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")

    # Collect English text nodes
    targets: List[Tuple[NavigableString, str, str, str]] = []
    phrases: List[str] = []

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent.name if node.parent else ""
        if parent in SKIP_PARENTS:
            continue
        original = str(node)
        stripped = normalize(original)
        if not is_english_like(stripped):
            continue

        leading = original[: len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()) :]
        targets.append((node, stripped, leading, trailing))
        phrases.append(stripped)

    if not targets:
        return 0

    # Batch translate
    batch_translate(phrases, cache)

    # Apply translations
    changes = 0
    for node, stripped, leading, trailing in targets:
        translated = cache.get(stripped, stripped)
        if translated and translated != stripped:
            replacement = f"{leading}{translated}{trailing}"
            node.replace_with(NavigableString(replacement))
            changes += 1

    if changes > 0 and not dry_run:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(str(soup))

    return changes


def main():
    parser = argparse.ArgumentParser(description="Complete Arabic translations")
    parser.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--files", default="", help="Comma-separated file subset")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    selected = {x.strip() for x in args.files.split(",") if x.strip()}

    # Find all Arabic HTML files
    ar_files = sorted(
        f for f in os.listdir(root)
        if f.endswith("-ar.html")
    )
    if selected:
        ar_files = [f for f in ar_files if f in selected]

    if not ar_files:
        print("No Arabic HTML files found.")
        return

    print(f"Found {len(ar_files)} Arabic files to process")

    # Create backup
    if not args.dry_run and not args.no_backup:
        backup_dir = os.path.join(root, f"_ar_backup_{time.strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(backup_dir, exist_ok=True)
        for f in ar_files:
            shutil.copy2(os.path.join(root, f), os.path.join(backup_dir, f))
        print(f"Backups saved to: {os.path.basename(backup_dir)}")

    cache: Dict[str, str] = {}
    total_changes = 0

    # Sort by expected work (smallest first for quick wins)
    for fn in ar_files:
        path = os.path.join(root, fn)
        print(f"\n[{fn}] Processing...", flush=True)
        try:
            changes = process_file(path, cache, args.dry_run)
            total_changes += changes
            print(f"[{fn}] -> {changes} translations applied", flush=True)
        except Exception as exc:
            print(f"[{fn}] ERROR: {exc}", flush=True)

    print(f"\n{'='*60}")
    print(f"Total translations: {total_changes}")
    print(f"Cached phrases: {len(cache)}")
    if args.dry_run:
        print("DRY RUN - no files modified")


if __name__ == "__main__":
    main()
