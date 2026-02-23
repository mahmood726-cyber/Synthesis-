#!/usr/bin/env python
"""
audit_arabic_gaps.py
Scans all *-ar.html files for untranslated English text nodes.

Heuristic: a text node is "English-like" if:
  - It has 3+ words
  - >85% of its letter characters are ASCII (a-z, A-Z)
  - It contains at least one common English stop word
"""

import io
import os
import sys
import glob

# Force UTF-8 stdout on Windows
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from bs4 import BeautifulSoup, NavigableString, Comment
except ImportError:
    print("ERROR: beautifulsoup4 not installed. Run: pip install beautifulsoup4")
    sys.exit(1)

# Tags whose text content we skip entirely
SKIP_TAGS = {"script", "style", "code", "pre", "noscript", "svg", "math"}

# Common English stop words (lowercase)
ENGLISH_STOPS = {
    "the", "is", "are", "and", "or", "for", "with", "from", "this", "that",
    "in", "of", "to", "a", "an", "it", "be", "was", "were", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "not", "no", "but", "if",
    "then", "than", "so", "as", "at", "by", "on", "up", "out", "about",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "each", "all", "both", "few", "more", "most", "other",
    "some", "such", "only", "own", "same", "too", "very", "just",
    "because", "until", "while", "how", "what", "which", "who", "whom",
    "where", "when", "why", "here", "there", "these", "those",
    "your", "you", "we", "they", "he", "she", "its", "our", "their",
    "my", "his", "her", "also", "any", "many", "much",
}


def is_inside_skip_tag(element):
    """Check if element is nested inside a tag we should skip."""
    for parent in element.parents:
        if parent.name and parent.name.lower() in SKIP_TAGS:
            return True
    return False


def is_english_like(text):
    """
    Return True if the text looks like untranslated English.
    Criteria:
      1. Has 3+ words
      2. >85% of letter characters are ASCII letters
      3. Contains at least one common English stop word
    """
    text = text.strip()
    if not text:
        return False

    words = text.split()
    if len(words) < 3:
        return False

    # Count ASCII vs non-ASCII letters
    ascii_letters = 0
    total_letters = 0
    for ch in text:
        if ch.isalpha():
            total_letters += 1
            if ch.isascii():
                ascii_letters += 1

    if total_letters == 0:
        return False

    ascii_ratio = ascii_letters / total_letters
    if ascii_ratio < 0.85:
        return False

    # Check for English stop words
    lower_words = {w.lower().strip(".,;:!?()[]{}\"'") for w in words}
    stop_hits = lower_words & ENGLISH_STOPS
    if len(stop_hits) < 1:
        return False

    return True


def audit_file(filepath):
    """
    Parse an HTML file and find English-like text nodes.
    Returns (total_text_nodes, english_nodes_count, english_examples).
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f, "html.parser")

    total_text_nodes = 0
    english_nodes = []

    for element in soup.descendants:
        if not isinstance(element, NavigableString):
            continue
        if isinstance(element, Comment):
            continue
        if is_inside_skip_tag(element):
            continue

        text = element.strip()
        if not text:
            continue

        total_text_nodes += 1

        if is_english_like(text):
            english_nodes.append(text)

    return total_text_nodes, len(english_nodes), english_nodes


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Find all *-ar.html files
    pattern = os.path.join(base_dir, "*-ar.html")
    ar_files = sorted(glob.glob(pattern))

    if not ar_files:
        print("No *-ar.html files found in", base_dir)
        sys.exit(1)

    print("=" * 80)
    print("ARABIC TRANSLATION GAP AUDIT")
    print(f"Directory: {base_dir}")
    print(f"Files found: {len(ar_files)}")
    print("=" * 80)
    print()

    grand_total_text = 0
    grand_total_english = 0
    file_results = []

    for filepath in ar_files:
        filename = os.path.basename(filepath)
        print(f"--- {filename} ---")

        total_text, eng_count, eng_examples = audit_file(filepath)
        grand_total_text += total_text
        grand_total_english += eng_count
        file_results.append((filename, total_text, eng_count, eng_examples))

        print(f"  Total text nodes: {total_text}")
        print(f"  English-like nodes: {eng_count}")

        if eng_count > 0:
            pct = (eng_count / total_text * 100) if total_text > 0 else 0
            print(f"  Untranslated ratio: {pct:.1f}%")
            print(f"  First {min(5, len(eng_examples))} examples:")
            for i, ex in enumerate(eng_examples[:5]):
                truncated = ex[:100] + ("..." if len(ex) > 100 else "")
                truncated = truncated.replace("\n", " ").replace("\r", "")
                print(f"    [{i+1}] {truncated}")
        else:
            print("  (No untranslated English detected)")
        print()

    # Summary
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total Arabic files scanned:       {len(ar_files)}")
    print(f"Total text nodes (all files):     {grand_total_text}")
    print(f"Total English-like nodes:         {grand_total_english}")
    if grand_total_text > 0:
        overall_pct = grand_total_english / grand_total_text * 100
        print(f"Overall untranslated ratio:       {overall_pct:.1f}%")
    print()

    # Rank files by English node count (worst first)
    ranked = sorted(file_results, key=lambda x: x[2], reverse=True)
    print("Files ranked by untranslated English nodes (worst first):")
    print(f"{'Rank':<5} {'English':<10} {'Total':<10} {'Pct':<8} {'File'}")
    print(f"{'-'*5} {'-'*10} {'-'*10} {'-'*8} {'-'*40}")
    for rank, (fname, total, eng, _) in enumerate(ranked, 1):
        pct = (eng / total * 100) if total > 0 else 0
        print(f"{rank:<5} {eng:<10} {total:<10} {pct:<7.1f}% {fname}")

    print("\nDone.")


if __name__ == "__main__":
    main()
